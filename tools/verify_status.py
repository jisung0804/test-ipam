"""SNMP 수집 상태 LED 검증 (우측 상단). 실행: manage.py shell -c "exec(open('.../verify_status.py').read())"
전제: 테스트 NetBox 웹(127.0.0.1:8001), admin/admin, student/student123!"""
import datetime as dt
import re
import uuid

import requests
from django.utils import timezone

from core.models import Job
from netbox_ip_request.jobs import SnmpCollectJob, collect_status

R = {'p': 0, 'f': 0}
NAME = SnmpCollectJob.Meta.name


def ok(c, n, x=''):
    R['p' if c else 'f'] += 1
    print(('  PASS ' if c else '  FAIL ') + n + ('' if c else f'  [{str(x)[:300]}]'))


saved = list(Job.objects.filter(name=NAME).values_list('pk', flat=True))
Job.objects.filter(name=NAME).update(name=NAME + ' (검증 보관)')
now = timezone.now()


def job(status, **kw):
    return Job.objects.create(name=NAME, status=status, job_id=uuid.uuid4(), interval=5, **kw)


try:
    print('== 상태 판정')
    ok(collect_status()['state'] == 'down' and '예약' in collect_status()['detail'], '작업 없음 → 적색(작업자 확인)', collect_status())
    j = job('completed', started=now - dt.timedelta(minutes=3), completed=now - dt.timedelta(minutes=2), data={'ok': 350, 'failed': 3})
    s = collect_status()
    ok(s['state'] == 'ok' and '350' in s['detail'], '최근 정상 완료 → 녹색(성공/실패 대수)', s)
    r = job('running', started=now - dt.timedelta(minutes=1))
    ok(collect_status()['state'] == 'running', '수집 중 → 녹색 깜빡임', collect_status())
    r.started = now - dt.timedelta(minutes=90); r.save()
    ok(collect_status()['state'] == 'down' and '끝나지 않음' in collect_status()['detail'], '90분째 안 끝남 → 적색', collect_status())
    r.delete()
    j.completed = now - dt.timedelta(minutes=40); j.save()
    ok(collect_status()['state'] == 'down' and '수집 없음' in collect_status()['detail'], '40분째 수집 없음 → 적색', collect_status())
    job('errored', started=now - dt.timedelta(minutes=2), completed=now - dt.timedelta(minutes=1),
        error='JobTimeoutException: Task exceeded maximum timeout value (300 seconds)')
    s = collect_status()
    ok(s['state'] == 'down' and 'RQ_DEFAULT_TIMEOUT' in s['detail'], '마지막 수집 오류(시간 초과) → 적색 + 조치 안내', s)
    Job.objects.filter(name=NAME).delete()
    job('scheduled', scheduled=now - dt.timedelta(minutes=30))
    ok(collect_status()['state'] == 'down' and 'netbox-worker' in collect_status()['detail'], '예약이 30분째 실행 안 됨 → 적색(작업자 재시작 안내)', collect_status())
    from netbox_ip_request.jobs import clear_stale_jobs
    n = clear_stale_jobs()
    ok(n >= 1 and not Job.objects.filter(name=NAME, status='scheduled').exists(),
       '끊긴 예약 정리(작업자 시작 시 자동) → NetBox 가 새로 예약할 수 있게', n)
    Job.objects.filter(name=NAME).delete()
    job('completed', started=now - dt.timedelta(minutes=2), completed=now - dt.timedelta(minutes=1), data={'ok': 10, 'failed': 0})

    print('== 화면')
    def login(u, p):
        w = requests.Session()
        t = w.get('http://127.0.0.1:8001/login/').text
        w.post('http://127.0.0.1:8001/login/', data={'username': u, 'password': p,
               'csrfmiddlewaretoken': re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', t).group(1)},
               headers={'Referer': 'http://127.0.0.1:8001/login/'})
        return w
    a = login('admin', 'admin')
    h = a.get('http://127.0.0.1:8001/').text
    ok('ipam-snmp-led' in h and 'ipam-led-ok' in h and 'SNMP 수집: 정상' in h, '관리자 화면 우측 상단에 LED(녹색)·설명', h.count('ipam-snmp-led'))
    js = a.get('http://127.0.0.1:8001/plugins/ip-request/snmp-status/').json()
    ok(js['state'] == 'ok', '상태 JSON (30초마다 갱신용)', js)
    st = login('student', 'student123!')
    ok('ipam-snmp-led' not in st.get('http://127.0.0.1:8001/').text, '일반 사용자에게는 표시 안 함')
finally:
    Job.objects.filter(name=NAME).delete()
    Job.objects.filter(pk__in=saved).update(name=NAME)
print(f"\n결과: PASS {R['p']} / FAIL {R['f']}")
