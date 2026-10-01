"""관리 IP 하나로 장비 등록·동기화 (SNMP 기반)

관리자가 넣는 것은 장비의 관리 IP 뿐이다. 나머지는 SNMP 로 읽어 NetBox 에 만든다/맞춘다.
  - 장치(Device): 이름=sysName, 제조사·모델·시리얼(ENTITY-MIB), 플랫폼(벤더), 역할(L3/L2 자동 판별), 수집 태그
  - 인터페이스: 이름·종류·속도·관리상태(enabled)·설명(ifAlias)·링크 상태(사용자 정의 필드 oper_status)
  - 관리 IP: 해당 인터페이스(예: Vlan1, vlan99, me0)에 할당 + 기본 IPv4 지정
  - L3 인터페이스(SVI) 주소: 대역(프리픽스) 생성 + VLAN 연결 + 게이트웨이 IP 등록
  - VLAN: VLAN 번호·이름 (사이트별 VLAN 그룹 안에)
  - 엑셀로 만든 포트 번호 인터페이스('9')는 실제 포트(Gi1/0/9)로 옮기고 정리
"""
import ipaddress
import re

from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.utils.text import slugify

from .logic import _port_key

JUNK = re.compile(r'^(lo\d|null|nu0|pfe-|pfh-|jsrv|bme\d|cbp\d|dsc$|esi$|fti\d|gre$|ipip$|lsi$|mtun$|pim[de]$|pip\d|pp\d|'
                  r'rbeb$|tap$|vtep$|vme$|em\d+\.\d+|fxp\d+\.\d+|demux|gr-|ip-|lt-|mt-|pd-|pe-|sp-|ut-|vt-|'
                  r'inactive|CPU|StackPort|Bluetooth|Ap\d|InLoopBack|Register-Tunnel)', re.I)
SVI = re.compile(r'^(?:vlan|vlanif|irb\.|vlan\.|vl|vlan-interface)\s*(\d{1,4})$', re.I)
VENDOR_PLATFORM = {9: ('Cisco IOS', 'cisco_ios'), 2636: ('Juniper Junos', 'juniper_junos'),
                   11: ('HP ProCurve', 'hp_procurve'), 25506: ('HPE Comware', 'hp_comware'),
                   47196: ('Aruba AOS-CX', 'aruba_aoscx'), 14823: ('Aruba AOS', 'aruba_os')}


def _iftype(t, speed, name):
    if t == 161 or re.match(r'^(port-channel|po\d|ae\d|bridge-aggregation|trk\d|lag)', name, re.I):
        return 'lag'
    if t in (6, 62, 69, 117):
        return {10: '10base-t', 100: '100base-tx', 1000: '1000base-t', 2500: '2.5gbase-t', 10000: '10gbase-x-sfpp',
                25000: '25gbase-x-sfp28', 40000: '40gbase-x-qsfpp', 100000: '100gbase-x-qsfp28'}.get(speed, '1000base-t')
    return 'virtual'


def _vid(name):
    m = SVI.match(name.strip())
    return int(m.group(1)) if m and 1 <= int(m.group(1)) <= 4094 else None


def _slug(text, prefix=''):
    s = slugify(text) or ''
    if not s:
        s = prefix + '-' + text.encode().hex()[:40]
    return s[:100]


def _model_from_descr(descr, vendor):
    for pat in (r'Cisco IOS Software, (\S+) Software', r'\b(C9\d{3}[A-Z0-9-]*|WS-C\S+|C\d{4}[A-Z0-9-]*)',
                r'Juniper Networks, Inc\. (\S+)', r'\b(J\d{4}[A-Z])\b', r'Aruba (\S+ \S+) Switch', r'\b(\d{4}[A-Z]?-\S+)'):
        m = re.search(pat, descr or '')
        if m:
            return m.group(1).strip(' ,')
    return f'{vendor} 장비'


def _set_cf_raw(table, values):
    """values: {pk: {키: 값}} — 변경 이력 없이 사용자 정의 필드 일부만 갱신 (링크 상태처럼 자주 바뀌는 값용)"""
    import json
    items = list(values.items())
    with connection.cursor() as cur:
        for i in range(0, len(items), 1000):
            chunk = items[i:i + 1000]
            cur.execute(
                f"UPDATE {table} AS t SET custom_field_data = t.custom_field_data || v.j::jsonb "
                f"FROM (VALUES {','.join(['(%s, %s)'] * len(chunk))}) AS v(id, j) WHERE t.id = v.id",
                [x for pk, d in chunk for x in (pk, json.dumps(d, ensure_ascii=False))])


def ensure_base(site):
    from dcim.models import DeviceRole
    from ipam.models import VLANGroup
    from core.models import ObjectType
    acc, _ = DeviceRole.objects.get_or_create(slug='access-switch', defaults={'name': 'Access Switch', 'color': '2196f3'})
    l3, _ = DeviceRole.objects.get_or_create(slug='core-l3', defaults={'name': 'L3 Switch', 'color': '9c27b0'})
    grp = VLANGroup.objects.filter(scope_type=ObjectType.objects.get_for_model(site), scope_id=site.pk).first()
    if grp is None:
        grp = VLANGroup(name=f'{site.name} VLAN'[:100], slug=_slug(f'{site.slug}-vlan', 'vg'), scope=site)
        grp.full_clean(); grp.save()
    return acc, l3, grp


def find_device(ip, sysname=''):
    from dcim.models import Device
    from ipam.models import IPAddress
    d = Device.objects.filter(primary_ip4__address__net_host=ip).first()
    if d:
        return d
    a = IPAddress.objects.filter(address__net_host=ip, assigned_object_type__model='interface').first()
    if a and a.assigned_object:
        return a.assigned_object.device
    return Device.objects.filter(name=sysname).first() if sysname else None


def sync_device(res, site, role_hint='auto', rename=False, log=print):
    """res: snmp.poll(..., what=('arp','mac','inv')) 의 장비 1대 결과 → NetBox 반영. 반환 (device, 요약 dict)"""
    from dcim.models import Device, DeviceType, Interface, Manufacturer, Platform
    from extras.models import Tag
    from ipam.models import IPAddress, Prefix, VLAN
    ip, sy, inv = res['host'], res['system'], res['inv']
    acc, l3, grp = ensure_base(site)
    out = {'created': False, 'renamed': False, 'if_new': 0, 'if_upd': 0, 'vlans': 0, 'prefixes': 0, 'gateways': 0,
           'moved_ports': 0}

    vendor = sy['vendor'].split('(')[0].strip() if sy.get('enterprise') else 'Generic'
    mf, _ = Manufacturer.objects.get_or_create(slug=_slug(vendor, 'mf'), defaults={'name': vendor[:100]})
    model = (inv.get('model') or _model_from_descr(sy.get('descr'), vendor))[:100]
    dt = DeviceType.objects.filter(manufacturer=mf, model=model).first()
    if dt is None:
        slug = _slug(model, 'dt')
        if DeviceType.objects.filter(manufacturer=mf, slug=slug).exists():
            slug = (slug + '-' + str(abs(hash(model)) % 10000))[:100]
        dt = DeviceType(manufacturer=mf, model=model, slug=slug); dt.full_clean(); dt.save()
    platform = None
    if sy.get('enterprise') in VENDOR_PLATFORM:
        pname, pslug = VENDOR_PLATFORM[sy['enterprise']]
        platform, _ = Platform.objects.get_or_create(slug=pslug.replace('_', '-'), defaults={'name': pname, 'manufacturer': mf})

    is_l3 = len(res.get('arp') or []) > 0 and len([a for a in inv['addrs'] if not a[0].startswith('127.')]) > 1
    role = {'l3': l3, 'l2': acc}.get(role_hint) or (l3 if is_l3 else acc)
    sysname = (sy.get('name') or '').strip()[:64]

    dev = find_device(ip, sysname)
    if dev is None:
        name = sysname if sysname and not Device.objects.filter(name=sysname, site=site).exists() else f'SW-{ip}'
        dev = Device(name=name, site=site, device_type=dt, role=role, status='active', platform=platform,
                     serial=(inv.get('serial') or '')[:50], description=sysname[:200])
        dev.full_clean(); dev.save()
        out['created'] = True
    else:
        dev.snapshot()
        changed = False
        if rename and sysname and dev.name != sysname and not Device.objects.filter(name=sysname, site=dev.site).exclude(pk=dev.pk).exists():
            dev.name = sysname; changed = out['renamed'] = True
        for f, v in (('device_type', dt), ('platform', platform or dev.platform), ('serial', (inv.get('serial') or dev.serial)[:50]),
                     ('description', dev.description or sysname[:200])):
            if getattr(dev, f) != v:
                setattr(dev, f, v); changed = True
        if role_hint in ('l2', 'l3') and dev.role != role:
            dev.role = role; changed = True
        if changed:
            dev.full_clean(); dev.save()
    tags = []
    if res.get('arp'):
        tags.append('ipam-arp')
    if res.get('mac'):
        tags.append('ipam-mac')
    for t in Tag.objects.filter(slug__in=tags):
        dev.tags.add(t)

    # ---- 인터페이스
    have = {i.name: i for i in Interface.objects.filter(device=dev)}
    by_index, oper = {}, {}
    for ifi, x in sorted(inv['interfaces'].items()):
        name = x['name'].strip()[:64]
        if not name or JUNK.match(name) or ('.' in name and not _vid(name) and not name.lower().startswith(('vlan', 'irb'))):
            continue
        itf = have.get(name)
        typ = _iftype(x['type'], x['speed'], name)
        if itf is None:
            itf = Interface(device=dev, name=name, type=typ, enabled=x['admin'], description=x['alias'][:200])
            try:
                itf.full_clean()
            except ValidationError:
                continue
            itf.save(); have[name] = itf; out['if_new'] += 1
        elif itf.enabled != x['admin'] or (x['alias'] and itf.description != x['alias'][:200]):
            itf.snapshot(); itf.enabled = x['admin']
            if x['alias']:
                itf.description = x['alias'][:200]
            itf.save(); out['if_upd'] += 1
        by_index[ifi] = itf
        oper[itf.pk] = {'oper_status': 'up' if x['oper'] else 'down'}
    _set_cf_raw('dcim_interface', oper)

    # 엑셀에서 만든 '9' 같은 포트 번호 인터페이스 → 실제 포트(Gi1/0/9)로 옮기기 (같은 번호가 하나뿐일 때만)
    real = [i for i in have.values() if not i.name.isdigit() and i.type != 'virtual']
    for num in [i for i in have.values() if i.name.isdigit()]:
        cands = [r for r in real if _port_key(r.name) == num.name]
        if len(cands) != 1:
            continue
        for a in IPAddress.objects.filter(custom_field_data__switch_port=num.pk):
            a.snapshot(); a.custom_field_data['switch_port'] = cands[0].pk; a.save()
        num.delete(); out['moved_ports'] += 1

    # ---- VLAN
    vlan_obj = {v.vid: v for v in VLAN.objects.filter(group=grp)}
    for vid, vname in sorted(inv['vlans'].items()):
        vname = (vname or '').strip()[:64] or f'VLAN{vid}'
        v = vlan_obj.get(vid)
        if v is None:
            if VLAN.objects.filter(group=grp, name=vname).exists():
                vname = f'{vname} ({vid})'[:64]
            v = VLAN(group=grp, vid=vid, name=vname, status='active')
            try:
                v.full_clean()
            except ValidationError:
                continue
            v.save(); vlan_obj[vid] = v; out['vlans'] += 1
        elif re.fullmatch(r'VLAN0*\d+', v.name, re.I) and not re.fullmatch(r'VLAN0*\d+', vname, re.I) \
                and not VLAN.objects.filter(group=grp, name=vname).exclude(pk=v.pk).exists():
            v.snapshot(); v.name = vname; v.save()

    # ---- IP 주소: 관리 IP + SVI 대역·게이트웨이
    mgmt = None
    for addr, mask, ifi in inv['addrs']:
        a = ipaddress.ip_address(addr)
        if a.is_loopback or a.is_link_local:
            if addr != ip:
                continue
        net = ipaddress.ip_network(f'{addr}/{mask}', strict=False)
        itf = by_index.get(ifi)
        vid = _vid(itf.name) if itf else None
        if not a.is_loopback and net.prefixlen <= 30:
            pfx = Prefix.objects.filter(prefix=str(net), vrf__isnull=True).first()
            if pfx is None:
                pfx = Prefix(prefix=str(net), status='active', scope=site, description=f'{dev.name} {itf.name if itf else ""}'.strip()[:200])
                pfx.full_clean(); pfx.save(); out['prefixes'] += 1
            if vid and pfx.vlan_id is None and vid in vlan_obj:
                pfx.snapshot(); pfx.vlan = vlan_obj[vid]; pfx.save()
            elif vid and pfx.vlan_id is None:
                v = VLAN(group=grp, vid=vid, name=f'VLAN{vid}', status='active'); v.full_clean(); v.save()
                vlan_obj[vid] = v; out['vlans'] += 1
                pfx.snapshot(); pfx.vlan = v; pfx.save()
        o = IPAddress.objects.filter(address__net_host=addr, vrf__isnull=True).first()
        if o is None:
            o = IPAddress(address=f'{addr}/{net.prefixlen}', status='active',
                          description='장비 관리 IP' if addr == ip else f'게이트웨이 ({dev.name} {itf.name if itf else ""})'[:200])
            if itf:
                o.assigned_object = itf
            try:
                o.full_clean()
            except ValidationError:
                continue
            o.save()
            if addr != ip:
                out['gateways'] += 1
        elif itf and o.assigned_object is None:
            o.snapshot(); o.assigned_object = itf; o.save()
        if addr == ip:
            mgmt = o
    if mgmt is None:   # 관리 IP 가 주소 표에 없음(대역 밖 관리망 등) → mgmt 가상 인터페이스에
        mgmt = IPAddress.objects.filter(address__net_host=ip, vrf__isnull=True).first()
        mg = have.get('mgmt') or Interface.objects.create(device=dev, name='mgmt', type='virtual', mgmt_only=True)
        if mgmt is None:
            mgmt = IPAddress(address=f'{ip}/32', status='active', description='장비 관리 IP', assigned_object=mg)
            mgmt.full_clean(); mgmt.save()
        elif mgmt.assigned_object is None:
            mgmt.snapshot(); mgmt.assigned_object = mg; mgmt.save()
    if mgmt.assigned_object and getattr(mgmt.assigned_object, 'device_id', None) == dev.pk and dev.primary_ip4_id != mgmt.pk:
        dev.snapshot(); dev.primary_ip4 = mgmt; dev.save()
    return dev, out


def onboard(ips, site, role_hint='auto', rename=False, collect=True, log=print, ports=None):
    """관리 IP 목록 → 장비 등록·동기화 → (선택) 바로 ARP/MAC 수집·대사. 반환 [(ip, 상태, 설명)]"""
    from . import snmp
    from .room_import import ensure_custom_fields
    ensure_custom_fields()
    snmp.ensure_tags()
    ports = ports or {}
    devs = [{'name': ip, 'host': ip, 'port': ports.get(ip), 'arp': True, 'mac': True} for ip in ips]
    results = snmp.poll(devs, ('arp', 'mac', 'inv'))
    rows, done = [], []
    for r in results:
        if r['error']:
            rows.append((r['host'], '실패', r['error']))
            continue
        try:
            with transaction.atomic():
                dev, o = sync_device(r, site, role_hint, rename, log)
        except Exception as e:  # 장비 1대 실패가 나머지를 막지 않게
            rows.append((r['host'], '실패', f'NetBox 반영 오류: {type(e).__name__}: {e}'[:300]))
            continue
        r['name'] = dev.name   # 수집 결과를 NetBox 장치 이름으로 저장
        done.append(r)
        rows.append((r['host'], '신규' if o['created'] else '갱신',
                     f"{dev.name} · {r['system']['vendor']} {r['inv'].get('model') or ''} · 인터페이스 +{o['if_new']}/수정 {o['if_upd']}"
                     f" · VLAN +{o['vlans']} · 대역 +{o['prefixes']} · 게이트웨이 +{o['gateways']}"
                     f"{' · 엑셀 포트 정리 ' + str(o['moved_ports']) if o['moved_ports'] else ''}"
                     f" · ARP {len(r['arp'])} · MAC {len(r['mac'])}"))
    summary = {}
    if collect and done:
        from .logic import check_ports, register_discovered
        from .recon import reconcile
        from netbox.plugins import get_plugin_config
        summary = snmp.ingest(done)
        if get_plugin_config('netbox_ip_request', 'auto_register_discovered'):
            summary['discovered'] = register_discovered()
            summary['ports'] = check_ports()
        summary['recon'] = reconcile()
    return rows, summary


def sync_all(log=print):
    """기본 IP 가 있는 수집 대상 장치 전체를 다시 읽어 인터페이스·VLAN·대역 갱신 (이름은 바꾸지 않음)"""
    from . import snmp
    targets, _ = snmp.targets()
    from dcim.models import Device
    by_site, ports = {}, {}
    for t in targets:
        d = Device.objects.filter(name=t['name']).first()
        if d:
            by_site.setdefault(d.site, []).append(t['host'])
            if t.get('port'):
                ports[t['host']] = t['port']
    rows = []
    for site, hosts in by_site.items():
        r, _ = onboard(hosts, site, collect=False, log=log, ports=ports)
        rows += r
    return rows
