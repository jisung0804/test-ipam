"""워치독 셸 검증용 상태 만들기 (테스트 NetBox 전용) — python manage.py shell -c "import wd_fixture as F; F.xxx()" """
import datetime as dt
import uuid

from django.utils import timezone

from core.models import Job
from netbox_ip_request import jobs as J

NAME = J.SnmpCollectJob.Meta.name


def names():
    return [c.name for c in J._plugin_system_jobs()]


def clean():
    """플러그인 자동 작업의 대기·실행 중 행을 지우고, 최근 완료 1건을 '정상'으로 둔다"""
    Job.objects.filter(name__in=names(), status__in=['pending', 'scheduled', 'running']).delete()
    Job.objects.filter(name=NAME, error__startswith='WDTEST').delete()
    now = timezone.now()
    Job.objects.create(name=NAME, status='completed', job_id=uuid.uuid4(), interval=5, created=now,
                       started=now - dt.timedelta(seconds=60), completed=now, data={'polled': 1, 'deferred': 0})
    for c, kw in J._plugin_system_jobs().items():
        c.enqueue_once(**kw)


def stuck(minutes=93):
    Job.objects.filter(name=NAME, status__in=['pending', 'scheduled', 'running']).delete()
    j = Job.objects.create(name=NAME, status='running', job_id=uuid.uuid4(), interval=5,
                           started=timezone.now() - dt.timedelta(minutes=minutes))
    print('JOB', j.pk)


def errors(n=3, msg='WDTEST Task exceeded maximum timeout value (900 seconds)'):
    now = timezone.now()
    for i in range(n):
        Job.objects.create(name=NAME, status='errored', job_id=uuid.uuid4(), interval=5, error=msg,
                           started=now - dt.timedelta(minutes=15 * (n - i)), completed=now + dt.timedelta(seconds=i + 1))


def slow(secs=800):
    now = timezone.now()
    Job.objects.create(name=NAME, status='completed', job_id=uuid.uuid4(), interval=5, error='',
                       started=now - dt.timedelta(seconds=secs), completed=now,
                       data={'polled': 385, 'deferred': 0, 'phases': {'poll': 230.0, 'ingest': 120.5, 'ports': 400.2, 'recon': 40.1}})


def status(pk):
    print('STATUS', Job.objects.get(pk=pk).status)


def tidy():
    Job.objects.filter(name=NAME, error__startswith='WDTEST').delete()
    Job.objects.filter(name=NAME, data__has_key='phases', data__polled=385).delete()
    clean()
