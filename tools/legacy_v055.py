"""0.5.5 의 포트 대조·ARP 적재(하나씩 조회) — 0.6.3 일괄 처리판과 결과가 같은지 비교하는 기준 (verify_perf.py 전용)"""
import re

from django.db import connection, transaction
from django.utils import timezone

from ipam.models import IPAddress, Prefix
from netbox_ip_request.logic import _host, _same_port, norm_mac
from netbox_ip_request.models import ArpEntry, Discrepancy, MacEntry


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


def _edge_port(e):
    """사용자 포트인지(업링크/LAG 아님). 같은 수집 회차(last_seen 동일)에 그 포트에서 본 MAC이 3개 이하면 사용자 포트.
    과거 회차까지 세면 사용자가 자주 바뀌는 포트가 업링크로 오판되므로 회차 단위로 센다."""
    return MacEntry.objects.filter(device=e.device, port=e.port, last_seen=e.last_seen).count() <= 3


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
