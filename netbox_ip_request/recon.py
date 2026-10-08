"""엑셀 대장 ↔ 실제 장비(ARP·MAC 테이블·인터페이스) 대사

IP 주소마다 자동 판정(대사 결과)을 사용자 정의 필드에 적는다 — 변경 이력 없이(5분마다 바뀔 수 있는 값이라).
  일치             : ARP 로 본 MAC 과 위치(스위치·포트)가 대장과 같음
  MAC 불일치       : 대장의 MAC ≠ ARP 로 본 MAC
  위치(포트) 불일치 : 대장의 스위치·포트 ≠ MAC 테이블에서 본 위치
  미관측           : 최근 N일(기본 30일) ARP 에 안 보임
  대장 없음(자동 발견): 엑셀 대장에 없던 IP (수집으로 자동 등록됨)
  IP 충돌          : 같은 IP 를 여러 MAC 이 씀
관측 MAC·관측 위치(스위치 포트 + 링크 상태 + 포트 설명 + VLAN)도 함께 적어 화면에서 대장 값과 나란히 비교한다.
엑셀은 최초 데이터일 뿐이고 실제 L2 가 기준이다(l2_overwrite=True, 기본):
  최근 1일 ARP 로 본 MAC 하나 + 액세스 스위치 MAC 테이블에서 본 포트가 있으면 대장의 MAC·스위치·포트를 그 값으로 덮어쓴다.
  덮어쓴 행은 '데이터 기준' = L2로 덮어씀, 'L2 덮어쓴 내용' = 날짜: 이전→새 값, '엑셀 원본값' = 처음 덮어쓰기 전 엑셀 값.
  '관리자 판정'이 예외(무시)인 IP 와 IP 충돌은 덮어쓰지 않는다.
관리자는 '관리자 판정' 칸(정상 확인/대장 수정 필요/회수 대상/삭제 대상/예외)을 IP 목록의 '선택 항목 편집'으로 한꺼번에 정한다.
"""
import datetime as dt
import json

from django.db import connection
from django.db.models import Count
from django.utils import timezone

from .logic import _same_port, cfg
from .models import ArpEntry, MacEntry


def _edge_entries(since):
    """MAC → 가장 최근 '사용자 포트' 관측 (업링크: 같은 회차에 MAC 4개 이상 본 포트는 제외)"""
    counts = {(r['device'], r['port'], r['last_seen']): r['n'] for r in
              MacEntry.objects.filter(last_seen__gte=since).values('device', 'port', 'last_seen').annotate(n=Count('id'))}
    best = {}
    for e in MacEntry.objects.filter(last_seen__gte=since).order_by('last_seen'):
        if counts.get((e.device, e.port, e.last_seen), 0) <= 3:
            best[e.mac] = e          # 오래된 것부터 덮어써서 최신이 남음
    return best


def reconcile(days=30, now=None):
    from dcim.models import Device, Interface
    from ipam.models import IPAddress
    now = now or timezone.now()
    since = now - dt.timedelta(days=days)
    arp = {}
    for e in ArpEntry.objects.filter(last_seen__gte=since).order_by('last_seen'):
        arp.setdefault(e.ip, {})[e.mac] = e
    edge = _edge_entries(now - dt.timedelta(days=1))
    # 관측 포트의 링크 상태·설명: (장치 이름, 포트) → Interface
    names = {e.device for e in edge.values()}
    itfs = {}
    for i in Interface.objects.filter(device__name__in=names).select_related('device'):
        itfs.setdefault(i.device.name, []).append(i)
    dev_names = dict(Device.objects.values_list('pk', 'name'))
    port_names = {}
    overwrite = cfg('l2_overwrite')
    overwrite = True if overwrite is None else overwrite
    fresh = now - dt.timedelta(days=1)
    today = timezone.localdate(now).isoformat()

    def iface(dev, port):
        for i in itfs.get(dev, []):
            if i.name == port or i.name == port.rsplit('.', 1)[0] or _same_port(i.name, port):
                return i
        return None

    out, counts = {}, {}
    rows = IPAddress.objects.exclude(status='deprecated').values('pk', 'address', 'custom_field_data', 'tags__slug',
                                                                  'assigned_object_id')
    seen_pk = {}
    for r in rows:   # 태그가 여러 개면 행이 여러 번 나오므로 합침
        x = seen_pk.setdefault(r['pk'], {'address': r['address'], 'cf': r['custom_field_data'] or {}, 'tags': set(),
                                         'own': r['assigned_object_id'] is not None})
        if r['tags__slug']:
            x['tags'].add(r['tags__slug'])
    sw_port_ids = {x['cf'].get('switch_port') for x in seen_pk.values() if x['cf'].get('switch_port')}
    port_names = dict(Interface.objects.filter(pk__in=sw_port_ids).values_list('pk', 'name'))
    for pk, x in seen_pk.items():
        cf, host = x['cf'], str(x['address'].ip)
        if x['own']:   # 장비 자신의 주소(관리 IP·게이트웨이)는 대사 대상 아님
            if cf.get('recon_state') or cf.get('obs_mac'):
                out[pk] = {'recon_state': None, 'obs_mac': None, 'obs_location': None}
            continue
        macs = arp.get(host, {})
        obs_mac = ''
        loc = ''
        if len(macs) > 1:
            state = 'conflict'
            obs_mac = ', '.join(sorted(macs))
        elif not macs:
            state = 'unseen'
        else:
            obs_mac = next(iter(macs))
            e = edge.get(obs_mac)
            state = 'ok'
            if cf.get('host_mac') and cf['host_mac'] != obs_mac:
                state = 'mac_diff'
            if e:
                i = iface(e.device, e.port)
                loc = f"{e.device} {e.port}"
                if i is not None:
                    st = (i.custom_field_data or {}).get('oper_status')
                    loc += f" [{st or '?'}{'' if i.enabled else ', 관리 down'}]" + (f" {i.description}" if i.description else '')
                loc += f" VLAN {e.vlan}" if e.vlan else ''
                reg_sw, reg_port = cf.get('switch'), cf.get('switch_port')
                if state == 'ok' and reg_sw and dev_names.get(reg_sw) and reg_port:
                    if not (dev_names[reg_sw] == e.device and _same_port(port_names.get(reg_port, ''), e.port)):
                        state = 'port_diff'
            else:
                loc = f"(ARP: {macs[obs_mac].device} {macs[obs_mac].interface}, 스위치 포트 미확인)"
        discovered = 'ipam-discovered' in x['tags']
        if discovered and state in ('ok', 'unseen'):
            state = 'discovered'
        new = {'recon_state': state, 'obs_mac': obs_mac or None, 'obs_location': loc[:250] or None}
        # ---- 실제 L2 를 기준으로 대장 덮어쓰기 (MAC·스위치·포트)
        #      덮어쓰기를 끈 판정 기간(l2_overwrite=0)에도 '빈칸'은 실제 값으로 채운다 (엑셀 값은 그대로 둠)
        src = cf.get('data_source') or ('l2_new' if discovered else 'excel')
        if (obs_mac and state != 'conflict' and cf.get('review') != 'ignore'
                and macs[obs_mac].last_seen >= fresh):
            chg, upd = [], {}
            if cf.get('host_mac') != obs_mac and (overwrite or not cf.get('host_mac')):
                chg.append(f"MAC {cf.get('host_mac') or '(빈칸)'}→{obs_mac}")
                upd['host_mac'] = obs_mac
            i = iface(e.device, e.port) if e else None
            if i is not None and not overwrite and (cf.get('switch') or cf.get('switch_port')):
                i = None                           # 판정 기간: 엑셀에 스위치·포트가 있으면 건드리지 않음
            if i is not None:
                old_sw, old_port = cf.get('switch'), port_names.get(cf.get('switch_port'), '')
                if old_sw != i.device_id:
                    chg.append(f"스위치 {dev_names.get(old_sw) or '(빈칸)'}→{e.device}")
                    upd['switch'] = i.device_id
                if cf.get('switch_port') != i.pk:
                    if old_sw != i.device_id or not (old_port and _same_port(old_port, i.name)):
                        chg.append(f"포트 {old_port or '(빈칸)'}→{i.name}")
                    upd['switch_port'] = i.pk          # 같은 포트의 다른 이름(엑셀 '1/0/5' ↔ 'Gi1/0/5')은 실제 포트로만 연결
            if chg:
                if not discovered and not cf.get('excel_orig') and src == 'excel':
                    upd['excel_orig'] = (f"MAC {cf.get('host_mac') or '-'} · 스위치 {dev_names.get(cf.get('switch')) or '-'}"
                                         f" · 포트 {port_names.get(cf.get('switch_port')) or '-'}")[:250]
                upd['l2_changed'] = f"{today}: " + ' · '.join(chg)
                src = 'l2_new' if discovered else 'l2_overwritten'
                counts['overwritten' if overwrite else 'filled'] = counts.get('overwritten' if overwrite else 'filled', 0) + 1
                if state in ('mac_diff', 'port_diff'):
                    state = new['recon_state'] = 'ok'      # 덮어써서 이제 대장 = 실제
            elif src == 'excel' and overwrite:
                src = 'l2_same'
            new.update(upd)
        new['data_source'] = src
        if any(cf.get(k) != v for k, v in new.items()):
            out[pk] = new
        counts[state] = counts.get(state, 0) + 1
    items = list(out.items())
    with connection.cursor() as cur:
        for i in range(0, len(items), 1000):
            chunk = items[i:i + 1000]
            cur.execute("UPDATE ipam_ipaddress AS t SET custom_field_data = t.custom_field_data || v.j::jsonb "
                        f"FROM (VALUES {','.join(['(%s, %s)'] * len(chunk))}) AS v(id, j) WHERE t.id = v.id",
                        [y for pk, d in chunk for y in (pk, json.dumps(d, ensure_ascii=False))])
    counts['changed'] = len(items)
    return counts


def apply_review(kinds, log=print, user=None):
    """관리자 판정 반영. kinds: {'fix_ledger','reclaim','delete'} 중 처리할 것.
    - fix_ledger : 관측 MAC·위치를 대장(MAC ADDRESS·스위치·포트)에 반영 → 판정을 '정상 확인'으로
    - reclaim    : 미사용(dormant) 처리 후 회수(격리) → 판정 비움
    - delete     : IP 삭제
    반환 건수 dict"""
    from dcim.models import Device, Interface
    from ipam.models import IPAddress
    from . import logic
    res = {'fix_ledger': 0, 'reclaim': 0, 'delete': 0, 'skipped': 0}
    for o in IPAddress.objects.filter(custom_field_data__review__in=list(kinds)):
        cf = o.custom_field_data
        kind = cf.get('review')
        if kind == 'delete':
            log(f'{o} 삭제')
            o.snapshot(); o.delete(); res['delete'] += 1
            continue
        if kind == 'fix_ledger':
            o.snapshot()
            if cf.get('obs_mac') and ',' not in cf['obs_mac']:
                cf['host_mac'] = cf['obs_mac']
            m = (cf.get('obs_location') or '').split(' ')
            if len(m) >= 2 and not m[0].startswith('('):
                d = Device.objects.filter(name=m[0]).first()
                i = d and (Interface.objects.filter(device=d, name=m[1]).first()
                           or Interface.objects.filter(device=d, name=m[1].rsplit('.', 1)[0]).first())
                if d:
                    cf['switch'] = d.pk
                if i:
                    cf['switch_port'] = i.pk
            cf['review'] = 'confirmed'
            cf['review_note'] = ((cf.get('review_note') or '') + ' [관측값 반영]').strip()[:200]
            o.save(); res['fix_ledger'] += 1
            log(f'{o} 대장 수정: MAC {cf.get("host_mac")} 위치 {cf.get("obs_location")}')
            continue
        if kind == 'reclaim':
            if o.status == 'quarantine':
                res['skipped'] += 1
                continue
            o.snapshot()
            if o.status != 'dormant':
                o.status = 'dormant'
            cf['review'] = None
            o.save()
            try:
                logic.reclaim(o.pk)
                res['reclaim'] += 1
                log(f'{o} 회수(격리)')
            except Exception as e:
                res['skipped'] += 1
                log(f'{o} 회수 실패: {e}')
    return res
