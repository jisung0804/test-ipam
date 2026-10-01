"""Phase 3 검증: 관리 IP 장비 등록 · 대사/판정 · IP 자원 · VLAN 엑셀 · 포함 검색 · 일괄 삭제 (테스트 NetBox 전용)
실행: manage.py shell -c "exec(open('.../verify_phase3.py').read())"   (snmp_simlab.py 시뮬레이터 필요)
"""
import io
import time

import pandas as pd
from django.db import transaction
from django.utils import timezone

from core.models import ObjectChange
from dcim.models import Device, DeviceRole, DeviceType, Interface, Manufacturer, Site
from ipam.filtersets import IPAddressFilterSet
from ipam.models import IPAddress, Prefix, VLAN, VLANGroup
from tenancy.models import Tenant
from netbox_ip_request import inventory, logic, recon, resources
from netbox_ip_request.models import ArpEntry, Discrepancy, MacEntry

R = {'p': 0, 'f': 0}


def ok(c, n, x=''):
    R['p' if c else 'f'] += 1
    print(('  PASS ' if c else '  FAIL ') + n + ('' if c else f'  [{str(x)[:300]}]'))


def wipe():
    ArpEntry.objects.all().delete(); MacEntry.objects.all().delete(); Discrepancy.objects.all().delete()
    for d in Device.objects.filter(site__slug__in=['p3-site', 'snmp-test']):
        d.primary_ip4 = None; d.save()
    IPAddress.objects.filter(address__net_host_contained='165.246.48.0/21').delete()
    IPAddress.objects.filter(address__net_host_contained='127.0.0.0/8').delete()
    for d in Device.objects.filter(site__slug__in=['p3-site', 'snmp-test']):
        d.delete()
    Prefix.objects.filter(prefix__net_contained_or_equal='165.246.48.0/21').delete()
    Prefix.objects.filter(vlan__group__slug='p3-site-vlan').update(vlan=None)
    VLAN.objects.filter(group__slug='p3-site-vlan').delete()
    VLANGroup.objects.filter(slug='p3-site-vlan').delete()


wipe()
site, _ = Site.objects.get_or_create(slug='p3-site', defaults={'name': 'P3-SITE'})
PORTS = {f'127.0.0.{i}': 1161 for i in (11, 12, 13, 15)}

print('== P1 엑셀로 만든 스위치(포트 번호 인터페이스) 준비')
mf, _ = Manufacturer.objects.get_or_create(slug='generic', defaults={'name': 'Generic'})
dt, _ = DeviceType.objects.get_or_create(slug='l2-access-switch', defaults={'model': 'L2 Access Switch', 'manufacturer': mf})
acc, _ = DeviceRole.objects.get_or_create(slug='access-switch', defaults={'name': 'Access Switch', 'color': '2196f3'})
xl = Device.objects.create(name='SW-127.0.0.12', site=site, device_type=dt, role=acc, status='active',
                           local_context_data={'snmp_port': 1161})
mg = Interface.objects.create(device=xl, name='mgmt', type='virtual')
mip = IPAddress.objects.create(address='127.0.0.12/24', assigned_object=mg); xl.primary_ip4 = mip; xl.save()
p5 = Interface.objects.create(device=xl, name='5', type='1000base-t')
Prefix.objects.create(prefix='165.246.49.0/24', status='active', description='엑셀 대역')


def mkip(addr, **cf):
    o = IPAddress(address=f'{addr}/24', status='active', description=cf.pop('desc', ''))
    o.custom_field_data.update(cf); o.full_clean(); o.save(); return o


mkip('165.246.49.30', desc='교수실험실', host_mac='00:11:22:00:00:30', switch=xl.pk, switch_port=p5.pk, ip_user='홍길동')
mkip('165.246.49.10', desc='행정실', host_mac='00:11:22:00:00:10')
mkip('165.246.49.12', desc='세미나실', host_mac='00:11:22:00:00:12')
mkip('165.246.49.40', desc='창고(미사용)')

print('== P2 관리 IP만으로 장비 등록 (Juniper 코어 · Cisco 액세스(엑셀 기존) · Juniper 액세스)')
t0 = time.monotonic()
with transaction.atomic():
    rows, summ = inventory.onboard(['127.0.0.11', '127.0.0.12', '127.0.0.13'], site, ports=PORTS)
print(f'    {time.monotonic() - t0:.1f}초', [r[1] for r in rows])
ok(all(r[1] in ('신규', '갱신') for r in rows), '3대 모두 SNMP 조회·반영 성공', rows)
core = Device.objects.get(name='core-ex9208')
ok(core.role.slug == 'core-l3' and core.tags.filter(slug='ipam-arp').exists(), '코어: 이름=sysName, 역할 L3 자동 판별, ARP 수집 태그',
   (core.role, list(core.tags.all())))
ok(core.device_type.model == 'EX9208' and core.serial == 'JN12345' and core.platform and core.platform.slug == 'juniper-junos',
   '모델·시리얼(ENTITY-MIB), 플랫폼(벤더)', (core.device_type, core.serial, core.platform))
ok(core.primary_ip4 and str(core.primary_ip4.address.ip) == '127.0.0.11' and core.primary_ip4.assigned_object.name == 'me0',
   '관리 IP → 해당 인터페이스(me0)에 할당 + 기본 IPv4', core.primary_ip4)
p49 = Prefix.objects.get(prefix='165.246.49.0/24')
ok(p49.vlan and p49.vlan.vid == 49 and p49.vlan.name == 'office-49', '기존 엑셀 대역에 VLAN 49(이름) 자동 연결', p49.vlan)
ok(Prefix.objects.filter(prefix='165.246.50.0/24', vlan__vid=50).exists(), 'SVI 대역 165.246.50.0/24 생성 + VLAN 50 연결')
gw = IPAddress.objects.get(address__net_host='165.246.49.1')
ok(gw.assigned_object and gw.assigned_object.name == 'irb.49' and gw.description.startswith('게이트웨이'), '게이트웨이 IP 등록(irb.49)', gw)
cis = Device.objects.get(primary_ip4__address__net_host='127.0.0.12')
ok(cis.name == 'SW-127.0.0.12' and cis.device_type.model == 'WS-C2960X-48FPD-L', '엑셀로 만든 스위치는 이름 유지, 모델 갱신', (cis.name, cis.device_type))
g5 = Interface.objects.get(device=cis, name='Gi1/0/5')
ok(g5.description == '9-101 교수연구실' and (g5.custom_field_data or {}).get('oper_status') == 'up', '인터페이스 설명(ifAlias)·링크 상태', (g5.description, g5.custom_field_data))
ok(not Interface.objects.filter(device=cis, name='5').exists()
   and IPAddress.objects.get(address__net_host='165.246.49.30').custom_field_data['switch_port'] == g5.pk,
   "엑셀 포트 '5' → 실제 Gi1/0/5 로 옮기고 정리")
jx = Device.objects.get(name='sw-juniper')
names = set(Interface.objects.filter(device=jx).values_list('name', flat=True))
ok({'ge-0/0/9', 'ge-0/0/10'} <= names and 'ge-0/0/9.0' not in names, '물리 포트만 등록(.0 논리 유닛 제외)', names)
ok(Interface.objects.get(device=jx, name='ge-0/0/10').custom_field_data.get('oper_status') == 'down', '링크 down 포트 표시')
ok(jx.tags.filter(slug='ipam-mac').exists() and jx.role.slug == 'access-switch', 'L2 자동 판별 + MAC 수집 태그')
ok(summ.get('arp', 0) >= 6 and summ.get('mac', 0) >= 9 and 'recon' in summ, '등록 직후 ARP/MAC 수집·대사까지 실행', summ)
ok(Device.objects.filter(site=site).count() == 3, '장치 3대(중복 생성 없음)')
with transaction.atomic():
    rows2, _ = inventory.onboard(['127.0.0.11', '127.0.0.12', '127.0.0.13'], site, ports=PORTS, collect=False)
ok(all(r[1] == '갱신' and '인터페이스 +0' in r[2] for r in rows2) and Device.objects.filter(site=site).count() == 3,
   '다시 실행해도 중복 없음(멱등)', rows2)
with transaction.atomic():
    bad, _ = inventory.onboard(['127.0.0.16'], site, ports={'127.0.0.16': 1161}, collect=False)
ok(bad[0][1] == '실패' and '타임아웃' in bad[0][2], '응답 없는 IP는 실패 사유만 남김', bad)

print('== P3 대사(자동) · 관리자 판정')
ArpEntry.objects.create(ip='165.246.49.77', mac='00:aa:00:00:00:01', device='core-ex9208', interface='irb.49',
                        first_seen=timezone.now(), last_seen=timezone.now())
ArpEntry.objects.create(ip='165.246.49.77', mac='00:aa:00:00:00:02', device='core-ex9208', interface='irb.49',
                        first_seen=timezone.now(), last_seen=timezone.now())
mkip('165.246.49.77', desc='충돌')
c0 = ObjectChange.objects.count()
cnt = recon.reconcile()
ok(ObjectChange.objects.count() == c0, '대사 결과 기록은 변경 이력을 남기지 않음', ObjectChange.objects.count() - c0)
st = lambda a: IPAddress.objects.get(address__net_host=a).custom_field_data
ok(st('165.246.49.10')['recon_state'] == 'ok' and 'sw-juniper ge-0/0/9.0 [up' in (st('165.246.49.10').get('obs_location') or ''),
   '일치 + 관측 위치(스위치 포트·링크 상태·VLAN)', st('165.246.49.10'))
ok(st('165.246.49.12')['recon_state'] == 'mac_diff' and st('165.246.49.12')['obs_mac'] == '00:11:22:00:00:99', 'MAC 불일치 + 관측 MAC')
ok(st('165.246.49.40')['recon_state'] == 'unseen', '최근 ARP 에 없음 → 미관측')
ok(st('165.246.49.77')['recon_state'] == 'conflict', '같은 IP 를 MAC 2개가 씀 → IP 충돌')
ok(st('165.246.49.99').get('recon_state') == 'discovered', '대장에 없던 IP(자동 발견)', st('165.246.49.99'))
o30 = IPAddress.objects.get(address__net_host='165.246.49.30')
ok(st('165.246.49.30')['recon_state'] in ('unseen',), '엑셀에만 있고 안 보이는 IP', st('165.246.49.30'))
# 위치 불일치: 49.10 을 Cisco Gi1/0/5 에 등록했다고 바꿈
o10 = IPAddress.objects.get(address__net_host='165.246.49.10')
o10.custom_field_data.update({'switch': cis.pk, 'switch_port': g5.pk}); o10.save()
recon.reconcile()
ok(st('165.246.49.10')['recon_state'] == 'port_diff', '대장 위치 ≠ 관측 위치 → 위치 불일치', st('165.246.49.10'))
# 판정 → 반영
for a, rv in (('165.246.49.10', 'fix_ledger'), ('165.246.49.12', 'fix_ledger'), ('165.246.49.40', 'reclaim'), ('165.246.49.99', 'delete')):
    o = IPAddress.objects.get(address__net_host=a); o.custom_field_data['review'] = rv; o.save()
with transaction.atomic():
    res = recon.apply_review({'fix_ledger', 'reclaim', 'delete'}, log=lambda m: None)
ok(res == {'fix_ledger': 2, 'reclaim': 1, 'delete': 1, 'skipped': 0}, '판정 반영 건수', res)
c10, c12 = st('165.246.49.10'), st('165.246.49.12')
ok(c10['switch'] == jx.pk and Interface.objects.get(pk=c10['switch_port']).name == 'ge-0/0/9' and c10['review'] == 'confirmed',
   '대장 수정: 관측 위치(sw-juniper ge-0/0/9)로 스위치·포트 교체 → 정상 확인', c10)
ok(c12['host_mac'] == '00:11:22:00:00:99', '대장 수정: 관측 MAC 으로 교체', c12)
ok(IPAddress.objects.get(address__net_host='165.246.49.40').status == 'quarantine', '회수 대상 → 격리')
ok(not IPAddress.objects.filter(address__net_host='165.246.49.99').exists(), '삭제 대상 → 삭제')
recon.reconcile()
ok(st('165.246.49.10')['recon_state'] == 'ok' and st('165.246.49.12')['recon_state'] == 'ok', '반영 후 다시 대사하면 일치')

print('== P4 IP 자원 현황 · VLAN 엑셀')
rows_r, tot = resources.compute(days=30)
r49 = next(r for r in rows_r if r['prefix'] == '165.246.49.0/24')
ip49 = {str(a.ip) for a in IPAddress.objects.filter(address__net_host_contained='165.246.49.0/24').values_list('address', flat=True)}
arp49 = {a for a in ArpEntry.objects.filter(ip__startswith='165.246.49.').values_list('ip', flat=True)}
ok(r49['vid'] == 49 and r49['registered'] == len(ip49) and r49['in_use'] == len(arp49)
   and r49['available'] == 254 - len(ip49 | arp49), 'VLAN 49: 전체·대장·실사용·가용 계산', (r49, len(ip49), len(arp49)))
ok(r49['l2_mac'] >= 2 and r49['l2_switch'] >= 1 and r49['gateway'] == '165.246.49.1', 'L2 단말 MAC 수·스위치 수·게이트웨이', r49)
ok(r49['unreg_used'] == len(arp49 - ip49) and r49['reg_unused'] == len(ip49 - arp49), '대장O·미사용 / 대장X·사용')
x = resources.to_excel(rows_r)
df = pd.read_excel(io.BytesIO(x), dtype=str).fillna('')
ok(len(df) == len(rows_r) and list(df.columns)[:4] == ['VLAN ID', 'VLAN 이름', '설명', '대역'], '엑셀 내보내기')
df.loc[df['대역'] == '165.246.49.0/24', '설명'] = '본관 1층 사무실'
df.loc[df['대역'] == '165.246.49.0/24', '소속'] = '총무팀'
extra = pd.DataFrame([{'VLAN ID': '51', 'VLAN 이름': 'lab-51', '설명': '실습실', '대역': '51', '게이트웨이': '1'},
                      {'VLAN ID': 'abc', 'VLAN 이름': 'x'}])
buf = io.BytesIO(); pd.concat([df, extra]).to_excel(buf, index=False); buf.seek(0)
with transaction.atomic():
    c = resources.import_vlans(buf, site, log=lambda m: None)
ok(c['skipped'] == 1 and c['vlan_new'] == 1 and c['prefix_new'] == 1 and c['gateway_new'] == 1, 'VLAN 엑셀 업로드 건수', dict(c))
v49 = VLAN.objects.get(group__slug='p3-site-vlan', vid=49)
ok(v49.description == '본관 1층 사무실' and v49.tenant and v49.tenant.name == '총무팀'
   and Prefix.objects.get(prefix='165.246.49.0/24').description == '본관 1층 사무실', '설명·소속이 VLAN·대역에 반영')
ok(Prefix.objects.filter(prefix='165.246.51.0/24', vlan__vid=51).exists()
   and IPAddress.objects.filter(address='165.246.51.1/24').exists(), "대역 '51' → 165.246.51.0/24 생성·VLAN 연결·게이트웨이 .1")

print('== P5 포함 검색')
fs = lambda q: set(str(a.address.ip) for a in IPAddressFilterSet({'q': q}, IPAddress.objects.all()).qs)
ok('165.246.49.10' in fs('49.10') and '165.246.49.10' in fs('6.49.1'), "IP 중간 글자 '49.10' · '6.49.1' 로 검색")
ok('165.246.49.30' in fs('실험'), "호실명 중간 글자 '실험' (교수실험실)")
ok('165.246.49.30' in fs('길동'), "관리자(사용자 정의 필드) 중간 글자 '길동'")
ok('165.246.49.12' in fs('2200.0099') and '165.246.49.12' in fs('00-99'), 'MAC 을 다른 구분자·일부로 검색')
ok('165.246.49.10' in fs('juniper'), '연결 스위치 이름으로 검색')
ok(not fs('room') or '165.246.49.10' not in fs('room'), '필드 이름(키)에는 걸리지 않음')

print('== P6 조건 일괄 삭제(스크립트 로직)')
for i in range(150, 160):
    mkip(f'165.246.49.{i}', desc='삭제시험')
qs = IPAddressFilterSet().search(IPAddress.objects.filter(assigned_object_id__isnull=True), 'q', '삭제시험')
ok(qs.count() == 10, '조건(검색어) 대상 10건')
with transaction.atomic():
    for o in qs:
        o.snapshot(); o.delete()
ok(not IPAddress.objects.filter(description='삭제시험').exists() and IPAddress.objects.filter(address__net_host='165.246.49.1').exists(),
   '대상만 삭제, 장비에 붙은 게이트웨이는 보존')

wipe()
print(f"\n결과: PASS {R['p']} / FAIL {R['f']}")
