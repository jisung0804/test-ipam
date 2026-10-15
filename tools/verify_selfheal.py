"""0.6.3 자체 진단·정리 검증 — 단계별 시간 기록, 관측 표 보관 기간 정리, 진단 보고(health_report)
실행: manage.py shell -c "exec(open('.../verify_selfheal.py').read())"   (snmp_simlab 시뮬레이터, 테스트 NetBox 전용)"""
import datetime as dt
import json
import time
import uuid

from django.conf import settings
from django.utils import timezone

from core.models import Job
from dcim.models import Device, DeviceRole, DeviceType, Interface, Manufacturer, Site
from extras.models import Tag
from ipam.models import IPAddress
from netbox_ip_request import jobs as J
from netbox_ip_request.models import ArpEntry, MacEntry

R = {'p': 0, 'f': 0, 'rows': []}
CFG = settings.PLUGINS_CONFIG['netbox_ip_request']


def ok(c, n, x=''):
    R['p' if c else 'f'] += 1
    R['rows'].append({'항목': n, '결과': 'PASS' if c else 'FAIL', '근거': str(x)[:300]})
    print(('  PASS ' if c else '  FAIL ') + n + ('' if c else f'  [{str(x)[:300]}]'))


class FakeJob:
    data = None
    pk = 0


site, _ = Site.objects.get_or_create(slug='sh-site', defaults={'name': 'SH-SITE'})
mf, _ = Manufacturer.objects.get_or_create(slug='sh-mf', defaults={'name': 'SH-MF'})
dtp, _ = DeviceType.objects.get_or_create(slug='sh-dt', manufacturer=mf, defaults={'model': 'SH-DT'})
role, _ = DeviceRole.objects.get_or_create(slug='sh-role', defaults={'name': 'SH-ROLE'})
tag_arp, _ = Tag.objects.get_or_create(slug='ipam-arp', defaults={'name': 'IPAM ARP 수집'})


def cleanup():
    for d in Device.objects.filter(site=site):
        d.primary_ip4 = None; d.save()
    IPAddress.objects.filter(address__net_host_contained='127.0.1.0/24', assigned_object_id__isnull=False,
                             interface__device__site=site).delete()
    Device.objects.filter(site=site).delete()
    ArpEntry.objects.filter(device__startswith='sh-').delete()
    MacEntry.objects.filter(device__startswith='sh-').delete()
    Job.objects.filter(name=J.SnmpCollectJob.Meta.name, error__startswith='SHTEST').delete()


cleanup()
others = list(Device.objects.filter(tags__slug__in=['ipam-arp', 'ipam-mac']).exclude(site=site))
other_tags = {d.pk: list(d.tags.all()) for d in others}
for d in others:
    d.tags.remove(*[t for t in d.tags.all() if t.slug in ('ipam-arp', 'ipam-mac')])
roles_bak = CFG.get('snmp_mac_roles'); CFG['snmp_mac_roles'] = []
try:
    print('== 1. 수집 결과에 단계별 시간 기록')
    for i in range(1, 4):
        d = Device.objects.create(name=f'sh-ok-{i}', site=site, device_type=dtp, role=role, status='active',
                                  local_context_data={'snmp_port': 1161})
        itf = Interface.objects.create(device=d, name='mgmt', type='virtual')
        ip = IPAddress.objects.create(address=f'127.0.1.{100 + i}/32', assigned_object=itf)
        d.primary_ip4 = ip; d.save(); d.tags.add(tag_arp)
    r = J.SnmpCollectJob.__new__(J.SnmpCollectJob); r.job = FakeJob()
    r.run()
    ph = (r.job.data or {}).get('phases', {})
    ok(set(ph) >= {'poll', 'ingest', 'ports', 'recon'} and all(isinstance(v, float) for v in ph.values()),
       f'job.data.phases = {ph}', r.job.data)

    print('== 2. 오래된 ARP·MAC 관측 정리 (보관 기간)')
    now = timezone.now()
    for k, age in enumerate((1, 50, 100, 200)):
        t = now - dt.timedelta(days=age)
        ArpEntry.objects.create(ip=f'10.99.0.{k + 1}', mac=f'02:00:00:00:00:0{k}', device='sh-core', interface='v',
                                first_seen=t, last_seen=t)
        MacEntry.objects.create(mac=f'02:00:00:00:00:0{k}', device='sh-sw', port='1', first_seen=t, last_seen=t)
    n = J.prune_observations(now=now)
    left = sorted((now - x.last_seen).days for x in ArpEntry.objects.filter(device='sh-core'))
    ok(n['arp'] >= 2 and n['mac'] >= 2 and left == [1, 50], f'기본 90일: 100·200일 전 관측 삭제, 1·50일 전은 남김 {n}', left)
    CFG['recon_days'], bak = 120, CFG.get('recon_days')
    ArpEntry.objects.create(ip='10.99.0.9', mac='02:00:00:00:00:09', device='sh-core', interface='v',
                            first_seen=now - dt.timedelta(days=100), last_seen=now - dt.timedelta(days=100))
    J.prune_observations(now=now)
    ok(ArpEntry.objects.filter(ip='10.99.0.9').exists(), '대사 기간(recon_days 120일)이 더 길면 그만큼은 남김')
    CFG['recon_days'] = bak

    print('== 3. 진단 보고(health_report)')
    L = J.health_report()
    txt = '\n'.join(L)
    ok('최근 수집' in txt and '자동 작업 예약' in txt and '관측 표' in txt and '판정:' in txt, '보고 항목(설정·LED·최근 수집·예약·관측 표·판정)', txt[:200])
    t = timezone.now()
    for i in range(2):
        Job.objects.create(name=J.SnmpCollectJob.Meta.name, status='errored', job_id=uuid.uuid4(), interval=5,
                           error='SHTEST Task exceeded maximum timeout value (900 seconds)',
                           started=t - dt.timedelta(minutes=15), completed=t + dt.timedelta(seconds=i + 1))
    txt = '\n'.join(J.health_report())
    ok('확인 필요' in txt and 'RQ 시간 한도 초과' in txt, '최근 오류가 있으면 판정 "확인 필요" + 시간 초과 안내',
       [x for x in txt.split('\n') if '판정' in x or '  - ' in x])
finally:
    CFG['snmp_mac_roles'] = roles_bak
    for pk, tags in other_tags.items():
        Device.objects.get(pk=pk).tags.add(*tags)
    cleanup()
    ArpEntry.objects.filter(device='sh-core').delete()
    MacEntry.objects.filter(device='sh-sw').delete()
json.dump(R['rows'], open('/tmp/claude_selfheal_report.json', 'w'), ensure_ascii=False, indent=1)
print(f"\n결과: PASS {R['p']} / FAIL {R['f']}")
