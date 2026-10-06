"""데이터 초기화 후 다시 쌓기 검증 — 장비 등록(SNMP) → VLAN 엑셀 → IP 대장 엑셀 → 수집·대사 → 관리자 판정 → L2 덮어쓰기
실행: manage.py shell -c "exec(open('.../verify_rebuild.py').read())"   (snmp_simlab 시뮬레이터 필요, 테스트 NetBox 전용 — 데이터 전부 지움)
"""
import io
import os

import pandas as pd
from django.conf import settings
from django.db import transaction

from dcim.models import Device, Interface, Site
from ipam.models import IPAddress, Prefix, VLAN
from users.models import User
from netbox_ip_request import inventory, recon, resources, room_import as RI
from netbox_ip_request.models import ArpEntry

R = {'p': 0, 'f': 0}
CFG = settings.PLUGINS_CONFIG['netbox_ip_request']
HERE = '/home/claude/netbox-ip-request'


def ok(c, n, x=''):
    R['p' if c else 'f'] += 1
    print(('  PASS ' if c else '  FAIL ') + n + ('' if c else f'  [{str(x)[:400]}]'))


print('== 0. 초기화 (reset_data.py)')
users_before = User.objects.count()
IPAddress.objects.create(address='10.99.0.1/24', description='초기화 전 잔여 데이터')
os.environ['CONFIRM'] = 'DELETE-ALL'
exec(open(f'{HERE}/deploy/centos/reset_data.py').read())
os.environ.pop('CONFIRM')
ok(IPAddress.objects.count() == 0 and Device.objects.count() == 0 and Prefix.objects.count() == 0 and VLAN.objects.count() == 0,
   'IP·장비·대역·VLAN 모두 0건', (IPAddress.objects.count(), Device.objects.count(), Prefix.objects.count()))
ok(User.objects.count() == users_before, '사용자 계정은 그대로', User.objects.count())

print('== 1. 관리자 판정 기간 설정: 자동 발견 끔 · L2 덮어쓰기 끔')
CFG['auto_register_discovered'] = False
CFG['l2_overwrite'] = False
site, _ = Site.objects.get_or_create(slug='rb-site', defaults={'name': 'RB-SITE'})
PORTS = {f'127.0.0.{i}': 1161 for i in (11, 12, 13)}

print('== 2. 장비 등록 (관리 IP 만, 수집 없이)')
with transaction.atomic():
    rows, summ = inventory.onboard(list(PORTS), site, ports=PORTS, collect=False)
ok(all(r[1] == '신규' for r in rows) and Device.objects.count() == 3, '3대 신규 등록', rows)
ok(ArpEntry.objects.count() == 0 and not summ, '이 단계에서는 수집 안 함', summ)
cis = Device.objects.get(primary_ip4__address__net_host='127.0.0.12')
jx = Device.objects.get(primary_ip4__address__net_host='127.0.0.13')
# 현장 스위치의 관리 IP(165.246.48.x)를 흉내 — 엑셀의 '스위치IP ADDRESS' 와 같은 주소
for d, a in ((cis, '165.246.48.12/24'), (jx, '165.246.48.13/24')):
    i = Interface.objects.create(device=d, name='mgmt-vlan', type='virtual')
    IPAddress.objects.create(address=a, assigned_object=i, description='switch mgmt')

print('== 3. VLAN 엑셀')
buf = io.BytesIO()
pd.DataFrame([{'VLAN ID': 49, 'VLAN 이름': 'EE-LAB', '설명': '9호관 연구실', '대역': '165.246.49.0/24'}]).to_excel(buf, index=False)
buf.seek(0)
c = resources.import_vlans(buf, site, log=lambda m: None)
p49 = Prefix.objects.get(prefix='165.246.49.0/24')
ok(p49.vlan and p49.vlan.vid == 49 and p49.vlan.name == 'EE-LAB' and p49.description == '9호관 연구실',
   'VLAN 49 이름·설명을 장비에서 만든 대역에 반영', (p49.vlan, p49.description, dict(c)))
ok(VLAN.objects.filter(vid=49).count() == 1, 'VLAN 49 중복 없음', list(VLAN.objects.filter(vid=49)))

print('== 4. IP 대장 엑셀 (장비 등록 뒤에 올림)')
HDR = ['연번', '호관호실', '학부(과)', '호관호실명칭', '용도', 'SUBNET ADDRESS', 'IP ADDRESS', 'MAC ADDRESS', 'OUTLET NO',
       'RACK 호관호실', '패치번호', '패치포트번호', '스위치IP ADDRESS', '스위치 포트 번호', '호스트 이름', '비고', 'PIC ID', '관리자', '전화번호']


def row(seq, room, ip, mac='', sw='', port=''):
    return [seq, room, '전기공학과', '연구실', '사무용', '049', ip, mac, '', '', '', '', sw, port, '', '', '', '홍', '']


xl = [row('1', '9-101', '10', '00:11:22:00:00:10', '48.13', 'ge-0/0/9'),     # 실제와 같음
      row('2', '9-102', '30', '00:11:22:00:00:30', '48.12', '1/0/5'),        # 포트 표기만 다름(Gi1/0/5)
      row('3', '9-103', '12', '00:11:22:00:00:12', '48.12', '9/9/9'),        # 없는 포트 + MAC 다름
      row('4', '9-104', '11', '00:11:22:00:00:11', '48.13', 'ge-0/0/9'),     # 실제는 ge-0/0/10
      row('5', '9-105', '40')]                                                 # 장비 정보 없음
path = '/tmp/rebuild.xlsx'
pd.DataFrame(xl, columns=HDR).to_excel(path, index=False)
rep = RI.validate(open(path, 'rb'), strict=False)
dev_before = Device.objects.count()
with transaction.atomic():
    cnt = RI.commit(rep['rows'], 'RB-SITE', mode='skip', log=lambda m: None)
ok(cnt['created'] == 5, 'IP 5건 등록', dict(cnt))
ok(cnt['sw_linked'] == 2 and cnt['sw_new'] == 0, '결과 요약: 등록 장비에 연결 2대, 새로 만듦 0대', dict(cnt))
ok(Device.objects.count() == dev_before and not Device.objects.filter(name__startswith='SW-165.246.48').exists(),
   "엑셀 스위치 IP 는 등록된 실제 장비로 연결 — 'SW-<IP>' 가짜 장비 안 만듦", list(Device.objects.values_list('name', flat=True)))
cf = lambda a: IPAddress.objects.get(address__net_host=a).custom_field_data
ok(cf('165.246.49.10')['switch'] == jx.pk and Interface.objects.get(pk=cf('165.246.49.10')['switch_port']).name == 'ge-0/0/9',
   '같은 이름 포트 연결', cf('165.246.49.10'))
ok(cf('165.246.49.30')['switch'] == cis.pk and Interface.objects.get(pk=cf('165.246.49.30')['switch_port']).name == 'Gi1/0/5',
   "엑셀 '1/0/5' → 실제 Gi1/0/5", cf('165.246.49.30'))
ok(cf('165.246.49.12')['switch'] == cis.pk and not cf('165.246.49.12').get('switch_port') and cnt['port_unmatched'] == 1
   and not Interface.objects.filter(device=cis, name='9/9/9').exists(),
   '실제 장비에 없는 포트(9/9/9)는 연결 안 함·가짜 인터페이스 안 만듦', (cf('165.246.49.12'), dict(cnt)))
ok(cf('165.246.49.12').get('excel_orig') == 'MAC 00:11:22:00:00:12 · 스위치 165.246.48.12 · 포트 9/9/9'
   and cf('165.246.49.12').get('data_source') == 'excel', "엑셀 원본값(스위치 IP·포트 글자 그대로) 보관, 데이터 기준 = 엑셀",
   cf('165.246.49.12'))

print('== 5. 첫 수집·대사 (자동 발견 끔, 덮어쓰기 끔)')
with transaction.atomic():
    _, summ = inventory.onboard(list(PORTS), site, ports=PORTS, collect=True)
ok(summ.get('arp', 0) > 0 and 'discovered' not in summ, 'ARP·MAC 수집, 자동 발견 안 함', summ)
ok(not IPAddress.objects.filter(address__net_host='165.246.49.99').exists(), '대장에 없는 IP 는 아직 등록 안 됨')
st = {a: cf(a).get('recon_state') for a in ('165.246.49.10', '165.246.49.11', '165.246.49.12', '165.246.49.30', '165.246.49.40')}
ok(st == {'165.246.49.10': 'ok', '165.246.49.11': 'port_diff', '165.246.49.12': 'mac_diff',
          '165.246.49.30': 'unseen', '165.246.49.40': 'unseen'}, '대사 결과: 일치·위치 불일치·MAC 불일치·미관측', st)
ok(cf('165.246.49.12')['host_mac'] == '00:11:22:00:00:12' and cf('165.246.49.12')['obs_mac'] == '00:11:22:00:00:99',
   '덮어쓰기 꺼짐: 대장 MAC 은 엑셀 값 그대로, 관측 MAC 은 옆 칸에', cf('165.246.49.12'))

print('== 6. 자동 발견 켬 → 대장에 없는 IP 만 추가')
CFG['auto_register_discovered'] = True
n10 = IPAddress.objects.filter(address__net_host='165.246.49.10').count()
with transaction.atomic():
    _, summ = inventory.onboard(list(PORTS), site, ports=PORTS, collect=True)
ok(IPAddress.objects.filter(address__net_host='165.246.49.99').exists()
   and IPAddress.objects.get(address__net_host='165.246.49.99').tags.filter(slug='ipam-discovered').exists(),
   '대장에 없던 IP → 자동 발견(태그)으로 등록', summ.get('discovered'))
ok(n10 == 1 and IPAddress.objects.filter(address__net_host='165.246.49.10').count() == 1, '대장 IP 와 겹치지 않음(중복 0)')
ok(cf('165.246.49.99').get('recon_state') == 'discovered', '자동 발견 IP 의 대사 결과 = 대장 없음(자동 발견)', cf('165.246.49.99'))

print('== 7. 관리자 판정 → 반영')
o = IPAddress.objects.get(address__net_host='165.246.49.12'); o.custom_field_data['review'] = 'fix_ledger'; o.save()
o = IPAddress.objects.get(address__net_host='165.246.49.40'); o.custom_field_data['review'] = 'ignore'; o.save()
with transaction.atomic():
    res = recon.apply_review({'fix_ledger'}, log=lambda m: None)
ok(res['fix_ledger'] == 1 and cf('165.246.49.12')['host_mac'] == '00:11:22:00:00:99' and cf('165.246.49.12')['review'] == 'confirmed',
   '대장 수정 필요 → 관측 MAC 반영, 판정 = 정상 확인', (res, cf('165.246.49.12')))
recon.reconcile()
ok(cf('165.246.49.11')['recon_state'] == 'port_diff', '판정하지 않은 행은 그대로 남음(덮어쓰기 꺼짐)', cf('165.246.49.11'))

print('== 8. 판정 기간 종료 → L2 덮어쓰기 켬')
CFG['l2_overwrite'] = True
cnt = recon.reconcile()
c11 = cf('165.246.49.11')
ok(Interface.objects.get(pk=c11['switch_port']).name == 'ge-0/0/10' and c11['data_source'] == 'l2_overwritten'
   and 'ge-0/0/9→ge-0/0/10' in (c11.get('l2_changed') or ''), '남은 차이는 실제 L2 로 덮어씀(기록 남김)', c11)
ok(c11.get('excel_orig') == 'MAC 00:11:22:00:00:11 · 스위치 165.246.48.13 · 포트 ge-0/0/9', '엑셀 원본값은 처음 값 유지', c11)
ok(cf('165.246.49.10')['data_source'] == 'l2_same', '처음부터 같던 행 = L2 확인', cf('165.246.49.10'))

CFG['auto_register_discovered'] = False
CFG['l2_overwrite'] = True
# 정리: 다른 검증 스크립트가 같은 시뮬레이터 IP 를 쓰므로 이 사이트 장비를 지운다
with transaction.atomic():
    for d in Device.objects.filter(site=site):
        d.primary_ip4 = None; d.save()
    IPAddress.objects.filter(address__net_host_contained='127.0.0.0/8').delete()
    IPAddress.objects.filter(address__net_host_contained='165.246.48.0/21').delete()
    Device.objects.filter(site=site).delete()
print(f"\n결과: PASS {R['p']} / FAIL {R['f']}")
