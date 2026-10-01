"""호실 기준 IP 대장(현행 엑셀 양식) → NetBox, 서버 안에서 직접 반영 (웹 업로드용)

엑셀 규칙은 tools/import_room_excel.py 와 같다.
  - SUBNET ADDRESS = 3번째 옥텟(/24 = VLAN 1개), IP ADDRESS = 4번째 옥텟, 앞 두 옥텟 = base(기본 165.246)
  - IP 000 또는 빈칸 = 미발급 → IP 없이 호실·부서·대역만 등록
  - PIC ID 무시, 스위치IP '49.2' → base.49.2, 호관호실 '1-001A' → 호관 '1' / 호실 '1-001A'
"""
import ipaddress
import re
from collections import Counter

import pandas as pd
import re as _re

from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction

from .logic import IP_LOCK, norm_mac

COLS = {'연번': 'seq', '호관호실': 'room', '학부(과)': 'dept', '호관호실명칭': 'room_name', '용도': 'purpose',
        'SUBNET ADDRESS': 'subnet', 'IP ADDRESS': 'host', 'MAC ADDRESS': 'mac', 'OUTLET NO': 'outlet',
        'RACK 호관호실': 'rack_room', '패치번호': 'patch', '패치포트번호': 'patch_port',
        '스위치IP ADDRESS': 'sw_ip', '스위치 포트 번호': 'sw_port', '호스트 이름': 'hostname', '비고': 'note',
        '관리자': 'manager', '전화번호': 'phone'}  # PIC ID는 사용하지 않음
REQUIRED = ('호관호실', 'SUBNET ADDRESS', 'IP ADDRESS')

CUSTOM_FIELDS = [  # (이름, 라벨(=원본 엑셀 컬럼명), 타입, 참조 모델(app_label, model), 그룹)
    # 라벨은 처음 만들 때만 쓰인다. 이후 이름 변경은 NetBox 화면 > 사용자 정의 > 사용자 정의 필드 에서.
    ('ip_user', '관리자', 'text', None, 'IPAM 운영'),
    ('host_mac', 'MAC ADDRESS', 'text', None, 'IPAM 운영'),
    ('purpose', '용도', 'text', None, 'IPAM 운영'),
    ('room', '호관호실', 'object', ('dcim', 'location'), '위치·배선'),
    ('switch', '스위치IP ADDRESS', 'object', ('dcim', 'device'), '위치·배선'),
    ('switch_port', '스위치 포트 번호', 'object', ('dcim', 'interface'), '위치·배선'),
    ('outlet_no', 'OUTLET NO', 'text', None, '위치·배선'),
    ('patch_panel', '패치번호', 'text', None, '위치·배선'),
    ('patch_port', '패치포트번호', 'text', None, '위치·배선'),
    ('manager_phone', '전화번호', 'text', None, '위치·배선'),
    ('legacy_seq', '연번', 'text', None, '위치·배선'),
    # 운영(수집·미사용 판정·신청)용
    ('assigned_on', '발급일', 'date', None, 'IPAM 운영'),
    ('expires_on', '만료일', 'date', None, 'IPAM 운영'),
    ('last_seen', '마지막 관측', 'datetime', None, 'IPAM 운영'),
    ('dormant_exempt', '미사용 판정 제외', 'boolean', None, 'IPAM 운영'),
    ('quarantine_until', '격리 종료일', 'date', None, 'IPAM 운영'),
]


def s(v):
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return ''
    return str(v).strip()


def octet(v, what):
    t = s(v)
    if not re.fullmatch(r'\d{1,3}', t) or int(t) > 255:
        raise ValueError(f'{what} 형식 오류: {v!r}')
    return int(t)


def fix_mac(v):
    """'00ID09B0DF2A'처럼 숫자 0·1 자리에 영문 O·I·L 이 들어간 MAC 을 보정. 보정해도 안 되면 None"""
    t = str(v or '').strip().upper()
    if not t:
        return None
    t2 = t.translate(str.maketrans({'O': '0', 'I': '1', 'L': '1'}))
    if t2 == t:
        return None
    try:
        return norm_mac(t2)
    except ValueError:
        return None


def validate(fileobj, base='165.246', strict=True):
    """엑셀을 읽어 행을 검사. 반환 {'errors', 'warnings', 'rows', 'blank', 'merged'}

    strict=True : 형식 오류가 있는 행은 errors 로 모은다(스크립트가 전체 취소).
    strict=False: 형식 오류는 경고로 바꾸고 문제 칸만 비운 채 행을 살린다.
                  - SUBNET/IP 오류, .255 → IP 없이(미발급처럼) 호실·소속만 등록, 원래 값은 비고에 남김
                  - MAC 오류 → MAC 칸 비우고 원래 값은 비고에 남김
                  - 같은 IP가 여러 행이면 한 행으로 통합(다른 값은 ' / '로 이어 붙임)
    파일을 못 읽거나 필수 컬럼이 없으면 두 모드 모두 오류."""
    try:
        df = pd.read_excel(fileobj, dtype=str)
    except Exception as e:
        return {'errors': [('-', f'엑셀 파일을 읽을 수 없음: {e}')], 'warnings': [], 'rows': [], 'blank': 0, 'merged': []}
    missing = [c for c in REQUIRED if c not in df.columns]
    if missing:
        return {'errors': [('1', f'필수 컬럼 없음: {", ".join(missing)} — 첫 행이 제목 줄인지 확인')],
                'warnings': [], 'rows': [], 'blank': 0, 'merged': []}
    df = df.rename(columns=COLS)
    df = df[[c for c in COLS.values() if c in df.columns]]
    df = df[df.apply(lambda r: any(s(x) for x in r), axis=1)]
    errors, warnings, rows = [], [], []
    b1, b2 = (int(x) for x in base.split('.'))

    for i, r in df.iterrows():
        line = i + 2
        problems, notes = [], []   # 이 행의 형식 오류, 비고에 남길 원래 값
        net = ip = None
        try:
            c3 = octet(r['subnet'], 'SUBNET')
            net = ipaddress.ip_network(f'{b1}.{b2}.{c3}.0/24')
        except ValueError as e:
            problems.append(str(e)); notes.append(f"원본 SUBNET: {s(r['subnet'])}")
        host = s(r['host'])
        if net and host not in ('', '0', '00', '000'):
            try:
                c4 = octet(host, 'IP')
                if c4 == 255:
                    raise ValueError(f'{b1}.{b2}.{c3}.255: 브로드캐스트 주소')
                ip = ipaddress.ip_address(f'{b1}.{b2}.{c3}.{c4}')
            except ValueError as e:
                problems.append(str(e)); notes.append(f'원본 IP: {host}')
        elif not net and host not in ('', '0', '00', '000'):
            notes.append(f'원본 IP: {host}')
        sw_ip = None
        if s(r.get('sw_ip')):
            raw_sw = s(r['sw_ip'])
            m = re.fullmatch(r'(?:(\d{1,3})\.(\d{1,3})\.)?(\d{1,3})\.(\d{1,3})', raw_sw)
            if m and m.group(1) and (int(m.group(1)), int(m.group(2))) != (b1, b2):
                m = None   # 앞 두 자리가 다른 전체 IP
            if not m or int(m.group(3)) > 255 or int(m.group(4)) > 255:
                warnings.append((line, f'스위치 IP 형식 확인 필요(스위치 연결 없이 반영): {raw_sw!r}'))
            else:   # '037.4' 처럼 앞에 0이 붙어도 숫자로 바꿔 165.246.37.4 로
                sw_ip = f'{b1}.{b2}.{int(m.group(3))}.{int(m.group(4))}'
        mac = None
        try:
            mac = norm_mac(s(r.get('mac')))
        except ValueError as e:
            fixed = fix_mac(s(r.get('mac')))
            if fixed:   # 영문 O·I·L 을 숫자 0·1 로 잘못 친 경우 자동 보정 (원본은 비고에 남김)
                mac = fixed
                warnings.append((line, f"[자동 보정] MAC {s(r.get('mac'))} → {fixed}"))
                notes.append(f"원본 MAC: {s(r.get('mac'))}")
            else:
                problems.append(str(e)); notes.append(f"원본 MAC: {s(r.get('mac'))}")
        if problems:
            if strict:
                for pmsg in problems:
                    errors.append((line, pmsg))
                continue
            warnings.append((line, '[무시하고 반영] ' + ' · '.join(problems)))
        room = s(r['room'])
        if not room:
            warnings.append((line, '호관호실이 비어 있음 — 호실 연결 없이 반영'))
        elif not re.fullmatch(r'[0-9A-Za-z]+-[0-9A-Za-z]+', room):
            warnings.append((line, f'호관호실 형식 확인: {room!r}'))
        if ip and sw_ip and not s(r.get('sw_port')):
            warnings.append((line, f'{ip}: 스위치({sw_ip})는 있는데 포트 번호가 없음'))
        note = ' / '.join([x for x in [s(r.get('note'))] + notes if x])
        rows.append(dict(line=line, ip=ip, net=net, mac=mac, room=room, building=room.split('-')[0] if room else '',
                         rooms=[room] if room else [],
                         room_name=s(r.get('room_name')), dept=s(r.get('dept')), purpose=s(r.get('purpose')),
                         sw_ip=sw_ip, sw_port=s(r.get('sw_port')), hostname=s(r.get('hostname')),
                         outlet=s(r.get('outlet')), patch=s(r.get('patch')), patch_port=s(r.get('patch_port')),
                         rack_room=s(r.get('rack_room')), manager=s(r.get('manager')),
                         phone=s(r.get('phone')), note=note, seq=s(r.get('seq'))))

    dups = [d for d, n in Counter(str(x['ip']) for x in rows if x['ip']).items() if n > 1]
    merged = []
    if dups and strict:
        for d in dups:
            errors.append(('-', f'파일 내 IP 중복: {d}'))
    elif dups:
        rows, merged = merge_duplicates(rows)
        for m in merged:
            warnings.append((m['line'], f"[통합] {m['ip']}: {m['line']}행을 한 행으로 합침"))
    return {'errors': errors, 'warnings': warnings, 'rows': rows, 'blank': sum(1 for x in rows if not x['ip']),
            'merged': merged}


JOIN_FIELDS = ('room_name', 'purpose', 'hostname', 'outlet', 'patch', 'patch_port', 'manager', 'phone', 'note', 'seq')


def _join(values):
    out = []
    for v in values:
        for part in str(v).split(' / '):
            if part and part not in out:
                out.append(part)
    return ' / '.join(out)


def merge_duplicates(rows):
    """같은 IP의 행들을 첫 행 기준으로 합친다. 반환 (합친 뒤 rows, 합친 행 목록)"""
    groups, order = {}, []
    for x in rows:
        key = str(x['ip']) if x['ip'] else f"blank-{x['line']}"
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(x)
    out, merged = [], []
    for key in order:
        g = groups[key]
        if len(g) == 1:
            out.append(g[0]); continue
        m = dict(g[0])
        m['line'] = ','.join(str(x['line']) for x in g)
        m['rooms'] = []
        for x in g:
            for rm in x['rooms']:
                if rm not in m['rooms']:
                    m['rooms'].append(rm)
        for f in JOIN_FIELDS:
            m[f] = _join([x[f] for x in g if x[f]])
        extra = []
        for f, label in (('dept', '소속'), ('mac', 'MAC'), ('sw_ip', '스위치'), ('sw_port', '포트'), ('room', '호실')):
            vals = []
            for x in g:
                if x[f] and x[f] not in vals:
                    vals.append(x[f])
            if vals:
                m[f] = vals[0]           # 한 값만 가질 수 있는 칸은 첫 값
                if len(vals) > 1:
                    extra.append(f"{label} 추가값: {', '.join(str(v) for v in vals[1:])}")
        if m['room']:
            m['building'] = m['room'].split('-')[0]
        m['note'] = _join([m['note']] + extra)
        out.append(m)
        merged.append(m)
    return out, merged


def _slug(prefix, text):
    return (prefix + '-' + text.encode().hex())[:100]


def ensure_custom_fields():
    from core.models import ObjectType
    from extras.models import CustomField
    from ipam.models import IPAddress
    ip_type = ObjectType.objects.get_for_model(IPAddress)
    for name, label, typ, rel, group in CUSTOM_FIELDS:
        cf = CustomField.objects.filter(name=name).first()
        if not cf:
            cf = CustomField(name=name, label=label, type=typ, group_name=group)
            if typ == 'boolean':
                cf.default = False
            if rel:
                cf.related_object_type = ObjectType.objects.get_by_natural_key(*rel)
            cf.full_clean(exclude=['object_types'])
            cf.save()
        if not cf.object_types.filter(pk=ip_type.pk).exists():
            cf.object_types.add(ip_type)


_DNS = _re.compile(r'^([0-9A-Za-z_-]+|\*)(\.[0-9A-Za-z_-]+)*\.?$')

EXISTING_MODES = (('skip', '건너뜀 (새 IP만 추가)'), ('merge', '합치기 (빈 칸 채우고 다른 값은 이어 붙임)'),
                  ('overwrite', '덮어쓰기 (엑셀 값으로 교체)'))
_TEXT_CF = ('ip_user', 'purpose', 'outlet_no', 'patch_panel', 'patch_port', 'manager_phone', 'legacy_seq')
_FILL_CF = ('room', 'switch', 'switch_port', 'host_mac')


def _merge_into(o, tenant, dns_name, desc, note, cf):
    """기존 IP에 엑셀 값을 합친다: 빈 칸은 채우고, 글자 칸은 다른 값이면 ' / '로 이어 붙인다."""
    o.tenant = o.tenant or tenant
    o.dns_name = o.dns_name or dns_name
    o.description = _join([o.description, desc])[:200]
    o.comments = _join([o.comments, note])
    d = o.custom_field_data
    for k in _TEXT_CF:
        if cf.get(k):
            d[k] = _join([d.get(k) or '', cf[k]]) or None
    for k in _FILL_CF:
        if cf.get(k) and not d.get(k):
            d[k] = cf[k]
    if cf.get('host_mac') and d.get('host_mac') and d['host_mac'] != cf['host_mac']:
        o.comments = _join([o.comments, f"엑셀 MAC: {cf['host_mac']}"])


def commit(rows, site_name, update_existing=False, log=print, mode=None):
    """NetBox 스크립트 트랜잭션 안에서 호출.
    이미 있는 IP 처리(mode): skip(건너뜀) / merge(합치기) / overwrite(덮어쓰기).
    mode 를 안 주면 update_existing 값으로 결정(예전 호출 호환)."""
    mode = mode or ('overwrite' if update_existing else 'skip')
    from dcim.models import Device, DeviceRole, DeviceType, Interface, Location, Manufacturer, Site
    from ipam.models import IPAddress, Prefix
    from tenancy.models import Tenant
    from netaddr import IPNetwork

    # NetBox API·플러그인과 같은 IP 잠금. 스크립트는 이미 트랜잭션 안이므로 트랜잭션 단위 잠금(커밋 때 해제)을 쓴다
    with connection.cursor() as cur:
        cur.execute('SELECT pg_advisory_xact_lock(%s)', [IP_LOCK])
    ensure_custom_fields()
    site, _ = Site.objects.get_or_create(name=site_name, defaults={'slug': _slug('site', site_name), 'status': 'active'})
    mf, _ = Manufacturer.objects.get_or_create(slug='generic', defaults={'name': 'Generic'})
    dt, _ = DeviceType.objects.get_or_create(slug='l2-access-switch', defaults={'manufacturer': mf, 'model': 'L2 Access Switch'})
    role, _ = DeviceRole.objects.get_or_create(slug='access-switch', defaults={'name': 'Access Switch', 'color': '2196f3'})

    locs = {l.name: l for l in Location.objects.filter(site=site)}

    def loc(name, parent=None, desc=''):
        name = name[:100]
        if name not in locs:
            o = Location(name=name, slug=_slug('loc', name), site=site, parent=parent, status='active', description=desc[:200])
            try:
                o.full_clean()
            except ValidationError as e:
                log(f"호실 '{name}' 등록 실패(그 호실 연결 없이 계속) — {'; '.join(e.messages)[:150]}")
                locs[name] = None
                return None
            o.save()
            locs[name] = o
        return locs[name]

    counts = Counter()
    for x in rows:
        for rm in x.get('rooms') or ([x['room']] if x['room'] else []):
            loc(rm, loc(f"{rm.split('-')[0]}호관"), x['room_name'] if rm == x['room'] else '')
        if x['rack_room']:
            loc(x['rack_room'], loc(f"{x['rack_room'].split('-')[0]}호관"))
    counts['room'] = len({rm for x in rows for rm in (x.get('rooms') or [x['room']]) if rm})

    tenants = {t.name: t for t in Tenant.objects.all()}
    for d in sorted({x['dept'] for x in rows if x['dept']} - set(tenants)):
        t = Tenant(name=d[:100], slug=_slug('dept', d))
        try:
            t.full_clean()
        except ValidationError as e:
            log(f"소속 '{d}' 등록 실패(소속 없이 계속) — {'; '.join(e.messages)[:150]}")
            continue
        t.save(); tenants[d] = t

    nets = [x['net'] for x in rows if x['net']]
    base16 = str(nets[0].supernet(new_prefix=16)) if nets else None
    have = {str(p.prefix) for p in Prefix.objects.filter(prefix__net_contained_or_equal=base16)} if base16 else set()
    for n in sorted({str(n) for n in nets} - have):
        p = Prefix(prefix=n, status='active', scope=site)
        try:
            p.full_clean()
        except ValidationError as e:
            log(f"대역 {n} 등록 실패 — {'; '.join(e.messages)[:150]}")
            continue
        p.save()

    devs = {d.name: d for d in Device.objects.filter(role=role)}
    ports = {(i.device_id, i.name): i for i in Interface.objects.filter(device__role=role)}
    for sw_ip, rr in sorted({(x['sw_ip'], x['rack_room']) for x in rows if x['sw_ip']}):
        name = f'SW-{sw_ip}'
        if name in devs:
            continue
        d = Device(name=name, device_type=dt, role=role, site=site, status='active', location=locs.get(rr))
        try:
            d.full_clean()
        except ValidationError as e:
            log(f"스위치 {name} 등록 실패(스위치 연결 없이 계속) — {'; '.join(e.messages)[:150]}")
            continue
        d.save(); devs[name] = d
        mg = Interface(device=d, name='mgmt', type='virtual', mgmt_only=True); mg.save()
        mip = IPAddress.objects.filter(address__net_host=sw_ip).first()
        if mip is None:
            mip = IPAddress(address=IPNetwork(f'{sw_ip}/24'), status='active', description='switch mgmt', assigned_object=mg)
            try:
                mip.full_clean()
            except ValidationError as e:
                log(f"스위치 {name} 관리 IP 등록 실패(장비만 등록) — {'; '.join(e.messages)[:150]}")
                continue
            mip.save()
        elif mip.assigned_object is None:
            mip.snapshot(); mip.assigned_object = mg; mip.save()
        if mip.assigned_object_id == mg.pk:
            d.snapshot(); d.primary_ip4 = mip; d.save()
    counts['switch'] = len({x['sw_ip'] for x in rows if x['sw_ip']})

    existing = {}
    if base16:
        for o in IPAddress.objects.filter(address__net_host_contained=base16):
            existing[str(o.address.ip)] = o
    for x in rows:
        if not x['ip']:
            counts['blank'] += 1
            continue
        sw = devs.get(f"SW-{x['sw_ip']}") if x['sw_ip'] else None
        port = None
        if sw and x['sw_port']:
            port = ports.get((sw.pk, x['sw_port']))
            if not port:
                port = Interface(device=sw, name=x['sw_port'], type='1000base-t'); port.save()
                ports[(sw.pk, x['sw_port'])] = port
        room_obj = locs.get(x['room'][:100]) if x['room'] else None
        cf = {'room': room_obj.pk if room_obj else None, 'switch': sw.pk if sw else None,
              'switch_port': port.pk if port else None,
              'host_mac': x['mac'], 'ip_user': x['manager'] or None, 'manager_phone': x['phone'] or None,
              'outlet_no': x['outlet'] or None, 'patch_panel': x['patch'] or None, 'purpose': x['purpose'] or None,
              'patch_port': x['patch_port'] or None, 'legacy_seq': x['seq'] or None}
        desc = x['room_name'][:200]           # 설명(화면 이름: 호실명) = 호관호실명칭
        host, note = x['hostname'], x['note']
        if host and not _DNS.match(host):     # DNS 이름 규칙에 안 맞는 호스트 이름은 비고로
            note, host = _join([note, f'호스트 이름: {host}']), ''
        o = existing.get(str(x['ip']))
        if o:
            if mode == 'skip':
                counts['skipped'] += 1
                continue
            o.snapshot()
            if mode == 'merge':
                _merge_into(o, tenants.get(x['dept']), host, desc, note, cf)
                kind = 'merged'
            else:
                o.tenant, o.dns_name, o.description, o.comments = tenants.get(x['dept']), host, desc, note
                o.custom_field_data.update(cf)
                kind = 'updated'
        else:
            o = IPAddress(address=IPNetwork(f"{x['ip']}/24"), status='active')
            o.tenant, o.dns_name, o.description, o.comments = tenants.get(x['dept']), host, desc, note
            o.custom_field_data.update(cf)
            kind = 'created'
        try:
            o.full_clean()   # 한 행이 검사에 걸려도 나머지는 계속 (그 행만 건너뜀)
        except ValidationError as e:
            counts['failed'] += 1
            log(f"{x['line']}행 {x['ip']}: 반영 실패 — {'; '.join(e.messages)[:200]}")
            continue
        o.save()
        counts[kind] += 1
        existing[str(x['ip'])] = o
        if (counts['created'] + counts['updated']) % 500 == 0:
            log(f"IP {counts['created'] + counts['updated']}건 처리")
    return counts
