"""자동 작업(SNMP 자동 수집·대사 계산 등) 상태 확인과 복구 — '마지막 수집 N분 전 / 수집 없음' 일 때

원인: 플러그인의 자동 작업은 NetBox 작업자(netbox-worker)가 시작할 때 한 번 예약되고, 끝날 때마다 다음 실행을
      스스로 예약한다. 작업 도중 작업자가 멈추거나(docker compose stop/restart, 서버 재부팅) 컨테이너가 죽으면
      그 작업이 '실행 중/대기' 상태로 남은 채 다음 예약이 끊기고, 작업자를 다시 켜도 '이미 예약돼 있다'고 보고
      새로 예약하지 않는다.
하는 일: ① 작업별 최근 상태 출력 ② 끊긴(오래된 실행 중·지난 대기) 작업을 '오류'로 정리 ③ 자동 작업을 다시 예약
실행 (/opt/netbox-docker):
  docker compose exec -T netbox /opt/netbox/venv/bin/python /opt/netbox/netbox/manage.py shell \
      < /opt/test-ipam/deploy/centos/fix_jobs.py            # 확인만
  docker compose exec -T -e FIX=1 netbox /opt/netbox/venv/bin/python /opt/netbox/netbox/manage.py shell \
      < /opt/test-ipam/deploy/centos/fix_jobs.py            # 정리 + 다시 예약
  docker compose restart netbox-worker                       # (권장) 작업자 다시 시작
"""
import datetime as dt
import os

from django.utils import timezone

from core.models import Job
from netbox.registry import registry

import netbox_ip_request.jobs  # noqa: F401  (자동 작업 등록)

FIX = os.environ.get('FIX') == '1'
now = timezone.now()
loc = lambda t: timezone.localtime(t).strftime('%m-%d %H:%M') if t else '-'

mine = {cls: kw for cls, kw in registry['system_jobs'].items() if cls.__module__.startswith('netbox_ip_request')}
print('== 플러그인 자동 작업')
for cls, kw in mine.items():
    qs = Job.objects.filter(name=cls.name)
    last = qs.filter(completed__isnull=False).order_by('-completed').first()
    live = list(qs.filter(status__in=['pending', 'scheduled', 'running']).order_by('created'))
    print(f"- {cls.name} (주기 {kw['interval']}분)")
    print(f"    마지막 완료: {loc(last.completed) if last else '없음'} {last.status if last else ''} "
          f"{(last.error or '')[:100] if last else ''}")
    for j in live:
        print(f"    진행 대기/중: #{j.pk} {j.status} 생성 {loc(j.created)} 예정 {loc(j.scheduled)} 시작 {loc(j.started)}")
    if not live:
        print('    ※ 다음 실행 예약 없음 → 자동 작업이 멈춘 상태')

stale = []
for cls, kw in mine.items():
    iv = kw['interval']
    for j in Job.objects.filter(name=cls.name, status__in=['pending', 'scheduled', 'running']):
        if j.status == 'running' and (j.started or j.created) < now - dt.timedelta(minutes=max(120, 6 * iv)):
            stale.append(j)
        elif j.status in ('pending', 'scheduled') and (j.scheduled or j.created) < now - dt.timedelta(minutes=30):
            stale.append(j)
print(f'\n끊긴 작업: {len(stale)}건' + ''.join(f'\n  #{j.pk} {j.name} {j.status} ({loc(j.scheduled or j.started or j.created)})' for j in stale))

if not FIX:
    print('\n확인만 했습니다. 정리하고 다시 예약하려면 -e FIX=1 을 붙여 다시 실행하세요.')
else:
    for j in stale:
        j.status, j.completed = 'errored', now
        j.error = '작업자 중단으로 끊긴 작업 — fix_jobs.py 가 정리'
        j.save()
    made = []
    for cls, kw in mine.items():
        j = cls.enqueue_once(**kw)          # 이미 정상 예약돼 있으면 그대로 둠
        made.append(f'{cls.name}: #{j.pk} {j.status} 예정 {loc(j.scheduled)}')
    print('\n== 다시 예약\n  ' + '\n  '.join(made))
    print('\n완료 — 1~2분 뒤 화면 우측 상단 SNMP 표시가 녹색(수집 중 깜빡임 → 정상)으로 바뀌는지 확인하세요.'
          '\n바뀌지 않으면: docker compose restart netbox-worker && docker compose logs --tail 50 netbox-worker')
