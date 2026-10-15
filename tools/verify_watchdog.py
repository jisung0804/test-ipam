"""SNMP 자동 수집 안정화 검증 — 실행 시간 한도·묶음 수집·끊긴 작업 자동 복구(워치독)
실행: manage.py shell -c "exec(open('.../verify_watchdog.py').read())"   (snmp_simlab 시뮬레이터, 테스트 NetBox 전용)
결과는 화면과 /tmp/claude_watchdog_report.json 에 남긴다 (테스트 보고서용)"""
import datetime as dt
import json
import time
import uuid

from django.conf import settings
from django.utils import timezone

from core.models import Job
from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Site
from extras.models import Tag
from ipam.models import IPAddress
from dcim.models import Interface
from netbox_ip_request import jobs as J, snmp

R = {'p': 0, 'f': 0, 'rows': []}
CFG = settings.PLUGINS_CONFIG['netbox_ip_request']
NAME = J.SnmpCollectJob.Meta.name


def ok(c, n, x=''):
    R['p' if c else 'f'] += 1
    R['rows'].append({'항목': n, '결과': 'PASS' if c else 'FAIL', '근거': str(x)[:300]})
    print(('  PASS ' if c else '  FAIL ') + n + ('' if c else f'  [{str(x)[:300]}]'))


class FakeJob:
    data = None
    pk = 0


def run_collect():
    r = J.SnmpCollectJob.__new__(J.SnmpCollectJob)
    r.job = FakeJob()
    t = time.monotonic()
    r.run()
    return r.job.data, time.monotonic() - t


# ---- 준비: 장비 60대 (응답 40 + 응답 없음 20)
site, _ = Site.objects.get_or_create(slug='wd-site', defaults={'name': 'WD-SITE'})
mf, _ = Manufacturer.objects.get_or_create(slug='wd-mf', defaults={'name': 'WD-MF'})
dtp, _ = DeviceType.objects.get_or_create(slug='wd-dt', manufacturer=mf, defaults={'model': 'WD-DT'})
role, _ = DeviceRole.objects.get_or_create(slug='wd-role', defaults={'name': 'WD-ROLE'})
tag_arp, _ = Tag.objects.get_or_create(slug='ipam-arp', defaults={'name': 'IPAM ARP 수집'})


def cleanup():
    for d in Device.objects.filter(site=site):
        d.primary_ip4 = None; d.save()
    IPAddress.objects.filter(address__net_host_contained='127.0.1.0/24').delete()
    IPAddress.objects.filter(address__net_host_contained='127.0.3.0/24').delete()
    Device.objects.filter(site=site).delete()


cleanup()
# 다른 검증이 남긴 수집 대상 장비는 이번 검증 동안 수집 대상에서 뺀다 (태그·역할로 고름)
others = list(Device.objects.filter(tags__slug__in=['ipam-arp', 'ipam-mac']).exclude(site=site).values_list('pk', flat=True))
other_tags = {d.pk: list(d.tags.all()) for d in Device.objects.filter(pk__in=others)}
for d in Device.objects.filter(pk__in=others):
    d.tags.remove(*[t for t in d.tags.all() if t.slug in ('ipam-arp', 'ipam-mac')])
roles_bak = CFG.get('snmp_mac_roles')
CFG['snmp_mac_roles'] = []
for n, net in [(f'wd-ok-{i:02d}', f'127.0.1.{i}') for i in range(1, 41)] + [(f'wd-dead-{i:02d}', f'127.0.3.{i}') for i in range(1, 21)]:
    d = Device.objects.create(name=n, site=site, device_type=dtp, role=role, status='active',
                              local_context_data={'snmp_port': 1161})
    i = Interface.objects.create(device=d, name='mgmt', type='virtual')
    ip = IPAddress.objects.create(address=f'{net}/32', assigned_object=i)
    d.primary_ip4 = ip; d.save(); d.tags.add(tag_arp)
from django.core.cache import cache
cache.delete('ipam_snmp_last_polled')
saved = list(Job.objects.filter(name__in=[c.name for c in J._plugin_system_jobs()]).exclude(status__in=['completed', 'errored', 'failed'])
             .values_list('pk', 'status'))
ORIG_BUDGET = J.job_budget
CFG_BAK = {k: CFG.get(k) for k in ('snmp_job_seconds', 'snmp_device_timeout', 'snmp_concurrency')}
try:
    print('== 1. 묶음 하나의 전체 시간 한도 (응답 없는 장비가 있어도 그 시간에 끝남)')
    dead = [{'name': f'x{i}', 'host': f'127.0.3.{i}', 'port': 1161, 'arp': True, 'mac': False} for i in range(1, 11)]
    t = time.monotonic()
    res = snmp.poll(dead, device_timeout=120, deadline=3)
    secs = time.monotonic() - t
    ok(secs < 8 and len(res) == 10 and all(r['error'] for r in res), f'장비 10대(응답 없음), 묶음 한도 3초 → {secs:.1f}초에 끝남', res[0]['error'])

    print('== 2. 실행 시간 한도(job_budget)')
    ok(J.job_budget() == min(240, settings.RQ_DEFAULT_TIMEOUT - 60),
       f"RQ_DEFAULT_TIMEOUT {settings.RQ_DEFAULT_TIMEOUT}초 → 수집 1회 최대 {J.job_budget()}초(기본 240초, RQ 한도-60초를 넘지 않음)", J.job_budget())
    CFG['snmp_job_seconds'] = 9999
    ok(J.job_budget() == settings.RQ_DEFAULT_TIMEOUT - 60, '설정을 크게 잡아도 RQ 한도를 넘지 않음', J.job_budget())

    print('== 3. 시간이 모자라면 일부만 읽고 다음 회차로 (오래 못 읽은 장비부터)')
    CFG.update(snmp_job_seconds=60, snmp_device_timeout=20, snmp_concurrency=5)
    J.job_budget.__globals__  # noqa
    _b = J.job_budget
    J.job_budget = lambda: 40
    d1, s1 = run_collect()
    ok(s1 < 40 + 10 and d1['deferred'] > 0 and d1['polled'] + d1['deferred'] == 60 and 'recon' in d1,
       f"1회차: 한도 40초 → {s1:.0f}초에 끝, 60대 중 {d1['polled']}대 읽고 {d1['deferred']}대 다음으로, 대사까지 실행",
       {k: d1.get(k) for k in ('polled', 'deferred', 'ok', 'failed', 'secs')})
    last = J._last_polled()
    first_round = {n for n in last}
    d2, s2 = run_collect()
    last2 = J._last_polled()
    newly = set(last2) - first_round
    ok(newly and len(newly) >= min(d1['deferred'], d2['polled']) - 1, f'2회차: 1회차에 못 읽은 장비부터 {len(newly)}대 읽음', (len(newly), d2['polled']))
    J.job_budget = lambda: 400
    CFG.update(snmp_device_timeout=10, snmp_concurrency=20)
    d3, s3 = run_collect()
    ok(d3['deferred'] == 0 and d3['ok'] == 40 and d3['failed'] == 20, f"한도가 충분하면 한 번에 60대 모두({s3:.0f}초, 성공 40·실패 20)",
       {k: d3.get(k) for k in ('polled', 'deferred', 'ok', 'failed')})
    J.job_budget = _b

    print('== 4. 워치독 점검(watchdog_check)')
    Job.objects.filter(name__in=[c.name for c in J._plugin_system_jobs()], status__in=['pending', 'scheduled', 'running']).delete()
    now = timezone.now()
    w = J.watchdog_check()
    # 실제 작업자가 떠 있으면 곧바로 실행돼 '완료'로 바뀔 수 있으므로 점검 시각 이후 만든 행을 센다
    live = {c.name: Job.objects.filter(name=c.name, created__gte=now).count() for c in J._plugin_system_jobs()}
    ok(w['action'] == 'requeued' and all(v == 1 for v in live.values()), '예약이 하나도 없음 → 4개 자동 작업 다시 예약', (w, live))
    ok(J.watchdog_check()['action'] == 'ok', '다시 점검하면 정상(중복 예약 없음)', J.watchdog_check())
    Job.objects.filter(name=NAME, status__in=['pending', 'scheduled']).delete()
    lost = Job.objects.create(name=NAME, status='scheduled', job_id=uuid.uuid4(), interval=5, scheduled=now - dt.timedelta(minutes=15))
    w = J.watchdog_check()
    lost.refresh_from_db()
    ok(w['action'] == 'requeued' and lost.status == 'errored'
       and Job.objects.filter(name=NAME, status__in=['pending', 'scheduled', 'running', 'completed']).exclude(pk=lost.pk).exists(),
       '예약 시각이 15분 지났는데 실행 안 됨(대기열 비워짐) → 정리 후 다시 예약', w)
    Job.objects.filter(name=NAME, status__in=['pending', 'scheduled']).delete()
    stuck = Job.objects.create(name=NAME, status='running', job_id=uuid.uuid4(), interval=5, started=now - dt.timedelta(minutes=93))
    w = J.watchdog_check()
    ok(w['action'] == 'restart' and '93분' in w['reason'], '수집이 93분째 실행 중(화면 오류 상황) → 작업자 재시작 판정', w)
    stuck.started = now - dt.timedelta(minutes=3); stuck.save()
    ok(J.watchdog_check()['action'] in ('ok', 'requeued'), '정상 범위(3분) 실행 중이면 건드리지 않음', J.watchdog_check())
    stuck.started = now - dt.timedelta(minutes=93); stuck.save()

    print('== 5. 작업자 재시작 직후 정리(on_start)')
    n = J.clear_stale_jobs(on_start=True)
    stuck.refresh_from_db()
    ok(n >= 1 and stuck.status == 'errored' and '재시작' in stuck.error, '작업자가 켜질 때 남아 있던 실행 중 작업 → 오류로 정리(새로 예약되게)', (n, stuck.status))
    print('== 6. DB 잠금 대기 한도 (다른 연결이 잠금을 쥐고 있어도 작업이 멈추지 않음)')
    import psycopg
    from django.db import connection
    o = IPAddress.objects.filter(address__net_host_contained='127.0.1.0/24').first()
    other = psycopg.connect(**{k: v for k, v in connection.get_connection_params().items() if k in ('dbname', 'user', 'password', 'host', 'port')})
    other.execute('BEGIN'); other.execute('SELECT id FROM ipam_ipaddress WHERE id = %s FOR UPDATE', [o.pk])
    CFG['db_lock_timeout'] = 3
    rj = J.ReconJob.__new__(J.ReconJob); rj.job = FakeJob()
    t = time.monotonic(); err = ''
    try:
        from django.db import transaction
        with transaction.atomic():
            J._db_guard()
            IPAddress.objects.filter(pk=o.pk).update(description='lock-test')
    except Exception as e:
        err = type(e).__name__ + ': ' + str(e)[:80]
    secs = time.monotonic() - t
    other.rollback(); other.close()
    CFG['db_lock_timeout'] = 60
    with connection.cursor() as c:
        c.execute("SET lock_timeout = 0"); c.execute("SET statement_timeout = 0")
    ok(err and 'lock' in err.lower() and secs < 10, f'잠긴 행 수정 → {secs:.1f}초 만에 오류로 끝남(무한 대기 없음)', err)
    st = J.collect_status()
    ok(st['state'] in ('down', 'running', 'ok'), f"LED 상태 계산 정상: {st['state']} — {st['detail'][:60]}", st)
finally:
    J.job_budget = ORIG_BUDGET
    CFG.update(CFG_BAK)
    CFG['snmp_mac_roles'] = roles_bak
    for pk, tags in other_tags.items():
        Device.objects.get(pk=pk).tags.add(*tags)
    cleanup()
json.dump(R['rows'], open('/tmp/claude_watchdog_report.json', 'w'), ensure_ascii=False, indent=1)
print(f"\n결과: PASS {R['p']} / FAIL {R['f']}")
