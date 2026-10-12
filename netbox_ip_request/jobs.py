import logging

from django.conf import settings

from netbox.jobs import JobRunner, system_job

logger = logging.getLogger('netbox_ip_request')


@system_job(interval=1440)  # 매일 1회 (분 단위)
class DormantIPJob(JobRunner):
    class Meta:
        name = 'IP 장기 미사용 판정 및 격리 해제'

    def run(self, *args, **kwargs):
        from .logic import classify_dormant, release_quarantine
        dormant = classify_dormant()
        released = release_quarantine()
        self.job.data = {'dormant': dormant, 'released': released}


# 주기는 설정(snmp_interval, 분)을 따른다. 0 이면 자동 수집을 등록하지 않음.
_INTERVAL = int(settings.PLUGINS_CONFIG.get('netbox_ip_request', {}).get('snmp_interval', 5) or 0)


class SnmpCollectJob(JobRunner):
    """태그(ipam-arp / ipam-mac) 또는 역할(access-switch) 장치에서 ARP·MAC 테이블을 SNMPv3로 읽어 대장과 대사."""

    class Meta:
        name = 'SNMP ARP/MAC 자동 수집'

    def run(self, *args, **kwargs):
        """한 번 실행 시간을 job_budget() 안으로 묶는다 (넘으면 RQ 가 작업을 죽여 다음 예약이 끊기기 때문).
        오래 못 읽은 장비부터 묶음(동시 수집 수 × 2)으로 읽고 묶음마다 바로 대장에 반영 → 시간이 다 되면
        남은 장비는 다음 실행에서 먼저 읽는다. 마지막에 포트 대조·자동 발견·대사는 항상 실행."""
        import time
        from . import snmp
        if not snmp.credentials():
            self.job.data = {'skipped': 'SNMP 계정 미설정 (env/ipam-snmp.env)'}
            return
        from .room_import import ensure_custom_fields
        from netbox.plugins import get_plugin_config
        cfg = lambda k: get_plugin_config('netbox_ip_request', k)
        ensure_custom_fields()
        import datetime as dt
        from django.utils import timezone
        from core.models import Job
        _db_guard()
        other = Job.objects.filter(name=self.Meta.name, status='running').exclude(pk=self.job.pk).filter(
            started__gte=timezone.now() - dt.timedelta(minutes=_running_limit_minutes(SnmpCollectJob, _INTERVAL)))
        if other.exists():                                         # 같은 수집이 이미 돌고 있으면 겹치지 않게 건너뜀
            self.job.data = {'skipped': f'다른 수집 작업(#{other.first().pk})이 실행 중 — 이번 회차 건너뜀'}
            return
        t0 = time.monotonic()
        budget = job_budget()
        devices, skipped = snmp.targets()
        last = _last_polled()
        devices.sort(key=lambda d: last.get(d['name'], 0))          # 오래 못 읽은 장비부터
        chunk = max(10, 2 * int(cfg('snmp_concurrency') or 20))
        tot = {'devices': len(devices), 'ok': 0, 'failed': 0, 'arp': 0, 'mac': 0, 'unregistered_use': 0,
               'mac_mismatch': 0, 'ip_conflict': 0, 'revived': 0, 'mac_filled': 0}
        done, errors = 0, []
        reserve = max(15, budget // 5)                             # 포트 대조·대사 몫
        for i in range(0, len(devices), chunk):
            left = budget - reserve - (time.monotonic() - t0)
            if left < 20:
                break
            part = devices[i:i + chunk]
            results = snmp.poll(part, device_timeout=min(cfg('snmp_device_timeout') or 120, left),
                                deadline=left)
            t = snmp.ingest(results, ports=False)
            for k, v in t.items():
                if isinstance(v, int) and k != 'devices':
                    tot[k] = tot.get(k, 0) + v
            _mark_polled([d['name'] for d in part])
            errors += [f"{r['name']}({r['host']}): {r['error']}" for r in results if r['error']]
            done += len(part)
        from .logic import check_ports
        tot['polled'] = done
        tot['deferred'] = len(devices) - done                       # 이번에 못 읽고 다음으로 미룬 장비
        tot['ports'] = check_ports() if tot['mac'] else {}
        if cfg('auto_register_discovered'):
            from .logic import register_discovered
            tot['discovered'] = register_discovered()
            if tot['discovered']['created']:
                tot['ports'] = check_ports()
        from .recon import reconcile
        tot['recon'] = reconcile(days=cfg('recon_days') or 30)
        tot['secs'] = round(time.monotonic() - t0, 1)
        tot['budget'] = budget
        tot['skipped'] = [f'{n}: {why}' for n, why in skipped]
        tot['errors'] = errors[:200]
        self.job.data = tot
        logger.info('SNMP 수집: 장비 %s 중 %s (성공 %s, 실패 %s, 다음으로 미룸 %s) %ss',
                    tot['devices'], done, tot['ok'], tot['failed'], tot['deferred'], tot['secs'])


def _db_guard():
    """이 작업의 DB 연결에 잠금·문장 대기 한도 — 다른 연결이 잡은 잠금을 끝없이 기다리다 작업이 멈추는 것을 막음
    (넘으면 오류로 끝나고 NetBox 가 다음 실행을 정상 예약한다)"""
    from django.db import connection
    try:
        with connection.cursor() as c:
            secs = int(settings.PLUGINS_CONFIG.get('netbox_ip_request', {}).get('db_lock_timeout') or 60)
            c.execute(f"SET lock_timeout = '{secs}s'")
            c.execute("SET statement_timeout = '300s'")
    except Exception:
        pass


def job_budget():
    """수집 작업 1회 최대 실행 시간(초) = min(설정 snmp_job_seconds, RQ 시간 제한 - 60).
    RQ 시간 제한(RQ_DEFAULT_TIMEOUT, 기본 300초)을 넘기면 작업자가 작업을 강제로 끝내 NetBox 에 '실행 중'으로
    남고 다음 예약이 끊기므로, 그보다 넉넉히 짧게 끝낸다."""
    rq = int(getattr(settings, 'RQ_DEFAULT_TIMEOUT', 300) or 300)
    want = int(settings.PLUGINS_CONFIG.get('netbox_ip_request', {}).get('snmp_job_seconds') or 240)
    return max(60, min(want, rq - 60))


def _last_polled():
    from django.core.cache import cache
    try:
        return cache.get('ipam_snmp_last_polled') or {}
    except Exception:
        return {}


def _mark_polled(names):
    import time
    from django.core.cache import cache
    try:
        d = cache.get('ipam_snmp_last_polled') or {}
        now = time.time()
        d.update({n: now for n in names})
        cache.set('ipam_snmp_last_polled', d, None)
    except Exception:
        pass


if _INTERVAL > 0:
    SnmpCollectJob = system_job(interval=_INTERVAL)(SnmpCollectJob)


class ReconJob(JobRunner):
    """대장 ↔ 수집 이력(ARP·MAC) 대사만 다시 계산 (SNMP 접속 없음, 수 초).
    장비가 많아 수집 작업이 시간 초과로 끝나도 '장비 대사 결과·판정' 화면이 비지 않도록 따로 돈다."""

    class Meta:
        name = 'IP 대장 대사 계산'

    def run(self, *args, **kwargs):
        from core.models import Job
        from netbox.plugins import get_plugin_config
        if Job.objects.filter(name=SnmpCollectJob.Meta.name, status='running').exists():
            self.job.data = {'skipped': '수집 작업 실행 중 — 그 작업이 끝나며 대사를 계산함'}
            return
        from .recon import reconcile
        _db_guard()
        self.job.data = {'recon': reconcile(days=get_plugin_config('netbox_ip_request', 'recon_days') or 30)}


if _INTERVAL > 0:
    ReconJob = system_job(interval=_INTERVAL)(ReconJob)


_INV = int(settings.PLUGINS_CONFIG.get('netbox_ip_request', {}).get('inventory_interval', 1440) or 0)


class InventorySyncJob(JobRunner):
    """등록된 장비 전체의 인터페이스(상태·설명)·VLAN·대역을 SNMP 로 다시 읽어 맞춤 (기본 하루 1회)"""

    class Meta:
        name = 'SNMP 장비 정보(인터페이스·VLAN) 동기화'

    def run(self, *args, **kwargs):
        from . import snmp
        if not snmp.credentials():
            self.job.data = {'skipped': 'SNMP 계정 미설정'}
            return
        from .inventory import sync_all
        rows = sync_all()
        self.job.data = {'devices': len(rows), 'failed': [r for r in rows if r[1] == '실패'][:200],
                         'ok': sum(1 for r in rows if r[1] != '실패')}


if _INV > 0:
    InventorySyncJob = system_job(interval=_INV)(InventorySyncJob)


# --------------------------------------------------------------------- 수집 상태 (우측 상단 LED)
def collect_status(now=None):
    """SNMP 자동 수집 작업 상태 → {'state': ok|running|down|off, 'label', 'detail'}
    ok      : 최근(주기 3배 안) 수집이 정상 완료
    running : 지금 수집 중
    down    : 마지막 수집이 오류/실패, 오래 수집이 없음, 예약이 처리되지 않음(작업자 멈춤), 작업이 너무 오래 안 끝남
    off     : 자동 수집 꺼짐(IPAM_SNMP_INTERVAL=0)"""
    import datetime as dt
    from django.utils import timezone
    from core.models import Job
    now = now or timezone.now()
    if _INTERVAL <= 0:
        return {'state': 'off', 'label': '자동 수집 꺼짐', 'detail': 'IPAM_SNMP_INTERVAL=0'}
    name = SnmpCollectJob.Meta.name
    qs = Job.objects.filter(name=name)
    loc = lambda t: timezone.localtime(t).strftime('%m-%d %H:%M') if t else '-'
    run = qs.filter(status='running').order_by('-started').first()
    if run and run.started:
        mins = int((now - run.started).total_seconds() // 60)
        if mins > _running_limit_minutes(SnmpCollectJob, _INTERVAL):
            return {'state': 'down', 'label': '중단 의심',
                    'detail': f'수집 작업이 {mins}분째 끝나지 않음 ({loc(run.started)} 시작) — 자동 복구(ipam_watchdog)가 '
                              '5분 안에 작업자를 다시 시작함. 계속되면 netbox-worker 로그 확인'}
        return {'state': 'running', 'label': '수집 중', 'detail': f'{loc(run.started)} 시작 · {mins}분 경과'}
    last = qs.filter(completed__isnull=False).order_by('-completed').first()
    nxt = qs.filter(status__in=['scheduled', 'pending']).order_by('scheduled').first()
    if nxt and (nxt.scheduled or nxt.created) < now - dt.timedelta(minutes=10):
        return {'state': 'down', 'label': '중단',
                'detail': f'예약된 수집({loc(nxt.scheduled or nxt.created)})이 실행되지 않음 — netbox-worker 재시작 필요'}
    if last is None:
        if nxt:
            return {'state': 'running', 'label': '첫 수집 대기', 'detail': f'{loc(nxt.scheduled)} 예정'}
        return {'state': 'down', 'label': '중단', 'detail': '수집 작업이 예약돼 있지 않음 — netbox-worker 를 다시 시작'}
    if last.status in ('errored', 'failed') and '자동 정리' in (last.error or '') and nxt:
        return {'state': 'running', 'label': '복구됨', 'detail': f'멈춘 작업을 자동 정리 — 다음 수집 {loc(nxt.scheduled or nxt.created)}'}
    if last.status in ('errored', 'failed'):
        err = (last.error or '').replace('\n', ' ')[:120]
        if 'Timeout' in err:
            err += ' — 장비가 많으면 RQ_DEFAULT_TIMEOUT 을 늘릴 것'
        return {'state': 'down', 'label': '중단', 'detail': f'{loc(last.completed)} 수집 오류: {err or last.status}'}
    if now - last.completed > dt.timedelta(minutes=max(3 * _INTERVAL, 15)):
        return {'state': 'down', 'label': '중단',
                'detail': f'마지막 수집 {loc(last.completed)} — {int((now - last.completed).total_seconds() // 60)}분째 수집 없음'
                          + ('' if nxt else ' · 다음 수집 예약 없음 → netbox-worker 재시작(끊긴 예약 자동 정리)')}
    d = last.data or {}
    tail = f" · 장비 성공 {d.get('ok', '?')} / 실패 {d.get('failed', '?')}" if isinstance(d, dict) else ''
    return {'state': 'ok', 'label': '정상', 'detail': f'마지막 수집 {loc(last.completed)}{tail}'}


def _plugin_system_jobs():
    from netbox.registry import registry
    return {cls: kw for cls, kw in registry['system_jobs'].items() if cls.__module__.startswith('netbox_ip_request')}


def _running_limit_minutes(cls, iv):
    """'실행 중'이 이 시간(분)을 넘으면 끊긴 것으로 본다"""
    if cls is SnmpCollectJob:
        return job_budget() // 60 + 10           # 수집은 job_budget 안에 끝나도록 만들었으므로
    return max(120, 6 * iv)


def clear_stale_jobs(now=None, on_start=False, overdue_minutes=30):
    """끊긴 자동 작업을 '오류'로 정리 → NetBox 가 새로 예약할 수 있게. 반환: 정리 건수
    - 실행 중인데 한도 시간을 넘긴 작업 (on_start=True: 작업자가 막 켜졌으므로 2분 넘은 '실행 중'은 모두 죽은 작업)
    - 예정 시각이 overdue_minutes 넘게 지났는데 실행되지 않은 대기·예약 작업 (Redis 큐가 비워진 경우 등)"""
    import datetime as dt
    from django.utils import timezone
    from core.models import Job
    now = now or timezone.now()
    n = 0
    for cls, kw in _plugin_system_jobs().items():
        iv = kw.get('interval') or 5
        for j in Job.objects.filter(name=cls.name, status__in=['pending', 'scheduled', 'running']):
            if j.status == 'running':
                lim = 2 if on_start else _running_limit_minutes(cls, iv)
                bad = (j.started or j.created) < now - dt.timedelta(minutes=lim)
            else:
                bad = (j.scheduled or j.created) < now - dt.timedelta(minutes=overdue_minutes)
            if bad:
                j.status, j.completed = 'errored', now
                j.error = ('작업자 재시작으로 끝난 작업' if on_start else '작업자 중단으로 끊긴 작업') + ' — 자동 정리'
                j.save()
                n += 1
    return n


def watchdog_check(now=None):
    """서버 cron(ipam_watchdog.sh, 5분마다)이 부르는 점검. 반환 {'action': ok|requeued|restart, 'reason': ...}
    - 실행 중인 작업이 한도를 넘김 → restart (작업자 프로세스 안에서 멈춘 작업은 작업자 재시작으로만 끝남)
    - 예약이 10분 넘게 실행 안 됨·예약 자체가 없음 → 정리 후 다시 예약(requeued)"""
    import datetime as dt
    from django.utils import timezone
    from core.models import Job
    now = now or timezone.now()
    for cls, kw in _plugin_system_jobs().items():
        lim = _running_limit_minutes(cls, kw.get('interval') or 5)
        j = Job.objects.filter(name=cls.name, status='running').order_by('started').first()
        if j and (j.started or j.created) < now - dt.timedelta(minutes=lim):
            mins = int((now - (j.started or j.created)).total_seconds() // 60)
            return {'action': 'restart', 'reason': f'{cls.name} #{j.pk} 가 {mins}분째 실행 중(한도 {lim}분)'}
    n = clear_stale_jobs(now=now, overdue_minutes=10)
    made = []
    for cls, kw in _plugin_system_jobs().items():
        if not Job.objects.filter(name=cls.name, status__in=['pending', 'scheduled', 'running']).exists():
            cls.enqueue_once(**kw)
            made.append(cls.name)
    if n or made:
        return {'action': 'requeued', 'reason': f'끊긴 작업 {n}건 정리, 다시 예약: {", ".join(made) or "-"}'}
    return {'action': 'ok', 'reason': '정상'}
