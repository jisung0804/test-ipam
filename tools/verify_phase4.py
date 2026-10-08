"""Phase 4 검증: 신청서(신청자 정보·180일 고정) · 발급 범위 21~252 · 관리자 수동 발급·대역 변경 · 발급 안내 메일
실행: manage.py shell -c "exec(open('.../verify_phase4.py').read())"
전제: 테스트 NetBox(API http://127.0.0.1:8001), 메일 수신기(smtp_sink.py, 127.0.0.1:2525 → /tmp/mail)
"""
import datetime as dt
import email
import glob
import os
import time

import requests
from django.db import transaction
from django.test.utils import override_settings
from django.utils import timezone

from core.models import ObjectType
from dcim.models import Location, Site
from ipam.models import IPAddress, Prefix
from tenancy.models import Tenant
from users.models import ObjectPermission, Token, User
from netbox_ip_request import logic
from netbox_ip_request.models import ArpEntry, IPRequest

R = {'p': 0, 'f': 0}
API = 'http://127.0.0.1:8001/api/plugins/ip-request'


def ok(c, n, x=''):
    R['p' if c else 'f'] += 1
    print(('  PASS ' if c else '  FAIL ') + n + ('' if c else f'  [{str(x)[:300]}]'))


def wipe():
    IPRequest.objects.filter(requester_email__endswith='@p4.test').delete()
    IPRequest.objects.filter(prefix__prefix__net_contained_or_equal='10.60.0.0/16').delete()
    IPRequest.objects.filter(prefix__prefix__net_contained_or_equal='10.61.0.0/16').delete()
    for net in ('10.60.0.0/16', '10.61.0.0/16'):
        IPAddress.objects.filter(address__net_host_contained=net).delete()
        Prefix.objects.filter(prefix__net_contained_or_equal=net).delete()
    for l in Location.objects.filter(name__regex=r'^7[789]-'):
        l.delete()
    Location.objects.filter(name__in=['77호관', '78호관', '79호관']).delete()
    ArpEntry.objects.filter(ip__startswith='10.60.').delete()
    User.objects.filter(username='p4user').delete()
    ObjectPermission.objects.filter(name='p4-request').delete()
    for f in glob.glob('/tmp/mail/*.eml'):
        os.remove(f)


def mails():
    out = []
    for f in sorted(glob.glob('/tmp/mail/*.eml')):
        m = email.message_from_bytes(open(f, 'rb').read())
        body = m.get_payload(decode=True).decode(m.get_content_charset() or 'utf-8')
        subj = str(email.header.make_header(email.header.decode_header(m['Subject'])))
        out.append((m['To'], subj, body, m['From']))
    return out


wipe()
WHO = {'requester_name': '홍길동', 'requester_dept': 'P4 학과', 'requester_email': 'hong@p4.test',
       'requester_phone': '032-860-1234', 'room': 'P4-101', 'room_name': '교수연구실'}
A = Prefix.objects.create(prefix='10.60.1.0/24', status='active', description='P4 A')
B = Prefix.objects.create(prefix='10.60.2.0/24', status='active', description='P4 B')
IPAddress.objects.create(address='10.60.2.254/24', status='active', description='게이트웨이 (테스트)')
site = Site.objects.get_or_create(slug='p4-site', defaults={'name': 'P4-SITE'})[0]
loc = Location.objects.get_or_create(slug='p4-101', defaults={'name': 'P4-101', 'site': site, 'status': 'active'})[0]
admin = requests.Session()
admin.headers.update({'Authorization': f"Token {open('/home/claude/nbtoken').read().strip()}", 'Accept': 'application/json'})

print('== Q1 신청서: 신청자 정보 필수 · 사용 기한 180일 고정')
r = admin.post(f'{API}/requests/', json={'prefix': A.pk, 'purpose': 'x'})
ok(r.status_code == 400 and all(k in r.json() for k in ('requester_name', 'requester_dept', 'requester_email',
                                                       'requester_phone')) and 'room' not in r.json(),
   '이름·소속·이메일·연락처 필수 (건물·호실은 모르면 비워도 됨)', r.text)
r = admin.post(f'{API}/requests/', json={'prefix': A.pk, 'purpose': 'x', **{**WHO, 'requester_email': 'not-mail'}})
ok(r.status_code == 400 and 'requester_email' in r.json(), '이메일 형식 검사')
u = User.objects.create(username='p4user')
perm = ObjectPermission.objects.create(name='p4-request', actions=['view', 'add'])
perm.object_types.set([ObjectType.objects.get_for_model(IPRequest)]); perm.users.add(u)
tok = Token(user=u, version=1); tok.save()
user = requests.Session(); user.headers.update({'Authorization': f'Token {tok.token}', 'Accept': 'application/json'})
r = user.post(f'{API}/requests/', json={'prefix': A.pk, 'purpose': '연구용 PC', 'period_days': 365, **WHO})
ok(r.status_code == 201 and r.json()['period_days'] == 180, '일반 사용자가 365일을 보내도 180일로 고정', r.text)
rid_user = r.json().get('id')
ok(r.json().get('prefix') is None and '관리자' in (r.json().get('match_note') or ''),
   '일반 사용자가 대역을 보내도 무시 → 건물·호실로 매칭(못 찾으면 비움)', (r.json().get('prefix'), r.json().get('match_note')))
r = admin.post(f'{API}/requests/', json={'prefix': A.pk, 'purpose': '장기 장비', 'period_days': 365,
                                         **{**WHO, 'requester_email': 'admin@p4.test'}})
ok(r.status_code == 201 and r.json()['period_days'] == 365, '관리자는 기한 변경 가능')
rid_admin = r.json().get('id')
r = admin.post(f'{API}/requests/', json={'prefix': A.pk, 'purpose': '기본값', **{**WHO, 'requester_email': 'd@p4.test'}})
ok(r.json().get('period_days') == 180, '기한을 안 보내면 기본 180일')
rid_def = r.json().get('id')
r = admin.post(f'{API}/requests/', json={'prefix': A.pk, 'purpose': 'm1', 'mac': '00:aa:bb:cc:dd:01', **WHO})
r2 = admin.post(f'{API}/requests/', json={'prefix': A.pk, 'purpose': 'm2', 'mac': '00-AA-BB-CC-DD-01', **WHO})
ok(r.status_code == 201 and r2.status_code == 400 and 'mac' in r2.json(), '같은 MAC 처리 중 신청 중복 → 400(서버 오류 아님)', r2.text[:200])
IPRequest.objects.filter(pk=r.json().get('id')).delete()

print('== Q2 자동 발급 범위 21~252')
ip = logic.approve(rid_user, 'admin', prefix=A)
ok(str(ip.address.ip) == '10.60.1.21', '빈 대역 첫 자동 발급 = .21 (.1~.20 제외)', ip)
for h in range(22, 26):
    IPAddress.objects.create(address=f'10.60.1.{h}/24', status='active')
now = timezone.now()
ArpEntry.objects.create(ip='10.60.1.26', mac='00:60:00:00:00:26', device='core', interface='irb.60', first_seen=now, last_seen=now)
ip2 = logic.approve(rid_def, 'admin')
ok(str(ip2.address.ip) == '10.60.1.27', '대장 사용분·최근 ARP 사용분(.26) 건너뜀 → .27', ip2)
nxt, free, outside, busy = logic.available_preview(A)
ok(nxt == '10.60.1.28' and outside == 20 + 2 and busy == 1 and len(free) == 252 - 28 + 1,
   '미리보기: 다음 발급 .28, 범위 밖 빈 IP 22개(.1~.20, .253~.254), 사용 중 1', (nxt, outside, busy, len(free)))
for h in range(28, 253):
    IPAddress.objects.create(address=f'10.60.1.{h}/24', status='active')
try:
    logic.approve(rid_admin, 'admin'); ok(False, '범위 소진 시 발급 실패')
except logic.AllocationError as e:
    ok('발급 범위' in str(e) and IPRequest.objects.get(pk=rid_admin).status == 'submitted',
       '21~252 가 다 차면 .2~.20·.253 이 비어 있어도 자동 발급 안 함(신청은 접수 유지)', e)

print('== Q3 관리자 수동 발급 · 대역(VLAN) 변경')
for bad, why in (('10.60.1.0', '네트워크'), ('10.60.1.22', '이미 대장'), ('10.60.9.5', '대역 밖'), ('abc', '형식')):
    try:
        logic.approve(rid_admin, 'admin', ip=bad); ok(False, f'수동 {bad} 거부')
    except logic.AllocationError as e:
        ok(True, f'수동 지정 거부: {bad} ({why}) — {e}')
ArpEntry.objects.create(ip='10.60.1.5', mac='00:60:00:00:00:05', device='core', interface='irb.60', first_seen=now, last_seen=now)
try:
    logic.approve(rid_admin, 'admin', ip='10.60.1.5'); ok(False, '사용 중 IP 수동 거부')
except logic.AllocationError as e:
    ok('사용 중' in str(e), '최근 사용 중으로 보인 IP 는 확인 체크 없이 거부', e)
ip3 = logic.approve(rid_admin, 'admin', ip='10.60.1.5', force=True)
ok(str(ip3.address) == '10.60.1.5/24' and IPRequest.objects.get(pk=rid_admin).status == 'allocated',
   "범위 밖(.5)·사용 중이어도 관리자가 '사용 중이어도 발급' 체크하면 수동 발급")
r = admin.post(f'{API}/requests/', json={'prefix': A.pk, 'purpose': 'VLAN 잘못 선택', **{**WHO, 'requester_email': 'v@p4.test'}})
rid_v = r.json()['id']
ip4 = logic.approve(rid_v, 'admin', prefix=B)
rq = IPRequest.objects.get(pk=rid_v)
ok(str(ip4.address) == '10.60.2.21/24' and rq.prefix_id == B.pk and '대역 변경' in rq.reason,
   '관리자가 대역(VLAN)을 바꿔 발급 + 변경 기록', (ip4, rq.reason))
r = user.post(f'{API}/requests/{rid_v}/approve/')
ok(r.status_code == 403, '일반 사용자는 발급 불가(403)', r.status_code)

print('== Q4 발급 대장 기록')
o = IPAddress.objects.get(pk=ip.pk)
cf = o.custom_field_data
ok(cf['ip_user'] == '홍길동' and cf['manager_phone'] == '032-860-1234' and cf['room'] == loc.pk and o.description == '교수연구실'
   and o.tenant and o.tenant.name == 'P4 학과' and f'REQ-{rid_user}' in o.comments,
   '사용자·연락처·호실(위치 연결)·호실명·소속·신청번호가 대장에 기록', (cf, o.description, o.tenant, o.comments))
exp = (timezone.now() + dt.timedelta(days=180)).date()
ok(cf['expires_on'] == exp.isoformat() and IPRequest.objects.get(pk=rid_user).expires_on == exp, '만료일 = 발급일 + 180일')

print('== Q5 발급 안내 메일')
time.sleep(1)
ms = mails()
m = next((x for x in ms if x[0] == 'hong@p4.test'), None)
ok(m is not None, '신청자 이메일로 자동 발송', [x[0] for x in ms])
body = m[2] if m else ''
want = ['IP 발급 요청이 처리되었습니다. 해당 호실에서 아래 IP 주소를 사용하면 됩니다', '호실번호 : P4-101', '호실명 : 교수연구실',
        '사용자 : 홍길동', '연락처 : 032-860-1234', f'사용기한 : {exp:%Y-%m-%d} (180일)', 'IP 주소 : 10.60.1.21',
        'subnetmask : 255.255.255.0', 'gateway : 10.60.1.1', 'DNS : 165.246.10.2, 165.246.10.3']
ok(all(w in body for w in want), '본문 항목(호실번호·호실명·사용자·연락처·사용기한·IP·subnetmask·gateway·DNS)',
   [w for w in want if w not in body])
mv = next((x for x in ms if x[0] == 'v@p4.test'), None)
ok(mv and 'gateway : 10.60.2.254' in mv[2], "대역에 '게이트웨이' IP 가 있으면 그 주소를 안내", mv and mv[2])
ok('10.60.1.21' in (m[1] if m else ''), '제목에 발급 IP', m and m[1])
ok(m and m[3] == __import__("django.conf").conf.settings.SERVER_EMAIL, "보내는 사람 = NetBox EMAIL FROM_EMAIL (webmaster@localhost 아님)", m and m[3])
rq = IPRequest.objects.get(pk=rid_user)
ok(rq.notified_at and rq.notify_result.startswith('발송 완료'), '발송 결과 기록', rq.notify_result)
ok(sum(1 for x in ms if x[0] == 'hong@p4.test') == 1, '1건 발급에 메일 1통(중복 없음)', [x[0] for x in ms])
n0 = len(mails())
ok(logic.notify(rid_user) and len(mails()) == n0 + 1, '관리자 재발송')
from django.conf import settings as _s
_bad = {k: dict(v, OPTIONS=dict(v['OPTIONS'], port=2599)) for k, v in _s.MAILERS.items()}
with override_settings(MAILERS=_bad):
    r = admin.post(f'{API}/requests/', json={'prefix': B.pk, 'purpose': '메일 실패', **{**WHO, 'requester_email': 'f@p4.test'}})
    rid_f = r.json()['id']
    ipf = logic.approve(rid_f, 'admin')
rq = IPRequest.objects.get(pk=rid_f)
ok(rq.status == 'allocated' and rq.notify_result.startswith('발송 실패') and IPAddress.objects.filter(pk=ipf.pk).exists(),
   '메일 서버 장애여도 발급은 유지, 실패 사유 기록', rq.notify_result)
n0 = len(mails())
with transaction.atomic():
    r = admin.post(f'{API}/requests/', json={'prefix': A.pk, 'purpose': '롤백', **{**WHO, 'requester_email': 'rb@p4.test'}})
try:
    logic.approve(r.json()['id'], 'admin')
except logic.AllocationError:
    pass
ok(len(mails()) == n0 and not any(x[0] == 'rb@p4.test' for x in mails()), '발급 실패(롤백) 시 메일 안 보냄')

print('== Q6 건물·호실 → VLAN 대역 자동 매칭')
from netbox_ip_request import match as M
C = Prefix.objects.create(prefix='10.61.1.0/24', status='active', description='P4 C')
D = Prefix.objects.create(prefix='10.61.2.0/24', status='active', description='P4 D')
E = Prefix.objects.create(prefix='10.61.3.0/24', status='active', description='78호관 전용')
def bld(n):
    return Location.objects.create(name=n, slug=f'p4-{abs(hash(n)) % 10**8}', site=site, status='active')
def rm(b, n, *ips):
    l = Location.objects.create(name=n, slug=f'p4-{abs(hash(n)) % 10**8}', site=site, parent=b, status='active')
    for a in ips:
        IPAddress.objects.create(address=a, status='active', custom_field_data={'room': l.pk})
    return l
b77, b78, b79 = bld('77호관'), bld('78호관'), bld('79호관')
rm(b77, '77-101', '10.61.1.10/24', '10.61.1.11/24'); rm(b77, '77-103', '10.61.1.12/24'); rm(b77, '77-102')
rm(b77, '77-202', '10.61.2.10/24'); rm(b77, '77-203', '10.61.3.10/24'); rm(b77, '77-201')
rm(b78, '78-101', '10.61.1.20/24'); rm(b78, '78-102', '10.61.2.20/24')
rm(b79, '79-101')
r = M.match(b77, '101')
ok(r['room'] == '77-101' and r['prefix'] == C and '같은 호실' in r['note'], "건물 + '101' → 77-101, 같은 호실 IP 의 대역", r)
r = M.match(b77, '102')
ok(r['prefix'] == C and '같은 층' in r['note'], '호실 IP 없음 → 같은 층(1층) 대역', r['note'])
r = M.match(b77, '201')
ok(r['prefix'] == C and '같은 건물' in r['note'], '같은 층이 동점(2층 D·E 1개씩) → 건물 다수(C 3/5)', r['note'])
r = M.match(b78, '301')
ok(r['prefix'] == E and '78호관' in r['note'], '건물 안에서 갈리면 → 대역 설명에 건물 이름 있는 대역', r['note'])
r = M.match(b79, '101')
ok(r['prefix'] is None and '관리자' in r['note'], '근거 없음 → 비워 두고 관리자 선택', r['note'])
r = M.match(None, '77-101')
ok(r['prefix'] == C and r['building'] == b77, '건물 비우고 9-101 형식으로 적어도 건물·대역 찾음', r)
r = M.match(None, '')
ok(r['prefix'] is None and '정보 없음' in r['note'], '건물·호실 모두 비우면 대역도 비움', r['note'])
ok([x.name for x in M.buildings() if x.name in ('77호관', '78호관', '79호관')] == ['77호관', '78호관', '79호관']
   and not any(x.name == '77-101' for x in M.buildings()), '건물 드롭다운: 하위 호실이 있는 최상위 위치, 숫자 순')
r = user.post(f'{API}/requests/', json={'building': b77.pk, 'room': '101', 'prefix': D.pk, 'purpose': 'm',
                                        **{**WHO, 'room': '101', 'requester_email': 'm1@p4.test'}})
ok(r.status_code == 201 and r.json()['room'] == '77-101' and r.json()['prefix']['id'] == C.pk,
   '신청(API): 사용자가 보낸 대역은 무시, 77호관 101 → 77-101 · C 대역', r.text[:300])
rid_m = r.json()['id']
r = user.post(f'{API}/requests/', json={'building': b79.pk, 'room': '105', 'purpose': 'm',
                                        **{**WHO, 'room': '105', 'requester_email': 'm2@p4.test'}})
rid_none = r.json()['id']
ok(r.json()['prefix'] is None, '매칭 실패 신청은 대역 비어 있음', r.json().get('match_note'))
try:
    logic.approve(rid_none, 'admin'); ok(False, '대역 없이 발급 거부')
except logic.AllocationError as e:
    ok('대역' in str(e), '대역 없이 발급 시도 → 거부(관리자 선택 필요)', str(e))
ipn = logic.approve(rid_none, 'admin', prefix=D)
rq = IPRequest.objects.get(pk=rid_none)
ok(str(ipn.address) == '10.61.2.21/24' and rq.reason.startswith('대역 선택(관리자)'), '관리자가 대역을 골라 발급', (str(ipn.address), rq.reason))
r = admin.post(f'{API}/requests/', json={'prefix': E.pk, 'room': '101', 'building': b77.pk, 'purpose': 'm',
                                         **{**WHO, 'room': '101', 'requester_email': 'm3@p4.test'}})
ok(r.json()['prefix']['id'] == E.pk, '관리자가 API 로 대역을 지정하면 그대로', r.text[:200])
# 화면: 일반 사용자 신청서에는 대역 칸이 없고 건물 드롭다운이 있음
import re as _re
u.set_password('p4pass!23'); u.save()
w = requests.Session()
t = w.get('http://127.0.0.1:8001/login/').text
w.post('http://127.0.0.1:8001/login/', data={'username': 'p4user', 'password': 'p4pass!23',
       'csrfmiddlewaretoken': _re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', t).group(1)},
       headers={'Referer': 'http://127.0.0.1:8001/login/'})
h = w.get('http://127.0.0.1:8001/plugins/ip-request/requests/add/').text
ok('id_building' in h and '77호관' in h and 'id_prefix' not in h and '모름 / 목록에 없음' in h,
   '신청 화면: 건물 드롭다운(모름 포함), 대역 선택 칸 없음')
tok_ = _re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', h).group(1)
resp = w.post('http://127.0.0.1:8001/plugins/ip-request/requests/add/', allow_redirects=False,
              headers={'Referer': 'http://127.0.0.1:8001/plugins/ip-request/requests/add/'},
              data={'csrfmiddlewaretoken': tok_, 'requester_name': '홍길동', 'requester_dept': 'P4 학과',
                    'requester_email': 'ui@p4.test', 'requester_phone': '032-860-1234', 'building': str(b77.pk),
                    'room': '103', 'room_name': '실험실', 'purpose': '화면 신청'})
q = IPRequest.objects.filter(requester_email='ui@p4.test').first()
ok(resp.status_code == 302 and q and q.building == b77 and q.room == '77-103' and q.prefix == C,
   '화면 신청(위치 보기 권한 없는 사용자): 77호관 103 → 77-103 · C 대역 자동 매칭', (resp.status_code, q and (q.room, q.prefix)))

print('== 추가 요청: 호실 필수·호실 목록 · IP 개수 · 여러 개 자동/수동 발급')
ok('ipr-room-list' in h and '"77-101"' in h and '"78-102"' in h, '신청 화면: 호실 콤보박스에 전체 호실 목록(건물별)', h.count('ipr-room-list'))
ok('id="id_ip_count"' in h and '10개' in h, '신청 화면: IP 개수 선택(1~10개)')
tok_ = _re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', h).group(1)
resp = w.post('http://127.0.0.1:8001/plugins/ip-request/requests/add/', allow_redirects=False,
              headers={'Referer': 'http://127.0.0.1:8001/plugins/ip-request/requests/add/'},
              data={'csrfmiddlewaretoken': tok_, **WHO, 'requester_email': 'noroom@p4.test', 'building': str(b77.pk),
                    'room': '', 'purpose': '호실 없음', 'ip_count': '1'})
ok(resp.status_code == 200 and '호실번호는 필수' in resp.text and not IPRequest.objects.filter(requester_email='noroom@p4.test').exists(),
   '화면: 호실번호 비우면 신청 안 됨', resp.status_code)
W2 = {k: v for k, v in WHO.items() if k != 'room'}
r = admin.post(f'{API}/requests/', json={**W2, 'requester_email': 'noroom2@p4.test', 'purpose': 'x'})
ok(r.status_code == 400 and 'room' in r.text, 'API: 호실번호 없으면 400', r.text[:200])
resp = w.post('http://127.0.0.1:8001/plugins/ip-request/requests/add/', allow_redirects=False,
              headers={'Referer': 'http://127.0.0.1:8001/plugins/ip-request/requests/add/'},
              data={'csrfmiddlewaretoken': tok_, **WHO, 'requester_email': 'multi@p4.test', 'building': str(b77.pk),
                    'room': '77-103', 'purpose': 'PC 3대', 'ip_count': '3'})
q = IPRequest.objects.filter(requester_email='multi@p4.test').first()
ok(resp.status_code == 302 and q and q.ip_count == 3 and q.prefix == C, '화면: IP 3개 신청 (호실은 목록 값 77-103 그대로)', (resp.status_code, q and q.ip_count))
for f in glob.glob('/tmp/mail/*.eml'):
    os.remove(f)
ip = logic.approve(q.pk, 'admin')
q.refresh_from_db()
got = sorted(str(o.address.ip) for o in q.ip_addresses.all())
ok(len(ip.issued) == 3 and len(got) == 3 and all(21 <= int(a.split('.')[-1]) <= 252 for a in got) and q.ip_address == ip,
   '자동 발급: 신청 개수(3)만큼 발급 범위 안에서', got)
time.sleep(1)
mm = [m for m in mails() if m[0] == 'multi@p4.test']
ok(mm and '외 2개' in mm[0][1] and all(a in mm[0][2] for a in got) and 'IP 주소 (3개)' in mm[0][2], '안내 메일: 3개 모두 표시', mm and mm[0][1])
from django.core.exceptions import ValidationError as _VE
bad = IPRequest(requester='p4user', **{**W2, 'requester_email': 'mac2@p4.test'}, building=b77, room='77-101', purpose='x',
                ip_count=2, mac='00:11:22:33:44:55')
try:
    bad.full_clean(); ok(False, 'IP 2개 이상 + MAC → 거부')
except _VE as e:
    ok('mac' in e.message_dict, 'IP 2개 이상 + MAC → 거부(MAC 은 수집으로 채움)', e.message_dict)
def mk(n, email_):
    o = IPRequest(requester='p4user', **{**W2, 'requester_email': email_}, building=b77, room='77-102', purpose='x', ip_count=n)
    o.full_clean(); o.save(); return o
q2 = mk(2, 'man@p4.test')
try:
    logic.approve(q2.pk, 'admin', ip='10.61.1.200'); ok(False, '수동 지정 개수 불일치 거부')
except logic.AllocationError as e:
    ok('2개' in str(e) and IPRequest.objects.get(pk=q2.pk).status == 'submitted', '수동 지정: IP 개수가 신청 개수와 다르면 거부', e)
ip = logic.approve(q2.pk, 'admin', ip='10.61.1.200, 10.61.1.201')
ok(sorted(str(o.address.ip) for o in ip.issued) == ['10.61.1.200', '10.61.1.201'], '수동 지정: 쉼표로 2개', [str(o) for o in ip.issued])
q3 = mk(3, 'cnt@p4.test')
ip = logic.approve(q3.pk, 'admin', count=1)
q3.refresh_from_db()
ok(len(ip.issued) == 1 and q3.ip_count == 1 and '발급 개수 변경: 3 → 1' in q3.reason, '관리자가 발급 개수 변경(3→1)', q3.reason)
small = Prefix.objects.create(prefix='10.61.9.0/29', status='active', description='P4 small')
q4 = mk(8, 'many@p4.test')
before = IPAddress.objects.filter(address__net_host_contained='10.61.9.0/29').count()
try:
    logic.approve(q4.pk, 'admin', prefix=small); ok(False, '빈 IP 부족 거부')
except logic.AllocationError as e:
    ok('8개' in str(e) and IPAddress.objects.filter(address__net_host_contained='10.61.9.0/29').count() == before
       and IPRequest.objects.get(pk=q4.pk).status == 'submitted', '빈 IP 가 모자라면 하나도 발급하지 않음(전체 취소)', e)
small.delete()
a = requests.Session()
t = a.get('http://127.0.0.1:8001/login/').text
a.post('http://127.0.0.1:8001/login/', data={'username': 'admin', 'password': 'admin',
       'csrfmiddlewaretoken': _re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', t).group(1)},
       headers={'Referer': 'http://127.0.0.1:8001/login/'})
q5 = mk(4, 'page@p4.test')
pg = a.get(f'http://127.0.0.1:8001/plugins/ip-request/requests/{q5.pk}/').text
ok('name="count"' in pg and 'value="4"' in pg and '신청 <b>4개</b> 자동 발급 예정' in pg and 'ipr-free' in pg,
   '관리자 발급 화면: 발급 개수(4)·자동 발급 예정 IP 4개·빈 IP 눌러 수동 지정', pg.count('ipr-free'))
tk = _re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', pg).group(1)
rr = a.post(f'http://127.0.0.1:8001/plugins/ip-request/requests/{q5.pk}/approve/', allow_redirects=True,
            headers={'Referer': f'http://127.0.0.1:8001/plugins/ip-request/requests/{q5.pk}/'},
            data={'csrfmiddlewaretoken': tk, 'prefix': str(C.pk), 'mode': 'auto', 'count': '2', 'period_days': '180'})
q5.refresh_from_db()
ok(q5.status == 'allocated' and q5.ip_addresses.count() == 2 and '발급 완료 — 2개' in rr.text, '화면에서 개수 2로 바꿔 자동 발급', q5.ip_addresses.count())

wipe()
print(f"\n결과: PASS {R['p']} / FAIL {R['f']}")
