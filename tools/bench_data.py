"""성능 검증용 대량 데이터 (테스트 NetBox 전용) — 10.200.0.0/16 대역, 장치 'bench-sw-NNN'
make(n_ip, n_dev, macs_per_dev) / drop()"""
import datetime as dt
import random

from django.db import connection
from django.utils import timezone

from dcim.models import Device, DeviceRole, DeviceType, Interface, Manufacturer, Site
from ipam.models import IPAddress, Prefix
from netbox_ip_request.models import ArpEntry, Discrepancy, MacEntry

NET = '10.200.0.0/16'


def mac(i):
    return ':'.join(f'{b:02x}' for b in (0x02, 0x42, (i >> 24) & 255, (i >> 16) & 255, (i >> 8) & 255, i & 255))


def drop():
    IPAddress.objects.filter(address__net_host_contained=NET).delete()
    Prefix.objects.filter(prefix=NET).delete()
    MacEntry.objects.filter(device__startswith='bench-sw-').delete()
    ArpEntry.objects.filter(device__startswith='bench-').delete()
    Discrepancy.objects.filter(ip__startswith='10.200.').delete()
    Interface.objects.filter(device__name__startswith='bench-sw-').delete()
    Device.objects.filter(name__startswith='bench-sw-').delete()


def make(n_ip=12000, n_dev=300, macs_per_dev=400, history=3, seed=7):
    """IP n_ip 개(대장 MAC 있음, 일부는 스위치·포트 등록), 장비 n_dev 대, 장비마다 MAC 테이블 macs_per_dev 행 × 수집 회차 history"""
    rnd = random.Random(seed)
    drop()
    site, _ = Site.objects.get_or_create(slug='bench', defaults={'name': 'BENCH'})
    mf, _ = Manufacturer.objects.get_or_create(slug='bench-mf', defaults={'name': 'BENCH-MF'})
    dtp, _ = DeviceType.objects.get_or_create(slug='bench-dt', manufacturer=mf, defaults={'model': 'BENCH-DT'})
    role, _ = DeviceRole.objects.get_or_create(slug='bench-role', defaults={'name': 'BENCH-ROLE'})
    devs = Device.objects.bulk_create([Device(name=f'bench-sw-{d:03d}', site=site, device_type=dtp, role=role,
                                              status='active') for d in range(n_dev)])
    itfs = Interface.objects.bulk_create([Interface(device=d, name=f'GigabitEthernet1/0/{p}', type='1000base-t')
                                          for d in devs for p in range(1, 49)] +
                                         [Interface(device=d, name='Port-channel1', type='lag') for d in devs])
    by = {(i.device_id, i.name): i for i in itfs}
    Prefix.objects.create(prefix=NET, status='active')
    now = timezone.now()
    ips, macs = [], []
    for k in range(n_ip):
        a = f'10.200.{k // 250}.{k % 250 + 2}'
        d = devs[k % n_dev]
        port = f'GigabitEthernet1/0/{k % 48 + 1}'
        cf = {'host_mac': mac(k)}
        r = rnd.random()
        if r < 0.5:                       # 대장 = 실제
            cf.update(switch=d.pk, switch_port=by[(d.pk, port)].pk)
        elif r < 0.6:                     # 대장 포트 다름
            cf.update(switch=d.pk, switch_port=by[(d.pk, 'GigabitEthernet1/0/48' if port != 'GigabitEthernet1/0/48' else 'GigabitEthernet1/0/1')].pk)
        ips.append(IPAddress(address=f'{a}/16', status='active', custom_field_data=cf))
        macs.append((mac(k), d.name, port))
    IPAddress.objects.bulk_create(ips, batch_size=2000)
    rows = []
    for h in range(history):              # 과거 회차까지 쌓인 MAC 테이블
        t = now - dt.timedelta(minutes=15 * h)
        for m, dn, p in macs:
            rows.append(MacEntry(mac=m, device=dn, port=p if h == 0 else p, vlan='10', first_seen=t, last_seen=t))
        for d in devs:                    # 업링크: 회차마다 MAC 많이 보임
            for j in range(macs_per_dev - n_ip // n_dev):
                rows.append(MacEntry(mac=mac(10_000_000 + d.pk * 1000 + j), device=d.name, port='Port-channel1', vlan='10',
                                     first_seen=t, last_seen=t))
    seen = set()
    rows = [r for r in rows if (r.mac, r.device, r.port) not in seen and not seen.add((r.mac, r.device, r.port))]
    MacEntry.objects.bulk_create(rows, batch_size=5000, ignore_conflicts=True)
    # 과거 회차 흉내: 같은 (mac, device, port)는 하나뿐이므로 last_seen 을 회차별로 흩뜨림
    with connection.cursor() as c:
        c.execute("UPDATE netbox_ip_request_macentry SET last_seen = last_seen - (id %% %s) * interval '15 minutes' "
                  "WHERE device LIKE 'bench-sw-%%' AND port = 'Port-channel1'", [history])
    ArpEntry.objects.bulk_create([ArpEntry(ip=f'10.200.{k // 250}.{k % 250 + 2}', mac=mac(k), device='bench-core',
                                           interface='Vlan10', first_seen=now, last_seen=now) for k in range(n_ip)],
                                 batch_size=5000)
    return {'ips': n_ip, 'devices': n_dev, 'macentry': MacEntry.objects.filter(device__startswith='bench-sw-').count()}
