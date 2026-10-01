"""IPAM 운영에 필요한 사용자 정의 필드를 NetBox에 생성 (여러 번 실행해도 안전)"""
from nb import S, URL

FIELDS = [
    ('ip_user', '사용자', 'text', None),
    ('host_mac', 'MAC(단말)', 'text', None),
    ('assigned_on', '발급일', 'date', None),
    ('expires_on', '만료일', 'date', None),
    ('last_seen', '마지막 관측', 'datetime', None),
    ('dormant_exempt', '미사용 판정 제외', 'boolean', False),
    ('quarantine_until', '격리 종료일', 'date', None),
]

for name, label, typ, default in FIELDS:
    if S.get(URL + '/extras/custom-fields/', params={'name': name}).json()['count']:
        continue
    body = {'name': name, 'label': label, 'type': typ, 'object_types': ['ipam.ipaddress'], 'group_name': 'IPAM 운영'}
    if default is not None:
        body['default'] = default
    r = S.post(URL + '/extras/custom-fields/', json=body)
    print(name, r.status_code, '' if r.ok else r.text[:200])
print('custom fields ready')
