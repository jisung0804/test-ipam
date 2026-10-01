"""Phase 2 SNMP 수집 검증 (테스트 NetBox 전용 — 수집 관측 데이터(ARP/MAC/불일치)를 모두 지운다!)

실행: cd /opt/netbox/netbox && python manage.py shell -c "exec(open('/path/verify_snmp.py').read())"
전제: simlab/gen.py 로 벤더별 SNMP 시뮬레이터가 127.0.0.x:161 에 떠 있어야 함
"""
import time

from django.db import transaction
from django.utils import timezone

from dcim.models import Device, DeviceRole, DeviceType, Interface, Manufacturer, Site
from extras.models import Tag
from ipam.models import IPAddress, Prefix
from netbox_ip_request import logic, snmp
from netbox_ip_request.models import ArpEntry, Discrepancy, MacEntry

R = {'pass': 0, 'fail': 0}


def ok(cond, name, extra=''):
    R['pass' if cond else 'fail'] += 1
    print(('  PASS ' if cond else '  FAIL ') + name + (f'  [{extra}]' if extra and not cond else ''))


def reset():
    ArpEntry.objects.all().delete(); MacEntry.objects.all().delete(); Discrepancy.objects.all().delete()
    for d in Device.objects.filter(site__slug='snmp-test'):
        d.primary_ip4 = None; d.save()
    IPAddress.objects.filter(description__startswith='snmptest').delete()
    IPAddress.objects.filter(tags__slug='ipam-discovered').delete()
    Prefix.objects.filter(description='자동 발견(ARP)').delete()
    Interface.objects.filter(device__site__slug='snmp-test').delete()
    Device.objects.filter(site__slug='snmp-test').delete()
    Prefix.objects.filter(description='snmptest').delete()


def setup():
    snmp.ensure_tags()
    site, _ = Site.objects.get_or_create(slug='snmp-test', defaults={'name': 'SNMP-TEST'})
    mf, _ = Manufacturer.objects.get_or_create(slug='generic', defaults={'name': 'Generic'})
    dt, _ = DeviceType.objects.get_or_create(slug='l2-access-switch', defaults={'model': 'L2 Access Switch', 'manufacturer': mf})
    acc, _ = DeviceRole.objects.get_or_create(slug='access-switch', defaults={'name': 'Access Switch', 'color': '4caf50'})
    core, _ = DeviceRole.objects.get_or_create(slug='core-l3', defaults={'name': 'Core L3', 'color': '2196f3'})
    arp_t, mac_t = Tag.objects.get(slug='ipam-arp'), Tag.objects.get(slug='ipam-mac')
    devs = {}
    spec = [('core-ex9208', '127.0.0.11', core, [arp_t]), ('sw-cisco', '127.0.0.12', acc, []),
            ('sw-juniper', '127.0.0.13', acc, []), ('sw-arubacx', '127.0.0.14', core, [arp_t, mac_t]),
            ('sw-procurve', '127.0.0.15', acc, []), ('sw-dead', '127.0.0.16', acc, []),
            ('sw-wrongpw', '127.0.0.17', acc, []), ('sw-comware', '127.0.0.18', core, [mac_t]),
            ('sw-noip', None, acc, [])]
    for name, ip, role, tags in spec:
        d = Device.objects.create(name=name, site=site, device_type=dt, role=role, status='active',
                                   local_context_data={'snmp_port': 1161})
        d.tags.set(tags)
        if ip:
            itf = Interface.objects.create(device=d, name='mgmt', type='virtual')
            a = IPAddress.objects.create(address=f'{ip}/32', assigned_object=itf, description='snmptest mgmt')
            d.primary_ip4 = a; d.save()
        devs[name] = d
    for i in list(range(49, 51)) + list(range(60, 76)):
        Prefix.objects.create(prefix=f'165.246.{i}.0/24', description='snmptest', status='active')
    port9 = Interface.objects.create(device=devs['sw-juniper'], name='9', type='1000base-t')
    gi5 = Interface.objects.create(device=devs['sw-cisco'], name='Gi1/0/5', type='1000base-t')

    def ip(addr, status='active', **cf):
        o = IPAddress(address=f'{addr}/24', status=status, description='snmptest')
        o.custom_field_data.update(cf); o.full_clean(); o.save(); return o
    ip('165.246.49.10', host_mac='00:11:22:00:00:10', switch=devs['sw-juniper'].pk, switch_port=port9.pk)  # 포트 일치
    ip('165.246.49.11')                                          # MAC 비어 있음 → ARP로 채움 → 포트 채움
    ip('165.246.49.12', host_mac='00:11:22:00:00:12')            # MAC 불일치
    ip('165.246.49.13', host_mac='00:11:22:00:00:13', switch=devs['sw-cisco'].pk, switch_port=gi5.pk)  # 포트 불일치
    ip('165.246.50.20', status='dormant', host_mac='00:11:22:00:05:20')  # 미사용 → 사용 복귀
    return devs


print('== S1 벤더별 표준 MIB 해석 (장치 직접 폴링)')
def P(host, name, arp=True, mac=True):
    return snmp.poll([{'name': name, 'host': host, 'port': 1161, 'arp': arp, 'mac': mac}])[0]
r = P('127.0.0.11', 'core-ex9208', mac=False)
ok(r['error'] is None and r['system']['vendor'] == 'Juniper', 'Juniper 코어 응답/벤더 판별', r)
arp = {a[0]: a for a in r['arp']}
ok(len(r['arp']) == 6, 'ARP 6건(invalid·멀티캐스트 제외)', r['arp'])
ok(arp['165.246.49.10'] == ('165.246.49.10', '00:11:22:00:00:10', 'irb.49'), 'ARP 행 = (IP, MAC, 인터페이스명)')
r = P('127.0.0.12', 'sw-cisco', arp=False)
ok(r['system']['vendor'] == 'Cisco' and 'VLAN context 3' in r['method'], 'Cisco: VLAN별 context(1,49,50) 사용, 1002 제외', r['method'])
m = {x[0]: x for x in r['mac']}
ok(m.get('00:11:22:00:00:30') == ('00:11:22:00:00:30', 49, 'Gi1/0/5'), 'Cisco MAC → VLAN 49 · Gi1/0/5', m.get('00:11:22:00:00:30'))
ok('00:11:22:00:00:ff' not in m and len(r['mac']) == 7, 'self(4)·포트0 항목 제외, 7건', len(r['mac']))
r = P('127.0.0.13', 'sw-juniper', arp=False)
m = {x[0]: x for x in r['mac']}
ok(r['method'] == 'Q-BRIDGE-MIB' and m['00:11:22:00:00:10'] == ('00:11:22:00:00:10', 49, 'ge-0/0/9.0'),
   'Juniper Q-BRIDGE: fdbId 5 → VLAN 49, 브리지포트 9 → ge-0/0/9.0', r['mac'])
r = P('127.0.0.14', 'sw-arubacx')
ok(r['mac'] == [('00:11:22:00:05:20', 50, '1/1/9')], 'Aruba CX: 브리지포트=ifIndex, VLAN=fdbId', r['mac'])
ok(r['arp'] == [('165.246.50.21', '00:11:22:00:05:21', 'vlan50')], 'Aruba CX ARP: ipNetToPhysical 대체 경로(local·IPv6 제외)', r['arp'])
r = P('127.0.0.15', 'sw-procurve', arp=False)
ok(r['method'] == 'BRIDGE-MIB' and r['mac'] == [('00:11:22:00:00:13', None, '7')], 'HP ProCurve: BRIDGE-MIB 기본 context', r)

print('== S2 오류 처리 / 다중 계정')
t0 = time.monotonic(); r = P('127.0.0.16', 'sw-dead'); dt = time.monotonic() - t0
ok(r['error'] and '타임아웃' in r['error'] and dt < 8, f'응답 없는 장비 → 타임아웃 안내, 다른 계정 재시도 안 함({dt:.1f}초)', r['error'])
r = P('127.0.0.17', 'sw-wrongpw')
ok(r['error'] and '인증 비밀번호' in r['error'], '비밀번호 틀림 → 원인 안내', r['error'])
r = P('127.0.0.18', 'sw-comware', arp=False)
ok(r['error'] is None and r.get('user') == 'ipam2' and len(r['mac']) == 1, '첫 계정 실패 시 두 번째 계정으로 성공', r)
r = snmp.poll([{'name': 'x', 'host': '127.0.0.11', 'port': 1161, 'arp': True, 'mac': False}], creds=[])[0]
ok(r['error'] and '계정' in r['error'], '계정 미설정 → 안내', r['error'])

for raw, want in (('Ciphering services not available or ciphertext is broken', '암호화 비밀번호'),
                  ('Unsupported SNMP security level', '보안 수준'), ('Unknown USM user', '사용자 이름'),
                  ('Wrong SNMP PDU digest', '인증 비밀번호')):
    ok(want in snmp._explain(raw), f'오류 문구 해석: {raw} → {snmp._explain(raw)}')
print('== S3 NetBox 대상 선정 + 대장 대사')
reset(); devs = setup()
tg, skipped = snmp.targets()
names = {t['name']: t for t in tg}
ok({'core-ex9208', 'sw-cisco', 'sw-juniper', 'sw-arubacx', 'sw-procurve', 'sw-comware'} <= set(names), '태그/역할로 대상 자동 선정', sorted(names))
ok(names['core-ex9208']['arp'] and not names['core-ex9208']['mac'], '코어: ARP만')
ok(names['sw-juniper']['mac'] and not names['sw-juniper']['arp'], '액세스(역할): MAC만')
ok(('sw-noip', '기본 IPv4(primary IP) 없음') in skipped, '기본 IP 없는 장치 → 건너뜀 사유')
test = [t for t in tg if t['name'] in {d for d in devs}]
res = snmp.poll(test)
tot = snmp.ingest(res)
ok(tot['devices'] == 8 and tot['ok'] == 6 and tot['failed'] == 2, '대상 8대: 성공 6 / 실패 2(dead·wrongpw)', tot)
disc = {(d.kind, d.ip) for d in Discrepancy.objects.filter(resolved=False)}
ok(('unregistered_use', '165.246.49.99') in disc and ('unregistered_use', '165.246.50.21') in disc, '대장에 없는 사용 IP 탐지(Juniper·Aruba ARP)', disc)
ok(('mac_mismatch', '165.246.49.12') in disc, 'MAC 불일치 탐지')
ok(IPAddress.objects.get(address='165.246.50.20/24').status == 'active', '미사용(dormant) IP가 다시 보이면 사용으로 복귀')
o = IPAddress.objects.get(address='165.246.49.11/24')
ok(o.custom_field_data.get('host_mac') == '00:11:22:00:00:11', '비어 있던 MAC을 ARP로 채움')
sw = Device.objects.filter(pk=o.custom_field_data.get('switch')).first()
pt = Interface.objects.filter(pk=o.custom_field_data.get('switch_port')).first()
ok(sw and sw.name == 'sw-juniper' and pt and pt.name == 'ge-0/0/10', '비어 있던 스위치·포트를 MAC 테이블로 채움', (sw, pt))
ok(tot['ports'].get('matched', 0) >= 1, "등록 포트 '9' = 관측 ge-0/0/9.0 일치", tot['ports'])
ok(('port_mismatch', '165.246.49.13') in disc, '등록(sw-cisco Gi1/0/5) ≠ 관측(sw-procurve 7) → 포트 불일치')
loc = logic.locate('165.246.49.10')
ok(loc and loc['device'] == 'sw-juniper' and loc['port'] == 'ge-0/0/9.0', 'IP 위치 추적 (IP→MAC→스위치 포트)', loc)
up = MacEntry.objects.filter(device='sw-cisco', port='Gi1/0/24').count()
ok(up == 6 and not any(e.port == 'Gi1/0/24' for e in MacEntry.objects.filter(mac='00:11:22:00:0a:00')
                        if logic._edge_port(e)), '업링크(MAC 6개) 포트는 사용자 위치로 쓰지 않음', up)

print('== S4 반복 수집 멱등성')
n_d, n_m, n_a = Discrepancy.objects.count(), MacEntry.objects.count(), ArpEntry.objects.count()
tot2 = snmp.ingest(snmp.poll(test))
ok((Discrepancy.objects.count(), MacEntry.objects.count(), ArpEntry.objects.count()) == (n_d, n_m, n_a),
   '두 번째 수집: 불일치·관측 건수 그대로(중복 생성 없음)')
ok(tot2['ports'].get('filled', 0) == 0 and tot2.get('mac_filled', 0) == 0, '이미 채운 값은 다시 바꾸지 않음', tot2)
from core.models import ObjectChange
c0 = ObjectChange.objects.count(); snmp.ingest(snmp.poll(test))
ok(ObjectChange.objects.count() == c0, '5분마다 돌아도 변경 이력이 쌓이지 않음(last_seen 은 이력 없이 갱신)', ObjectChange.objects.count() - c0)
e = MacEntry.objects.get(mac='00:11:22:00:00:30'); e.last_seen = timezone.now() - timezone.timedelta(days=3); e.save()
MacEntry.objects.filter(device='sw-cisco', port='Gi1/0/5').exclude(pk=e.pk).delete()
for k in range(5):  # 과거 회차에 다른 사용자 5명이 거쳐 간 포트
    MacEntry.objects.create(mac=f'00:44:00:00:00:0{k}', device='sw-cisco', port='Gi1/0/5', vlan='49',
                            first_seen=timezone.now() - timezone.timedelta(days=10 + k),
                            last_seen=timezone.now() - timezone.timedelta(days=10 + k))
snmp.ingest(snmp.poll([names['sw-cisco']]))
e.refresh_from_db()
ok(logic._edge_port(e), '사용자가 자주 바뀐 포트도 회차 기준으로 세어 업링크로 오판하지 않음')

print('== S5 규모: 액세스 40대 × 약 200 MAC + 코어 ARP 4,000건')
big = [{'name': f'big-{i}', 'host': f'127.0.1.{i}', 'port': 1161, 'arp': False, 'mac': True} for i in range(1, 41)]
big.append({'name': 'bigcore', 'host': '127.0.2.1', 'port': 1161, 'arp': True, 'mac': False})
t0 = time.monotonic(); res = snmp.poll(big); t_poll = time.monotonic() - t0
t0 = time.monotonic(); tot = snmp.ingest(res); t_ing = time.monotonic() - t0
ok(tot['ok'] == 41 and tot['mac'] == 40 * (47 * 3 + 60) and tot['arp'] == 4000, '41대 전부 수집, 건수 정확', tot)
print(f'    수집 {t_poll:.1f}초 · 대장 반영 {t_ing:.1f}초')
ok(t_poll + t_ing < 300, '1회 수집+반영이 5분 주기 안에 끝남', f'{t_poll + t_ing:.0f}s')
ok(Discrepancy.objects.filter(kind='unregistered_use', ip__startswith='165.246.6').count() == 4000 - 0
   or Discrepancy.objects.filter(kind='unregistered_use').count() >= 4000, '대장 없는 4,000건 모두 불일치로 등록')
t0 = time.monotonic(); snmp.ingest(res); t_ing2 = time.monotonic() - t0
print(f'    두 번째 반영 {t_ing2:.1f}초')

print('== S6 대장에 없는 단말 IP 자동 등록')
Prefix.objects.filter(prefix='165.246.75.0/24').delete()
n_unreg = Discrepancy.objects.filter(kind='unregistered_use', resolved=False).count()
t0 = time.monotonic(); reg = logic.register_discovered(); t_reg = time.monotonic() - t0
print(f'    등록 {t_reg:.1f}초', reg)
ok(reg['created'] == 4002 and reg['prefixes'] == 1, 'ARP에만 있던 IP 4,002건 등록, 없던 대역 1개(/24) 생성', reg)
o = IPAddress.objects.get(address__net_host='165.246.49.99')
ok(o.tags.filter(slug='ipam-discovered').exists() and o.custom_field_data.get('host_mac') == '00:11:22:00:00:ee'
   and str(o.address) == '165.246.49.99/24' and o.custom_field_data.get('last_seen'), "'자동 발견' 태그·MAC·마지막 관측·대역 마스크", (o.address, o.custom_field_data))
ok(IPAddress.objects.filter(address__net_host_contained='165.246.75.0/24').count() == 250
   and Prefix.objects.filter(prefix='165.246.75.0/24', description='자동 발견(ARP)').exists(), '대역이 없던 IP는 /24 프리픽스를 만들어 그 안에 등록')
ok(Discrepancy.objects.filter(kind='unregistered_use', resolved=False).count() <= n_unreg - 4000,
   "등록된 IP의 '대장 없는 사용' 불일치는 해결 처리")
ok(logic.register_discovered()['created'] == 0, '다시 실행해도 중복 등록 없음')
ok(not IPAddress.objects.filter(address__net_host='165.246.50.1').exists(), '장비 자기 주소(local)·멀티캐스트는 등록 안 함')

print(f"\n결과: PASS {R['pass']} / FAIL {R['fail']}")
