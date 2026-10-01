"""할당·대사·미사용 판정 로직.

중복 발급 방지 핵심 규칙
- NetBox의 IP 중복 검사는 모델 clean()(애플리케이션 단)에서만 수행되고 DB 제약은 없다.
- NetBox REST API는 IP 생성/수정/삭제를 전역 advisory lock('available-ips')으로 직렬화한다.
- 따라서 이 플러그인의 모든 IP 쓰기도 **같은 락을 트랜잭션 바깥에서** 잡는다.
  (세션 락이므로 트랜잭션 안에서 잡으면 커밋 전에 풀려 경합 창이 생김)
"""
import datetime as dt
import re

from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.utils import timezone
from django_pg_utils import advisory_lock
from netaddr import IPNetwork

from ipam.models import IPAddress, Prefix
from netbox.constants import ADVISORY_LOCK_KEYS
from netbox.plugins import get_plugin_config

from .models import ArpEntry, Discrepancy, IPRequest, MacEntry, RequestStatusChoices

PLUGIN = 'netbox_ip_request'
IP_LOCK = ADVISORY_LOCK_KEYS['available-ips']


def cfg(key):
    return get_plugin_config(PLUGIN, key)


class AllocationError(Exception):
    pass


_MAC = re.compile(r'^[0-9a-f]{12}$')


def norm_mac(v):
    """aa-bb-.., AABB.CCDD.EEFF, aabbccddeeff → aa:bb:cc:dd:ee:ff (멀티캐스트/브로드캐스트 거부)"""
    if v is None or str(v).strip() in ('', 'nan', '-'):
        return None
    h = re.sub(r'[^0-9a-fA-F]', '', str(v)).lower()
    if not _MAC.match(h):
        raise ValueError(f'MAC 형식 오류: {v}')
    if h in ('000000000000', 'ffffffffffff') or int(h[1], 16) & 1:
        raise ValueError(f'사용할 수 없는 MAC: {v}')
    return ':'.join(h[i:i + 2] for i in range(0, 12, 2))


def mac_in_use(mac):
    ip = IPAddress.objects.filter(custom_field_data__host_mac=mac, status='active').first()
    return str(ip) if ip else None


def _host(ipaddr):
    return str(ipaddr.address.ip)


# --------------------------------------------------------------------- 할당
def _recently_seen(prefix, now):
    cutoff = now - dt.timedelta(days=cfg('arp_guard_days'))
    return {str(ip) for ip in ArpEntry.objects.filter(last_seen__gt=cutoff)
            .extra(where=['ip << %s::cidr'], params=[str(prefix.prefix)]).values_list('ip', flat=True)}


def _allocate_locked(prefix, *, user_name, tenant=None, mac=None, hostname='', purpose='', period_days=None,
                     now=None):
    """호출자가 IP_LOCK + transaction을 잡은 상태에서만 호출."""
    now = now or timezone.now()
    seen = _recently_seen(prefix, now)
    for ip in prefix.get_available_ips():
        if str(ip) in seen:
            continue  # 대장엔 비었지만 네트워크에서 쓰이고 있는 IP
        obj = IPAddress(address=IPNetwork(f'{ip}/{prefix.prefix.prefixlen}'), vrf=prefix.vrf, tenant=tenant,
                        status='active', dns_name=hostname or '', description=purpose[:200])
        obj.custom_field_data.update({
            'ip_user': user_name, 'host_mac': norm_mac(mac),
            'assigned_on': now.date().isoformat(),
            'expires_on': (now + dt.timedelta(days=period_days)).date().isoformat() if period_days else None,
        })
        obj.full_clean()
        obj.save()
        return obj
    raise AllocationError(f'{prefix}: 할당 가능한 IP가 없습니다')


def allocate(prefix, **kw):
    with advisory_lock(IP_LOCK):          # 락이 바깥
        with transaction.atomic():        # 트랜잭션이 안쪽 → 커밋 후 락 해제
            return _allocate_locked(prefix, **kw)


# --------------------------------------------------------------------- 신청 워크플로
def approve(request_pk, approver, now=None):
    """승인 = 할당. 한 트랜잭션: 할당 실패 시 승인도 롤백. 동시 승인은 1건만 성공."""
    with advisory_lock(IP_LOCK):
        with transaction.atomic():
            req = IPRequest.objects.select_for_update().get(pk=request_pk)
            if req.status != RequestStatusChoices.SUBMITTED:
                raise AllocationError(f'{req}: 이미 처리된 신청입니다({req.get_status_display()})')
            req.snapshot()
            ip = _allocate_locked(req.prefix, user_name=req.requester, tenant=req.tenant, mac=req.mac,
                                  hostname=req.hostname, purpose=req.purpose, period_days=req.period_days, now=now)
            req.status, req.approver, req.ip_address = RequestStatusChoices.ALLOCATED, approver, ip
            req.save()
            return ip


def reject(request_pk, approver, reason):
    if not (reason or '').strip():
        raise AllocationError('반려 사유는 필수입니다')
    with transaction.atomic():
        req = IPRequest.objects.select_for_update().get(pk=request_pk)
        if req.status != RequestStatusChoices.SUBMITTED:
            raise AllocationError(f'{req}: 이미 처리된 신청입니다')
        req.snapshot()
        req.status, req.approver, req.reason = RequestStatusChoices.REJECTED, approver, reason
        req.save()


# --------------------------------------------------------------------- 수집 대사
def _flag(kind, ip, detail):
    _, created = Discrepancy.objects.get_or_create(kind=kind, ip=ip, resolved=False, defaults={'detail': detail[:300]})
    return int(created)


def _managed(ip):
    return Prefix.objects.filter(prefix__net_contains=ip).exists()


def ingest_arp(device, entries, now=None):
    """entries: [(ip, mac, interface)] 한 번의 폴링 결과 → 관측 적재, last_seen 갱신, 불일치 탐지."""
    now = now or timezone.now()
    found = {'unregistered_use': 0, 'mac_mismatch': 0, 'ip_conflict': 0, 'revived': 0}
    by_ip = {}
    with transaction.atomic():
        for ip, mac, itf in entries:
            mac = norm_mac(mac)
            by_ip.setdefault(ip, set()).add(mac)
            obj, created = ArpEntry.objects.get_or_create(ip=ip, mac=mac, device=device,
                                                          defaults={'interface': itf, 'first_seen': now, 'last_seen': now})
            if not created:
                ArpEntry.objects.filter(pk=obj.pk).update(last_seen=now, interface=itf)
        ips = list(by_ip)
        # last_seen 갱신은 변경 이력(changelog)을 남기지 않도록 SQL로 일괄 처리 (5분마다 이력 폭증 방지)
        with connection.cursor() as cur:
            cur.execute("""UPDATE ipam_ipaddress SET custom_field_data =
                             jsonb_set(custom_field_data, '{last_seen}', to_jsonb(%s::text))
                           WHERE host(address)::inet = ANY(%s::inet[])""", [now.isoformat(timespec='seconds'), ips])
        objs = {}
        for o in IPAddress.objects.extra(where=['host(address)::inet = ANY(%s::inet[])'], params=[ips]):
            objs.setdefault(_host(o), []).append(o)
        for ip, macs in by_ip.items():
            if len(macs) > 1:
                found['ip_conflict'] += _flag('ip_conflict', ip, f'{device}: {sorted(macs)}')
            rows = objs.get(ip, [])
            if not rows:
                if _managed(ip):
                    found['unregistered_use'] += _flag('unregistered_use', ip, f'{device}: {sorted(macs)} (대장 없음)')
                continue
            for o in rows:
                if o.status == 'quarantine':
                    found['unregistered_use'] += _flag('unregistered_use', ip, f'{device}: {sorted(macs)} (격리 중 사용)')
                elif o.status == 'dormant':
                    o.snapshot(); o.status = 'active'; o.save()
                    found['revived'] += 1
                reg = o.custom_field_data.get('host_mac')
                if o.status == 'active' and not reg and len(macs) == 1:
                    # 대장에 MAC이 비어 있으면 ARP로 확인된 MAC을 채움(스위치 포트 대조의 전제)
                    o.snapshot(); o.custom_field_data['host_mac'] = next(iter(macs)); o.save()
                    found['mac_filled'] = found.get('mac_filled', 0) + 1
                elif o.status in ('active', 'dormant') and reg and reg not in macs:
                    found['mac_mismatch'] += _flag('mac_mismatch', ip, f'대장 {reg} ≠ 관측 {sorted(macs)}')
    return found


def ingest_mac(device, entries, now=None):
    """entries: [(mac, vlan, port)] 한 장비 1회 수집분. 대량(수천 건)도 쿼리 몇 번으로 처리."""
    now = now or timezone.now()
    rows = {}
    for mac, vlan, port in entries:
        rows[(norm_mac(mac), str(port))] = vlan
    with transaction.atomic():
        have = {(e.mac, e.port): e.pk for e in MacEntry.objects.filter(device=device).only('pk', 'mac', 'port')}
        seen = [have[k] for k in rows if k in have]
        for i in range(0, len(seen), 5000):
            MacEntry.objects.filter(pk__in=seen[i:i + 5000]).update(last_seen=now)
        MacEntry.objects.bulk_create([MacEntry(mac=m, device=device, port=p, vlan='' if v is None else str(v),
                                               first_seen=now, last_seen=now)
                                      for (m, p), v in rows.items() if (m, p) not in have],
                                     batch_size=2000, ignore_conflicts=True)


def _edge_port(e):
    """사용자 포트인지(업링크/LAG 아님). 같은 수집 회차(last_seen 동일)에 그 포트에서 본 MAC이 3개 이하면 사용자 포트.
    과거 회차까지 세면 사용자가 자주 바뀌는 포트가 업링크로 오판되므로 회차 단위로 센다."""
    return MacEntry.objects.filter(device=e.device, port=e.port, last_seen=e.last_seen).count() <= 3


def locate(ip):
    """IP → MAC(ARP) → 액세스 포트(MAC 테이블). MAC이 4개 이상 학습된 포트(업링크/LAG)는 제외."""
    arp = ArpEntry.objects.filter(ip=ip).order_by('-last_seen').first()
    if not arp:
        return None
    for e in MacEntry.objects.filter(mac=arp.mac).order_by('-last_seen'):
        if _edge_port(e):
            return {'mac': arp.mac, 'device': e.device, 'port': e.port}
    return {'mac': arp.mac, 'device': None, 'port': None}


# --------------------------------------------------------------------- 장기 미사용 / 회수
def classify_dormant(now=None):
    """active IP 중 COALESCE(last_seen, assigned_on, created) 가 dormant_days 이상 지난 것 → dormant.
    제외: 판정 예외(dormant_exempt), 역할 IP(VIP/anycast 등), 장비 인터페이스에 붙은 IP(장비 자기 IP는 ARP에 안 보임)."""
    now = now or timezone.now()
    cutoff = now - dt.timedelta(days=cfg('dormant_days'))
    with connection.cursor() as cur:
        cur.execute("""
            UPDATE ipam_ipaddress SET status='dormant', last_updated=now()
             WHERE status='active' AND COALESCE(role,'')=''
               AND assigned_object_id IS NULL
               AND COALESCE((custom_field_data->>'dormant_exempt')::boolean, false) = false
               AND COALESCE((custom_field_data->>'last_seen')::timestamptz,
                            (custom_field_data->>'assigned_on')::date::timestamptz, created) <= %s
            RETURNING host(address)""", [cutoff])
        return sorted(r[0] for r in cur.fetchall())


def reclaim(ip_pk, now=None):
    now = now or timezone.now()
    with advisory_lock(IP_LOCK):
        with transaction.atomic():
            o = IPAddress.objects.select_for_update().get(pk=ip_pk)
            if o.status != 'dormant':
                raise AllocationError(f'{o}: dormant 상태가 아니라 회수할 수 없습니다')
            o.snapshot()
            o.status = 'quarantine'
            o.custom_field_data['quarantine_until'] = (now + dt.timedelta(days=cfg('quarantine_days'))).date().isoformat()
            o.save()


def release_quarantine(now=None):
    """격리 만료 + 격리 기간 중 미관측 → IP 객체 삭제(= NetBox에서 '가용'). 삭제 이력은 changelog에 남음."""
    now = now or timezone.now()
    released = []
    with advisory_lock(IP_LOCK):
        with transaction.atomic():
            for o in IPAddress.objects.filter(status='quarantine'):
                until = o.custom_field_data.get('quarantine_until')
                if not until or dt.date.fromisoformat(until) > now.date():
                    continue
                start = dt.datetime.fromisoformat(until).replace(tzinfo=dt.timezone.utc) - dt.timedelta(days=cfg('quarantine_days'))
                ls = o.custom_field_data.get('last_seen')
                if ls and dt.datetime.fromisoformat(ls) >= start:
                    continue  # 격리 중 다시 사용됨 → 해제 보류
                released.append(_host(o))
                o.snapshot()
                o.delete()
    return released


# --------------------------------------------------------------------- 스위치 포트 대사
def _port_key(name):
    """포트 이름 비교용 키. 'ge-0/0/9.0' → '9', 'GigabitEthernet1/0/9' → '9', '9' → '9'.
    엑셀에는 포트 번호만 적혀 있으므로, 등록값이 숫자뿐이면 관측 포트의 마지막 숫자와 비교한다."""
    n = re.sub(r'\.\d+$', '', (name or '').strip())  # 논리 유닛(.0) 제거
    m = re.search(r'(\d+)$', n)
    return m.group(1) if m else n.lower()


def _same_port(registered, observed):
    if not registered or not observed:
        return False
    r = registered.strip()
    if r.isdigit():
        return _port_key(observed) == r
    return re.sub(r'\.\d+$', '', r).lower() == re.sub(r'\.\d+$', '', observed.strip()).lower()


def check_ports(fill_empty=True):
    """MAC 테이블(MacEntry)에서 본 실제 연결 위치와 IP에 등록된 스위치·포트를 비교.
    - 등록값이 비어 있으면 관측값을 제안(fill_empty=True면 채움)
    - 다르면 port_mismatch 불일치 알림
    반환: {'matched', 'filled', 'mismatch', 'not_seen'}"""
    from dcim.models import Device, Interface
    out = {'matched': 0, 'filled': 0, 'mismatch': 0, 'not_seen': 0}
    for ip in IPAddress.objects.filter(status='active').exclude(custom_field_data__host_mac=None):
        mac = ip.custom_field_data.get('host_mac')
        if not mac:
            continue
        seen = [e for e in MacEntry.objects.filter(mac=mac).order_by('-last_seen') if _edge_port(e)]  # 업링크 제외
        if not seen:
            out['not_seen'] += 1
            continue
        obs = seen[0]
        sw_id, port_id = ip.custom_field_data.get('switch'), ip.custom_field_data.get('switch_port')
        sw = Device.objects.filter(pk=sw_id).first() if sw_id else None
        port = Interface.objects.filter(pk=port_id).first() if port_id else None
        if sw and port and sw.name == obs.device and _same_port(port.name, obs.port):
            out['matched'] += 1
        elif not port and (not sw or sw.name == obs.device) and fill_empty:
            # 포트가 비어 있고 (스위치도 비었거나 같은 스위치) → 관측값으로 채움
            dev = Device.objects.filter(name=obs.device).first()
            itf = dev and (Interface.objects.filter(device=dev, name=obs.port).first()
                           or next((i for i in dev.interfaces.all() if _same_port(i.name, obs.port)), None)
                           or Interface.objects.create(device=dev, name=re.sub(r'\.0$', '', obs.port), type='1000base-t'))
            if not dev:
                out['not_seen'] += 1  # 수집 장비 이름이 NetBox Device 이름과 다름
            elif itf:
                ip.snapshot()
                ip.custom_field_data.update({'switch': dev.pk, 'switch_port': itf.pk})
                ip.save()
                out['filled'] += 1
        else:
            reg = f"{sw.name if sw else '-'} {port.name if port else '-'}"
            out['mismatch'] += _flag('port_mismatch', _host(ip), f'대장 {reg} ≠ 관측 {obs.device} {obs.port} (MAC {mac})')
    return out


# --------------------------------------------------------------------- 자동 발견 등록
def register_discovered(days=1, prefix_len=None, now=None):
    """최근 N일 ARP에 보였지만 대장(IP 주소)에 없는 IP를 '자동 발견' 태그를 붙여 등록한다.
    - 들어갈 대역(프리픽스)이 없으면 /prefix_len(기본 설정 discovered_prefix_len=24)으로 만든다
    - MAC·마지막 관측 시각을 채우고, 스위치·포트는 이후 check_ports()가 MAC 테이블로 채운다
    - 해당 IP의 '대장 없는 사용' 불일치는 해결 처리
    반환: {'created', 'prefixes', 'skipped'}"""
    import ipaddress as _ip
    from extras.models import Tag
    from .room_import import ensure_custom_fields
    now = now or timezone.now()
    prefix_len = prefix_len or cfg('discovered_prefix_len') or 24
    out = {'created': 0, 'prefixes': 0, 'skipped': 0}
    latest = {}
    for e in ArpEntry.objects.filter(last_seen__gte=now - dt.timedelta(days=days)).order_by('ip', '-last_seen'):
        latest.setdefault(e.ip, e)
    if not latest:
        return out
    ensure_custom_fields()
    tag, _ = Tag.objects.get_or_create(slug='ipam-discovered', defaults={
        'name': 'IPAM 자동 발견', 'color': 'ff9800', 'description': '대장에 없던 IP를 ARP 수집으로 자동 등록'})

    def work():
        have = {_host(o) for o in IPAddress.objects.extra(where=['host(address)::inet = ANY(%s::inet[])'],
                                                        params=[list(latest)])}
        nets = sorted(((_ip.ip_network(str(p.prefix)), p) for p in Prefix.objects.filter(vrf__isnull=True)
                       if p.prefix.version == 4), key=lambda t: -t[0].prefixlen)
        cache, done = {}, []

        def find_prefix(a):
            key = _ip.ip_network(f'{a}/{prefix_len}', strict=False)
            if key not in cache:   # 같은 /24 안의 IP는 한 번만 찾는다
                cache[key] = next((p for n, p in nets if a in n and n.prefixlen <= prefix_len), None)
            hit = cache[key]
            if hit is None or _ip.ip_network(str(hit.prefix)).prefixlen > prefix_len:
                return next((p for n, p in nets if a in n), None)
            # 더 작은(세분된) 대역이 있으면 그쪽 우선
            return next((p for n, p in nets if a in n and n.prefixlen > prefix_len), None) or hit

        for ip, e in latest.items():
            a = _ip.ip_address(ip)
            if ip in have or a.version != 4 or a.is_loopback or a.is_link_local or a.is_multicast or a.is_unspecified:
                continue
            pfx = find_prefix(a)
            if pfx is None:
                net = _ip.ip_network(f'{ip}/{prefix_len}', strict=False)
                pfx = Prefix(prefix=str(net), status='active', description='자동 발견(ARP)')
                pfx.full_clean(); pfx.save()
                nets.append((net, pfx)); nets.sort(key=lambda t: -t[0].prefixlen); cache.clear()
                out['prefixes'] += 1
            net = pfx.prefix
            if net.prefixlen < 31 and ip in (str(net.network), str(net.broadcast)):
                out['skipped'] += 1
                continue
            o = IPAddress(address=f'{ip}/{net.prefixlen}', status='active',
                          comments=f'SNMP 자동 발견: {e.device} {e.interface} (처음 관측 {timezone.localtime(e.first_seen):%Y-%m-%d %H:%M})')
            o.custom_field_data.update({'host_mac': e.mac, 'last_seen': e.last_seen.isoformat(timespec='seconds')})
            try:
                o.full_clean()   # 검사만 먼저 (행마다 savepoint 를 쓰면 NetBox 검색색인 작업이 행 수만큼 쌓임)
            except ValidationError:
                out['skipped'] += 1
                continue
            o.save(); o.tags.add(tag)
            done.append(ip)
            out['created'] += 1
        for i in range(0, len(done), 2000):
            Discrepancy.objects.filter(kind='unregistered_use', ip__in=done[i:i + 2000], resolved=False).update(resolved=True)

    if connection.in_atomic_block:   # 스크립트(트랜잭션 안) → 트랜잭션 단위 잠금
        with connection.cursor() as cur:
            cur.execute('SELECT pg_advisory_xact_lock(%s)', [IP_LOCK])
        work()
    else:                            # 백그라운드 작업 → 세션 잠금을 트랜잭션 바깥에서
        with advisory_lock(IP_LOCK):
            with transaction.atomic():
                work()
    return out
