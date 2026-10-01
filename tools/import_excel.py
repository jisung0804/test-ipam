"""기존 엑셀 발급대장 → NetBox 이관

엑셀 형식 (시트 2개)
  [서브넷] CIDR | VLAN | 이름 | 게이트웨이 | 위치
  [IP]     IP | MAC | 호스트명 | 사용자 | 부서 | 용도 | 발급일

사용법
  python import_excel.py 대장.xlsx            # 드라이런: 검증 리포트만 (NetBox 변경 없음)
  python import_excel.py 대장.xlsx --commit   # 오류 0건일 때만 반영

- NetBox는 xlsx를 직접 가져오지 못하므로(CSV/JSON/YAML만 지원) 이 스크립트가 검증 후 REST API로 넣는다.
- IP는 한 번의 bulk POST로 전송 → NetBox가 한 트랜잭션으로 처리(1건이라도 실패하면 전체 미반영).
"""
import ipaddress
import sys

import pandas as pd

from nb import S, URL, get_all

from netparse import norm_mac

IP_COLS = {'IP': 'ip', 'MAC': 'mac', '호스트명': 'hostname', '사용자': 'user', '부서': 'dept', '용도': 'purpose',
           '발급일': 'assigned_on'}
NET_COLS = {'CIDR': 'cidr', 'VLAN': 'vlan', '이름': 'name', '게이트웨이': 'gateway', '위치': 'site'}


def _s(v):
    return '' if v is None or (isinstance(v, float) and pd.isna(v)) or str(v).strip() in ('nan', 'NaT') else str(v).strip()


def validate(path):
    xl = pd.read_excel(path, sheet_name=None, dtype=str)
    nets_df = xl.get('서브넷', pd.DataFrame(columns=list(NET_COLS))).rename(columns=NET_COLS)
    ips_df = xl['IP'].rename(columns=IP_COLS)
    errors, warnings = [], []

    existing_prefixes = {p['prefix']: p for p in get_all('/ipam/prefixes/')}
    new_nets = []
    for i, r in nets_df.iterrows():
        line = f'서브넷!{i + 2}'
        try:
            net = ipaddress.ip_network(_s(r['cidr']), strict=True)
        except ValueError:
            errors.append((line, f"CIDR 오류: {r['cidr']}")); continue
        overlap = [p for p in list(existing_prefixes) + [str(n['net']) for n in new_nets]
                   if ipaddress.ip_network(p).overlaps(net) and p != str(net)]
        if overlap:
            warnings.append((line, f'{net} 가 기존 대역 {overlap} 과 겹침(상하위 계층이면 정상)'))
        gw = _s(r.get('gateway')) or str(next(net.hosts()))
        if ipaddress.ip_address(gw) not in net:
            errors.append((line, f'게이트웨이 {gw} 가 {net} 밖')); continue
        if str(net) not in existing_prefixes:
            new_nets.append({'net': net, 'vlan': _s(r.get('vlan')), 'name': _s(r.get('name')), 'gateway': gw})
    all_nets = [ipaddress.ip_network(p) for p in existing_prefixes] + [n['net'] for n in new_nets]
    gateways = {n['gateway'] for n in new_nets}

    existing_ips = {a['address'].split('/')[0] for a in get_all('/ipam/ip-addresses/', brief=1)}
    seen_ip, seen_mac, rows = {}, {}, []
    for i, r in ips_df.iterrows():
        line = f'IP!{i + 2}'
        try:
            ip = ipaddress.ip_address(_s(r['ip']))
        except ValueError:
            errors.append((line, f"IP 형식 오류: {r['ip']}")); continue
        if str(ip) in seen_ip:
            errors.append((line, f'파일 내 IP 중복: {ip} (행 {seen_ip[str(ip)]})')); continue
        seen_ip[str(ip)] = line
        nets = sorted([n for n in all_nets if ip in n], key=lambda n: n.prefixlen, reverse=True)
        if not nets:
            errors.append((line, f'등록된 서브넷 밖: {ip}')); continue
        net = nets[0]
        if net.prefixlen < 31 and ip in (net.network_address, net.broadcast_address):
            errors.append((line, f'네트워크/브로드캐스트 주소: {ip}')); continue
        if str(ip) in gateways:
            errors.append((line, f'게이트웨이(예약) 주소: {ip}')); continue
        if str(ip) in existing_ips:
            errors.append((line, f'NetBox에 이미 존재: {ip}')); continue
        try:
            mac = norm_mac(_s(r.get('mac')))
        except ValueError as e:
            errors.append((line, str(e))); continue
        if mac and mac in seen_mac:
            warnings.append((line, f'동일 MAC 복수 IP: {mac} ({seen_mac[mac]}) — 서버 다중 IP면 정상'))
        if mac:
            seen_mac.setdefault(mac, line)
        if not _s(r.get('user')):
            errors.append((line, '사용자 누락')); continue
        ad = _s(r.get('assigned_on'))
        try:
            ad = pd.to_datetime(ad).date().isoformat() if ad else None
        except (ValueError, TypeError):
            errors.append((line, f'발급일 형식 오류: {ad}')); continue
        rows.append({'ip': ip, 'net': net, 'mac': mac, 'hostname': _s(r.get('hostname')), 'user': _s(r.get('user')),
                     'dept': _s(r.get('dept')), 'purpose': _s(r.get('purpose')), 'assigned_on': ad})
    return {'errors': errors, 'warnings': warnings, 'new_nets': new_nets, 'rows': rows}


def _ensure_tenant(name, cache):
    if not name:
        return None
    if name not in cache:
        found = S.get(URL + '/tenancy/tenants/', params={'name': name}).json()['results']
        if found:
            cache[name] = found[0]['id']
        else:
            slug = 'dept-' + str(abs(hash(name)) % 10**8)
            cache[name] = S.post(URL + '/tenancy/tenants/', json={'name': name, 'slug': slug}).json()['id']
    return cache[name]


def commit(rep):
    if rep['errors']:
        raise SystemExit(f"오류 {len(rep['errors'])}건 — 원본을 정리한 뒤 다시 실행하세요 (반영 안 함)")
    for n in rep['new_nets']:
        body = {'prefix': str(n['net']), 'status': 'active', 'description': n['name']}
        if n['vlan']:
            v = S.get(URL + '/ipam/vlans/', params={'vid': n['vlan']}).json()['results']
            body['vlan'] = v[0]['id'] if v else S.post(URL + '/ipam/vlans/', json={
                'vid': int(n['vlan']), 'name': n['name'] or f"VLAN{n['vlan']}", 'status': 'active'}).json()['id']
        r = S.post(URL + '/ipam/prefixes/', json=body); r.raise_for_status()
        r = S.post(URL + '/ipam/ip-addresses/', json={'address': f"{n['gateway']}/{n['net'].prefixlen}",
                                                      'status': 'reserved', 'description': 'gateway'})
        r.raise_for_status()
    tenants, payload = {}, []
    for x in rep['rows']:
        payload.append({'address': f"{x['ip']}/{x['net'].prefixlen}", 'status': 'active',
                        'tenant': _ensure_tenant(x['dept'], tenants), 'dns_name': x['hostname'],
                        'description': x['purpose'][:200],
                        'custom_fields': {'ip_user': x['user'], 'host_mac': x['mac'], 'assigned_on': x['assigned_on']}})
    if payload:
        r = S.post(URL + '/ipam/ip-addresses/', json=payload)   # bulk = 단일 트랜잭션
        if not r.ok:
            raise SystemExit(f'NetBox가 거부 — 전체 미반영: {r.status_code} {r.text[:300]}')
    return len(payload)


if __name__ == '__main__':
    rep = validate(sys.argv[1])
    for line, msg in rep['errors']:
        print('[오류]', line, msg)
    for line, msg in rep['warnings']:
        print('[경고]', line, msg)
    print(f"서브넷 신규 {len(rep['new_nets'])}, IP 유효 {len(rep['rows'])}, 오류 {len(rep['errors'])}, 경고 {len(rep['warnings'])}")
    if '--commit' in sys.argv:
        print('반영:', commit(rep), '건')
