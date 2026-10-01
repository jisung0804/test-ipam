"""호실 기준 IP 대장(학교 현행 양식) → NetBox 이관

현행 엑셀 컬럼 (19개)
  연번 | 호관호실 | 학부(과) | 호관호실명칭 | 용도 | SUBNET ADDRESS | IP ADDRESS | MAC ADDRESS | OUTLET NO |
  RACK 호관호실 | 패치번호 | 패치포트번호 | 스위치IP ADDRESS | 스위치 포트 번호 | 호스트 이름 | 비고 | PIC ID | 관리자 | 전화번호

해석 규칙
  - SUBNET ADDRESS = C클래스 3번째 옥텟(VLAN 1개 = /24 1개), IP ADDRESS = 4번째 옥텟
  - 앞 두 옥텟은 엑셀에 없음 → 기본값 165.246 (다르면 --base 로 지정)
  - IP ADDRESS가 000 또는 빈칸 = 미발급(공백) → IP는 만들지 않고 호실·대역만 등록
  - PIC ID 컬럼은 사용하지 않음(무시)
  - 스위치IP ADDRESS '49.2' = 스위치 관리 IP의 3·4번째 옥텟 → <base>.49.2
  - 호관호실 '1-001A' → 호관 '1' / 호실 '1-001A'

NetBox 매핑
  호관 → Location(상위), 호실 → Location(하위), 학부(과) → Tenant
  SUBNET → Prefix /24,  IP → IPAddress(active)
  스위치 → Device(역할 'access-switch'), 스위치 포트 → Interface
  IP ↔ 호실·스위치·포트 → 사용자 정의 필드(객체 참조) room / switch / switch_port
  OUTLET NO, 패치번호, 패치포트번호, 관리자, 전화번호, 비고 → 사용자 정의 필드(텍스트)

사용법
  python import_room_excel.py 대장.xlsx --site 인하대학교            # 드라이런(리포트만)
  python import_room_excel.py 대장.xlsx --site 인하대학교 --commit   # 오류 0건일 때만 반영
"""
import argparse
import ipaddress
import re
from collections import Counter

import pandas as pd

from nb import S, URL, get_all
from netparse import norm_mac

COLS = {'연번': 'seq', '호관호실': 'room', '학부(과)': 'dept', '호관호실명칭': 'room_name', '용도': 'purpose',
        'SUBNET ADDRESS': 'subnet', 'IP ADDRESS': 'host', 'MAC ADDRESS': 'mac', 'OUTLET NO': 'outlet',
        'RACK 호관호실': 'rack_room', '패치번호': 'patch', '패치포트번호': 'patch_port',
        '스위치IP ADDRESS': 'sw_ip', '스위치 포트 번호': 'sw_port', '호스트 이름': 'hostname', '비고': 'note',
        '관리자': 'manager', '전화번호': 'phone'}  # PIC ID는 사용하지 않음

CUSTOM_FIELDS = [  # (이름, 라벨, 타입, 참조 모델)
    ('ip_user', '사용자', 'text', None),
    ('host_mac', 'MAC(단말)', 'text', None),
    ('room', '호실', 'object', 'dcim.location'),
    ('switch', '연결 스위치', 'object', 'dcim.device'),
    ('switch_port', '스위치 포트', 'object', 'dcim.interface'),
    ('outlet_no', '아웃렛 번호', 'text', None),
    ('patch_panel', '패치 번호', 'text', None),
    ('patch_port', '패치 포트', 'text', None),
    ('manager_phone', '관리자 전화', 'text', None),
    ('legacy_seq', '엑셀 연번', 'text', None),
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


def validate(path, base):
    df = pd.read_excel(path, dtype=str).rename(columns=COLS)
    df = df[[c for c in COLS.values() if c in df.columns]]
    df = df[df.apply(lambda r: any(s(x) for x in r), axis=1)]  # 완전히 빈 행 제거
    errors, warnings, rows = [], [], []
    b1, b2 = (int(x) for x in base.split('.'))
    for i, r in df.iterrows():
        line = i + 2
        try:
            c3 = octet(r['subnet'], 'SUBNET')
        except ValueError as e:
            errors.append((line, str(e))); continue
        net = ipaddress.ip_network(f'{b1}.{b2}.{c3}.0/24')
        host = s(r['host'])
        if host in ('', '0', '00', '000'):
            ip = None  # 미발급(공백): 호실·대역만 등록
        else:
            try:
                c4 = octet(host, 'IP')
            except ValueError as e:
                errors.append((line, str(e))); continue
            ip = ipaddress.ip_address(f'{b1}.{b2}.{c3}.{c4}')
            if c4 == 255:
                errors.append((line, f'{ip}: 브로드캐스트 주소')); continue
        sw_ip = None
        if s(r.get('sw_ip')):
            m = re.fullmatch(r'(\d{1,3})\.(\d{1,3})', s(r['sw_ip']))
            if not m:
                warnings.append((line, f'스위치 IP 형식 확인 필요: {s(r["sw_ip"])!r}'))
            else:
                sw_ip = f'{b1}.{b2}.{m.group(1)}.{m.group(2)}'
        try:
            mac = norm_mac(s(r.get('mac')))
        except ValueError as e:
            errors.append((line, str(e))); continue
        room = s(r['room'])
        if not re.fullmatch(r'[0-9A-Za-z]+-[0-9A-Za-z]+', room):
            warnings.append((line, f'호관호실 형식 확인: {room!r}'))
        if ip and sw_ip and not s(r.get('sw_port')):
            warnings.append((line, f'{ip}: 스위치({sw_ip})는 있는데 포트 번호가 없음'))
        rows.append(dict(line=line, ip=ip, net=net, mac=mac, room=room, building=room.split('-')[0],
                         room_name=s(r['room_name']), dept=s(r['dept']), purpose=s(r['purpose']),
                         sw_ip=sw_ip, sw_port=s(r.get('sw_port')), hostname=s(r.get('hostname')),
                         outlet=s(r.get('outlet')), patch=s(r.get('patch')), patch_port=s(r.get('patch_port')),
                         rack_room=s(r.get('rack_room')), manager=s(r.get('manager')),
                         phone=s(r.get('phone')), note=s(r.get('note')), seq=s(r.get('seq'))))
    dup = [ip for ip, n in Counter(str(x['ip']) for x in rows if x['ip']).items() if n > 1]
    for d in dup:
        errors.append(('-', f'파일 내 IP 중복: {d}'))
    return {'errors': errors, 'warnings': warnings, 'rows': rows,
            'blank': sum(1 for x in rows if not x['ip'])}


# ------------------------------------------------------------------ NetBox 쓰기 (있으면 재사용, 없으면 생성)
def get_or_create(path, lookup, body):
    r = S.get(URL + path, params=lookup).json()
    if r['count']:
        return r['results'][0]['id']
    resp = S.post(URL + path, json=body)
    if not resp.ok:
        raise SystemExit(f'{path} 생성 실패: {resp.status_code} {resp.text[:300]}')
    return resp.json()['id']


def slug(t):
    return re.sub(r'[^a-z0-9-]+', '-', t.lower()).strip('-') or 'x'


def ensure_custom_fields():
    for name, label, typ, obj in CUSTOM_FIELDS:
        if S.get(URL + '/extras/custom-fields/', params={'name': name}).json()['count']:
            continue
        group = 'IPAM 운영' if name in ('ip_user', 'host_mac') else '위치·배선'
        body = {'name': name, 'label': label, 'type': typ, 'object_types': ['ipam.ipaddress'], 'group_name': group}
        if obj:
            body['related_object_type'] = obj
        r = S.post(URL + '/extras/custom-fields/', json=body)
        if not r.ok:
            raise SystemExit(f'사용자 정의 필드 {name} 생성 실패: {r.text[:300]}')


def _bulk_post(path, items, chunk=500):
    """여러 객체를 한 번에 생성 (500개씩). 생성된 객체 목록 반환."""
    out = []
    for i in range(0, len(items), chunk):
        r = S.post(URL + path, json=items[i:i + chunk])
        if not r.ok:
            raise SystemExit(f'{path} 생성 실패({i}번째부터): {r.status_code} {r.text[:400]}')
        out += r.json()
    return out


def commit(rep, site_name, progress=print):
    """미리 전부 조회 → 없는 것만 묶음으로 생성. 이미 NetBox에 있는 IP는 건너뜀(재실행 안전)."""
    if rep['errors']:
        raise SystemExit(f"오류 {len(rep['errors'])}건 — 원본 정리 후 다시 실행 (반영 안 함)")
    ensure_custom_fields()
    rows = rep['rows']
    site = get_or_create('/dcim/sites/', {'name': site_name},
                         {'name': site_name, 'slug': slug('site-' + site_name.encode().hex()[:12]), 'status': 'active'})
    mf = get_or_create('/dcim/manufacturers/', {'slug': 'generic'}, {'name': 'Generic', 'slug': 'generic'})
    dt = get_or_create('/dcim/device-types/', {'slug': 'l2-access-switch'},
                       {'manufacturer': mf, 'model': 'L2 Access Switch', 'slug': 'l2-access-switch'})
    role = get_or_create('/dcim/device-roles/', {'slug': 'access-switch'},
                         {'name': 'Access Switch', 'slug': 'access-switch', 'color': '2196f3'})

    # 1) 위치: 호관(상위) → 호실(하위). 스위치가 놓인 RACK 호실도 포함
    locs = {l['name']: l['id'] for l in get_all('/dcim/locations/', site_id=site, brief=1)}
    rooms = {x['room']: x['room_name'] for x in rows}
    rooms.update({x['rack_room']: '' for x in rows if x['rack_room'] and x['rack_room'] not in rooms})
    blds = {f"{r.split('-')[0]}호관" for r in rooms}
    new = [{'name': b, 'slug': slug('loc-' + b), 'site': site, 'status': 'active'} for b in sorted(blds) if b not in locs]
    for o in _bulk_post('/dcim/locations/', new):
        locs[o['name']] = o['id']
    new = [{'name': r, 'slug': slug('loc-' + r), 'site': site, 'status': 'active',
            'parent': locs[f"{r.split('-')[0]}호관"], 'description': (rooms[r] or '')[:200]}
           for r in sorted(rooms) if r not in locs]
    for o in _bulk_post('/dcim/locations/', new):
        locs[o['name']] = o['id']
    progress(f'  위치 {len(blds)}개 호관 / {len(rooms)}개 호실')

    # 2) 부서(테넌트), 3) 대역(/24)
    tenants = {t['name']: t['id'] for t in get_all('/tenancy/tenants/', brief=1)}
    new = [{'name': d, 'slug': slug('dept-' + d.encode().hex()[:16])} for d in sorted({x['dept'] for x in rows if x['dept']}) if d not in tenants]
    for o in _bulk_post('/tenancy/tenants/', new):
        tenants[o['name']] = o['id']
    base = str(rows[0]['net'].supernet(new_prefix=16)) if rows else None
    prefixes = {p['prefix'] for p in get_all('/ipam/prefixes/', within_include=base, brief=1)} if base else set()
    new = [{'prefix': n, 'status': 'active', 'scope_type': 'dcim.site', 'scope_id': site}
           for n in sorted({str(x['net']) for x in rows}) if n not in prefixes]
    _bulk_post('/ipam/prefixes/', new)
    progress(f'  대역 {len({str(x["net"]) for x in rows})}개')

    # 4) 스위치 + 관리 인터페이스 + 관리 IP, 5) 스위치 포트
    devs = {d['name']: d['id'] for d in get_all('/dcim/devices/', role_id=role, brief=1)}
    sw_rows = {}
    for x in rows:
        if x['sw_ip']:
            sw_rows.setdefault(x['sw_ip'], x['rack_room'])
    new = [{'name': f'SW-{ip}', 'device_type': dt, 'role': role, 'site': site, 'status': 'active',
            **({'location': locs[rr]} if rr else {})} for ip, rr in sorted(sw_rows.items()) if f'SW-{ip}' not in devs]
    created = _bulk_post('/dcim/devices/', new)
    for o in created:
        devs[o['name']] = o['id']
    if created:
        mg = _bulk_post('/dcim/interfaces/', [{'device': o['id'], 'name': 'mgmt', 'type': 'virtual', 'mgmt_only': True} for o in created])
        mips = _bulk_post('/ipam/ip-addresses/', [{'address': f"{o['name'][3:]}/24", 'status': 'active', 'description': 'switch mgmt',
                                                   'assigned_object_type': 'dcim.interface', 'assigned_object_id': m['id']}
                                                  for o, m in zip(created, mg)])
        r = S.patch(URL + '/dcim/devices/', json=[{'id': o['id'], 'primary_ip4': ip['id']} for o, ip in zip(created, mips)])
        if not r.ok:
            raise SystemExit(f'스위치 관리 IP 지정 실패: {r.text[:300]}')
    ports = {}
    if devs:
        for i in get_all('/dcim/interfaces/', device_role_id=role, brief=1):
            ports[(i['device']['id'], i['name'])] = i['id']
    need = sorted({(devs[f"SW-{x['sw_ip']}"], x['sw_port']) for x in rows if x['ip'] and x['sw_ip'] and x['sw_port']} - set(ports))
    for o in _bulk_post('/dcim/interfaces/', [{'device': d, 'name': n, 'type': '1000base-t'} for d, n in need]):
        ports[(o['device']['id'], o['name'])] = o['id']
    progress(f'  스위치 {len(sw_rows)}대 / 포트 {len(ports)}개')

    # 6) IP — 이미 있는 것은 건너뜀
    existing = {a['address'].split('/')[0] for a in get_all('/ipam/ip-addresses/', parent=base, brief=1)} if base else set()
    payload, skipped = [], 0
    for x in rows:
        if not x['ip']:
            continue
        if str(x['ip']) in existing:
            skipped += 1
            continue
        sw = devs.get(f"SW-{x['sw_ip']}") if x['sw_ip'] else None
        port = ports.get((sw, x['sw_port'])) if sw and x['sw_port'] else None
        payload.append({
            'address': f"{x['ip']}/24", 'status': 'active', 'tenant': tenants.get(x['dept']),
            'dns_name': x['hostname'], 'description': (x['purpose'] + ' / ' + x['room_name']).strip(' /')[:200],
            'comments': x['note'],
            'custom_fields': {'room': locs[x['room']], 'switch': sw, 'switch_port': port, 'host_mac': x['mac'],
                              'ip_user': x['manager'], 'manager_phone': x['phone'], 'outlet_no': x['outlet'],
                              'patch_panel': x['patch'], 'patch_port': x['patch_port'], 'legacy_seq': x['seq']}})
    done = 0
    for i in range(0, len(payload), 500):
        _bulk_post('/ipam/ip-addresses/', payload[i:i + 500])
        done += len(payload[i:i + 500])
        progress(f'  IP {done}/{len(payload)}')
    if skipped:
        progress(f'  이미 NetBox에 있어 건너뛴 IP {skipped}건')
    return len(payload)


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('xlsx')
    ap.add_argument('--base', default='165.246', help='IP 앞 두 옥텟 (기본 165.246)')
    ap.add_argument('--site', default='본교')
    ap.add_argument('--commit', action='store_true')
    a = ap.parse_args()
    rep = validate(a.xlsx, a.base)
    for line, msg in rep['errors']:
        print('[오류]', line, msg)
    for line, msg in rep['warnings']:
        print('[경고]', line, msg)
    print(f"유효 {len(rep['rows'])}건(그중 IP 미발급 {rep['blank']}건), 오류 {len(rep['errors'])}건, 경고 {len(rep['warnings'])}건")
    if a.commit:
        print('반영:', commit(rep, a.site), '건')
