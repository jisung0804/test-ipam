"""엑셀 가져오기 — 형식 오류 무시 + 중복 IP 통합 + 기존 IP 합치기 검증 (테스트 NetBox 전용)
실행: manage.py shell -c "exec(open('.../verify_excel_lenient.py').read())"
"""
import io
import pandas as pd
from django.db import transaction
from ipam.models import IPAddress, Prefix
from dcim.models import Device, Location
from netbox_ip_request import room_import as R

R_ = {'p': 0, 'f': 0}
def ok(c, name, extra=''):
    R_['p' if c else 'f'] += 1; print(('  PASS ' if c else '  FAIL ') + name + ('' if c else f'  [{extra}]'))

HDR = ['연번', '호관호실', '학부(과)', '호관호실명칭', '용도', 'SUBNET ADDRESS', 'IP ADDRESS', 'MAC ADDRESS', 'OUTLET NO',
       'RACK 호관호실', '패치번호', '패치포트번호', '스위치IP ADDRESS', '스위치 포트 번호', '호스트 이름', '비고', 'PIC ID', '관리자', '전화번호']
def row(seq, room, dept, name, subnet, ip, mac='', host='', note='', mgr='', sw='', port=''):
    return [seq, room, dept, name, '사무용', subnet, ip, mac, '', '', '', '', sw, port, host, note, '', mgr, '']
rows = [
    row('1', '9-101', '전기공학과', '교수연구실', '052', '10', '00:11:22:52:00:10', 'pc-101', '', '김', '49.2', '3'),
    row('2', '9-102', '전기공학과', '실험실', '052', '10', '00:11:22:52:00:99', '', '공용', '이'),       # 1행과 같은 IP → 통합
    row('3', '9-103', '기계공학과', '세미나실', '05X', '11'),                                          # SUBNET 오류
    row('4', '9-104', '기계공학과', '사무실', '052', 'abc'),                                           # IP 오류
    row('5', '9-105', '기계공학과', '창고', '052', '255'),                                             # 브로드캐스트
    row('6', '9-106', '화학과', '분석실', '052', '12', 'ZZ-11-22'),                                    # MAC 오류
    row('7', '9-107', '화학과', '준비실', '052', '13', '', '한글 호스트'),                              # DNS 규칙 위반 호스트명
    row('8', '9-108', '화학과', '행정실', '052', '14', '', '', '', '박'),
    row('9', '9-109', '물리학과', '연구실', '052', '14', '', '', '', '최'),                              # 8행과 같은 IP → 통합
    row('10', '9-110', '물리학과', '연구실2', '052', '10', '', '', '', '정'),                           # 1·2행과 같은 IP(3중)
    row('11', '9-111', '화학과', '공용', '052', '15', '00ID09B0DF2A'),                                  # 영문 I → 숫자 1 자동 보정
    row('12', '9-112', '화학과', '공용', '052', '16', 'UPS'),                                           # MAC 칸에 글자
    row('13', '9-113', '화학과', '공용', '052', '17', '', '', '', '', '052.004', '7'),                    # 스위치 IP 앞자리 0
    row('14', '9-114', '가' * 150, '긴이름' * 50, '052', '18', '', '', '', '', '52.300', '1'),          # 범위 밖 스위치 IP, 너무 긴 소속·호실명
    row('15', '9-115' + 'X' * 120, '화학과', '공용', '052', '19'),                                     # 너무 긴 호관호실
]
path = '/tmp/lenient.xlsx'; pd.DataFrame(rows, columns=HDR).to_excel(path, index=False)

def cleanup():
    for d in Device.objects.filter(name__startswith='SW-165.246.52.'):
        d.primary_ip4 = None; d.save(); d.delete()
    IPAddress.objects.filter(address__net_host_contained='165.246.52.0/24').delete()
    Prefix.objects.filter(prefix='165.246.52.0/24').delete()
cleanup()

print('== 엄격 모드(기존 동작)')
st = R.validate(open(path, 'rb'), strict=True)
ok(len(st['errors']) >= 5, f"형식 오류·중복이 오류로 잡힘 ({len(st['errors'])}건)", st['errors'])

print('== 관대 모드: 검사')
rep = R.validate(open(path, 'rb'), strict=False)
ok(not rep['errors'], '오류 0건 (모두 경고로 전환)', rep['errors'])
ips = [str(x['ip']) for x in rep['rows'] if x['ip']]
ok(sorted(ips) == ['165.246.52.10', '165.246.52.12', '165.246.52.13', '165.246.52.14', '165.246.52.15', '165.246.52.16', '165.246.52.17', '165.246.52.18', '165.246.52.19'], 'IP 9개로 정리(중복 통합)', ips)
ok(len(rep['rows']) == 12, '행 15 → 12 (3중 1건 + 2중 1건 통합)', len(rep['rows']))
m17 = next(x for x in rep['rows'] if str(x['ip']) == '165.246.52.17')
ok(m17['sw_ip'] == '165.246.52.4', "스위치 IP '052.004' → 165.246.52.4 (앞자리 0 제거)", m17['sw_ip'])
m18 = next(x for x in rep['rows'] if str(x['ip']) == '165.246.52.18')
ok(m18['sw_ip'] is None and any('52.300' in w[1] for w in rep['warnings']), '범위 밖 스위치 IP는 경고 후 스위치 연결 없이', m18['sw_ip'])
m15 = next(x for x in rep['rows'] if str(x['ip']) == '165.246.52.15')
ok(m15['mac'] == '00:1d:09:b0:df:2a' and '원본 MAC: 00ID09B0DF2A' in m15['note'], 'MAC 의 영문 I·O 를 숫자로 자동 보정(원본은 비고)', m15)
m16 = next(x for x in rep['rows'] if str(x['ip']) == '165.246.52.16')
ok(m16['mac'] is None and '원본 MAC: UPS' in m16['note'], "MAC 칸의 'UPS' 같은 글자 → MAC 비우고 IP는 등록", m16)
m10 = next(x for x in rep['rows'] if str(x['ip']) == '165.246.52.10')
ok(m10['line'] == '2,3,11' and m10['rooms'] == ['9-101', '9-102', '9-110'], '통합 행: 원래 엑셀 행번호·호실 3개 보존', (m10['line'], m10['rooms']))
ok(m10['room_name'] == '교수연구실 / 실험실 / 연구실2' and m10['manager'] == '김 / 이 / 정', '글자 칸은 / 로 이어 붙임', m10)
ok(m10['mac'] == '00:11:22:52:00:10' and 'MAC 추가값: 00:11:22:52:00:99' in m10['note'], 'MAC은 첫 값, 다른 MAC은 비고에', m10['note'])
ok(m10['dept'] == '전기공학과' and '소속 추가값: 물리학과' in m10['note'], '소속은 첫 값, 다른 소속은 비고에', m10['note'])
blank = {x['room']: x for x in rep['rows'] if not x['ip']}
ok(set(blank) == {'9-103', '9-104', '9-105'}, 'SUBNET·IP 오류/브로드캐스트 행은 IP 없이 호실만', sorted(blank))
ok('원본 IP: abc' in blank['9-104']['note'] and '원본 SUBNET: 05X' in blank['9-103']['note'], '원래 잘못된 값은 비고에 보존')
m12 = next(x for x in rep['rows'] if str(x['ip']) == '165.246.52.12')
ok(m12['mac'] is None and '원본 MAC: ZZ-11-22' in m12['note'], 'MAC 오류 → MAC 비우고 비고에 원본')
ok(sum(1 for w in rep['warnings'] if w[1].startswith('[무시하고 반영]')) == 5, '무시한 형식 오류 5건 경고 표시', rep['warnings'])

print('== 관대 모드: 반영')
with transaction.atomic():
    c = R.commit(rep['rows'], 'LENIENT-SITE', mode='skip', log=lambda m: None)
ok(c['created'] == 9 and c['failed'] == 0 and c['blank'] == 3, '신규 9건, 실패 0 (긴 소속·호실명도 중단 없이)', dict(c))
ok(IPAddress.objects.filter(address__net_host='165.246.52.17').first().custom_field_data.get('switch')
   and Device.objects.filter(name='SW-165.246.52.4').exists(), '정규화된 스위치 IP 로 스위치 생성·연결')
o = IPAddress.objects.get(address__net_host='165.246.52.10')
ok(o.description == '교수연구실 / 실험실 / 연구실2' and o.tenant.name == '전기공학과', '호실명=설명, 소속=테넌트', (o.description, o.tenant))
ok(Location.objects.get(pk=o.custom_field_data['room']).name == '9-101' and Location.objects.filter(name__in=['9-102', '9-110'], site__name='LENIENT-SITE').count() == 2,
   '호관호실 = 첫 호실, 나머지 호실도 위치로 생성')
ok(o.custom_field_data.get('purpose') == '사무용', "용도는 '용도' 칸(사용자 정의 필드)으로", o.custom_field_data.get('purpose'))
h = IPAddress.objects.get(address__net_host='165.246.52.13')
ok(h.dns_name == '' and '호스트 이름: 한글 호스트' in h.comments, 'DNS 규칙에 안 맞는 호스트 이름은 비고로 (행 실패 없음)', (h.dns_name, h.comments))

print('== 기존 IP 합치기(merge) / 덮어쓰기(overwrite)')
rows2 = [row('1', '9-201', '전기공학과', '신규실', '052', '10', '00:11:22:52:00:10', 'pc-new', '추가메모', '홍')]
path2 = '/tmp/lenient2.xlsx'; pd.DataFrame(rows2, columns=HDR).to_excel(path2, index=False)
rep2 = R.validate(open(path2, 'rb'), strict=False)
with transaction.atomic():
    c2 = R.commit(rep2['rows'], 'LENIENT-SITE', mode='merge', log=lambda m: None)
o.refresh_from_db()
ok(c2['merged'] == 1 and o.description == '교수연구실 / 실험실 / 연구실2 / 신규실', '합치기: 설명 이어 붙임', o.description)
ok(o.dns_name == 'pc-101' and o.custom_field_data['ip_user'] == '김 / 이 / 정 / 홍', '합치기: 기존 값 유지 + 새 값 추가', (o.dns_name, o.custom_field_data['ip_user']))
ok(Location.objects.get(pk=o.custom_field_data['room']).name == '9-101', '합치기: 이미 있는 호실 연결은 유지')
with transaction.atomic():
    c3 = R.commit(rep2['rows'], 'LENIENT-SITE', mode='overwrite', log=lambda m: None)
o.refresh_from_db()
ok(c3['updated'] == 1 and o.description == '신규실' and o.dns_name == 'pc-new', '덮어쓰기: 엑셀 값으로 교체', (o.description, o.dns_name))
with transaction.atomic():
    c4 = R.commit(rep2['rows'], 'LENIENT-SITE', mode='skip', log=lambda m: None)
ok(c4['skipped'] == 1, '건너뜀: 기존 IP 그대로')
print('== 엑셀은 최초 값: L2 로 확인·덮어쓴 MAC·스위치·포트는 엑셀 재업로드로 되돌리지 않음')
ok(h.custom_field_data.get('data_source') == 'excel', "엑셀로 새로 만든 IP 의 데이터 기준 = 'excel'", h.custom_field_data.get('data_source'))
o.refresh_from_db()
o.custom_field_data.update({'data_source': 'l2_overwritten', 'host_mac': '00:aa:bb:cc:dd:ee', 'switch': None, 'switch_port': None})
o.save()
with transaction.atomic():
    c5 = R.commit(rep2['rows'], 'LENIENT-SITE', mode='overwrite', log=lambda m: None)
o.refresh_from_db()
ok(c5['l2_kept'] == 1 and o.custom_field_data['host_mac'] == '00:aa:bb:cc:dd:ee' and o.custom_field_data['data_source'] == 'l2_overwritten',
   '덮어쓰기 모드여도 L2 값(MAC) 유지', (dict(c5), o.custom_field_data.get('host_mac')))
ok(o.dns_name == 'pc-new' and o.custom_field_data['ip_user'] == '홍', 'L2 와 무관한 칸(호스트·관리자 등)은 엑셀대로 반영', (o.dns_name, o.custom_field_data['ip_user']))
cleanup()
print(f"\n결과: PASS {R_['p']} / FAIL {R_['f']}")
