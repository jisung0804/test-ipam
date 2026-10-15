"""SNMP 수집 후처리 성능·정확도 검증 (테스트 NetBox 전용 — 10.200.0.0/16 대량 데이터를 만들고 지움)
  1) 0.5.5(하나씩 조회) ↔ 0.6.3(일괄) 결과가 같은지: 포트 대조(check_ports), ARP 적재(ingest_arp)
  2) 데이터가 커질 때 걸리는 시간 (0.5.5 는 IP·MAC 테이블에 비례해 늘어 결국 RQ 시간 한도를 넘김)
실행: cd netbox/netbox && python manage.py shell -c "exec(open('/home/claude/netbox-ip-request/tools/verify_perf.py').read())"
결과: 화면 + /tmp/claude_perf_report.json"""
import datetime as dt
import json
import sys
import time

from django.db import connection, transaction
from django.utils import timezone

sys.path.insert(0, '/home/claude/netbox-ip-request/tools')
import bench_data as B  # noqa: E402
import legacy_v055 as OLD  # noqa: E402

from ipam.models import IPAddress  # noqa: E402
from netbox_ip_request import logic as NEW  # noqa: E402
from netbox_ip_request.models import ArpEntry, Discrepancy  # noqa: E402

R = {'p': 0, 'f': 0, 'rows': [], 'timing': []}


def ok(c, n, x=''):
    R['p' if c else 'f'] += 1
    R['rows'].append({'항목': n, '결과': 'PASS' if c else 'FAIL', '근거': str(x)[:300]})
    print(('  PASS ' if c else '  FAIL ') + n + ('' if c else f'  [{str(x)[:300]}]'))


class Rollback(Exception):
    pass


def snapshot():
    d = set(Discrepancy.objects.filter(ip__startswith='10.200.', resolved=False).values_list('kind', 'ip', 'detail'))
    from dcim.models import Device, Interface
    dn = dict(Device.objects.values_list('pk', 'name'))
    pn = dict(Interface.objects.filter(device__name__startswith='bench-sw-').values_list('pk', 'name'))
    num = lambda x: int(x) if x not in (None, '') and str(x).isdigit() else x
    ips = {str(a): (st, cf.get('host_mac'), dn.get(num(cf.get('switch')), cf.get('switch')),   # id 대신 이름으로 비교
                    pn.get(num(cf.get('switch_port')), cf.get('switch_port')))                   # (되돌린 생성분의 id 차이 무시)
           for a, st, cf in IPAddress.objects.filter(address__net_host_contained=B.NET)
           .values_list('address', 'status', 'custom_field_data')}
    arp = set(ArpEntry.objects.filter(device__startswith='bench-').values_list('ip', 'mac', 'device', 'interface'))
    return d, ips, arp


def run_isolated(fn):
    """fn 을 실행하고 결과·변경 상태를 기록한 뒤 되돌림 (두 구현을 같은 출발점에서 비교)"""
    out = {}
    try:
        with transaction.atomic():
            t = time.monotonic()
            out['ret'] = fn()
            out['secs'] = round(time.monotonic() - t, 2)
            out['state'] = snapshot()
            raise Rollback
    except Rollback:
        pass
    return out


def nq(fn):
    from django.test.utils import CaptureQueriesContext
    with CaptureQueriesContext(connection) as c:
        fn()
    return len(c.captured_queries)


try:
    print('== 1. 포트 대조(check_ports) 결과 비교 — IP 3,000 · 장비 100 · MAC 테이블 회차 3')
    info = B.make(3000, 100, 60)
    # 엣지 케이스: 스위치 id 를 문자열로 저장한 행, 존재하지 않는 장치 id, 이름이 다른 포트(숫자만)
    rows = list(IPAddress.objects.filter(address__net_host_contained=B.NET).order_by('pk')[:40])
    for i, o in enumerate(rows):
        cf = o.custom_field_data
        if i < 10 and cf.get('switch'):
            cf['switch'] = str(cf['switch'])
        elif i < 20:
            cf['switch'], cf['switch_port'] = 999999, None
        elif i < 30 and cf.get('switch_port'):
            cf['switch_port'] = 999998
    IPAddress.objects.bulk_update(rows, ['custom_field_data'])
    for fill in (False, True):
        o = run_isolated(lambda: OLD.check_ports(fill_empty=fill))
        n = run_isolated(lambda: NEW.check_ports(fill_empty=fill))
        ok(o['ret'] == n['ret'], f'fill_empty={fill}: 집계 같음 {n["ret"]}', (o['ret'], n['ret']))
        ok(o['state'][0] == n['state'][0], f'fill_empty={fill}: 만든 불일치 알림 {len(n["state"][0])}건 같음',
           (len(o['state'][0]), len(n['state'][0]), list(o['state'][0] ^ n['state'][0])[:3]))
        ok(o['state'][1] == n['state'][1], f'fill_empty={fill}: IP 의 MAC·스위치·포트 값 같음',
           [(k, o['state'][1][k], n['state'][1].get(k)) for k in o['state'][1] if o['state'][1][k] != n['state'][1].get(k)][:3])
        R['timing'].append({'작업': f'check_ports(fill={fill}) IP 3,000', '0.5.5 초': o['secs'], '0.6.3 초': n['secs']})
        print(f'     시간: 0.5.5 {o["secs"]}초 → 0.6.3 {n["secs"]}초')
    # 미해결 알림이 이미 있으면 다시 만들지 않음 (두 번째 실행)
    with transaction.atomic():
        NEW.check_ports(fill_empty=False)
        r2 = NEW.check_ports(fill_empty=False)
        transaction.set_rollback(True)
    ok(r2['mismatch'] == 0, '두 번째 실행: 이미 있는 미해결 알림은 다시 만들지 않음', r2)

    print('== 2. ARP 적재(ingest_arp) 결과 비교')
    now = timezone.now()
    ips = list(IPAddress.objects.filter(address__net_host_contained=B.NET).order_by('pk'))
    # 상태 섞기: 휴면 20, 격리 20, MAC 빈칸 20
    for o in ips[100:120]:
        o.status = 'dormant'
    for o in ips[120:140]:
        o.status = 'quarantine'
    for o in ips[140:160]:
        o.custom_field_data.pop('host_mac', None)
    IPAddress.objects.bulk_update(ips[100:160], ['status', 'custom_field_data'])
    ent = []
    for k in range(0, 2600):                                   # 기존 항목(같은 MAC), 일부 인터페이스 바뀜
        ent.append((f'10.200.{k // 250}.{k % 250 + 2}', B.mac(k), 'Vlan20' if k % 50 == 0 else 'Vlan10'))
    for k in range(2600, 2650):                                # 다른 MAC(대장 불일치)
        ent.append((f'10.200.{k // 250}.{k % 250 + 2}', B.mac(k + 500000), 'Vlan10'))
    for k in range(2650, 2670):                                # IP 충돌(MAC 2개)
        a = f'10.200.{k // 250}.{k % 250 + 2}'
        ent += [(a, B.mac(k), 'Vlan10'), (a, B.mac(k + 600000), 'Vlan10')]
    for k in range(20):                                        # 대장 없음(관리 대역 안 / 밖)
        ent.append((f'10.200.250.{k + 10}', B.mac(700000 + k), 'Vlan10'))
        ent.append((f'172.31.250.{k + 10}', B.mac(800000 + k), 'Vlan10'))
    ent.append((ent[0][0], ent[0][1], 'Vlan99'))               # 같은 항목 두 번(마지막 인터페이스)
    o = run_isolated(lambda: OLD.ingest_arp('bench-core', ent, now=now))
    n = run_isolated(lambda: NEW.ingest_arp('bench-core', ent, now=now))
    ok(o['ret'] == n['ret'], f'집계 같음 {n["ret"]}', (o['ret'], n['ret']))
    ok(o['state'][0] == n['state'][0], f'불일치 알림 {len(n["state"][0])}건 같음', list(o['state'][0] ^ n['state'][0])[:3])
    ok(o['state'][1] == n['state'][1], 'IP 상태·MAC 값 같음 (휴면→사용 복귀, 빈 MAC 채움)',
       [k for k in o['state'][1] if o['state'][1][k] != n['state'][1].get(k)][:3])
    ok(o['state'][2] == n['state'][2], f'ARP 관측 {len(n["state"][2])}행 같음 (새 항목·인터페이스 변경 포함)',
       list(o['state'][2] ^ n['state'][2])[:3])
    R['timing'].append({'작업': f'ingest_arp 항목 {len(ent):,}', '0.5.5 초': o['secs'], '0.6.3 초': n['secs']})
    print(f'     시간: 0.5.5 {o["secs"]}초 → 0.6.3 {n["secs"]}초')
    with transaction.atomic():
        q_old = nq(lambda: OLD.ingest_arp('bench-core', ent, now=now))
        transaction.set_rollback(True)
    with transaction.atomic():
        q_new = nq(lambda: NEW.ingest_arp('bench-core', ent, now=now))
        transaction.set_rollback(True)
    ok(q_new * 10 < q_old, f'DB 쿼리 수 {q_old:,} → {q_new:,}', (q_old, q_new))

    print('== 3. 큰 데이터 — IP 15,000 · 장비 300 · MAC 테이블 회차 3 (0.6.3 만, 0.5.5 는 아래 추정)')
    t = time.monotonic()
    info = B.make(15000, 300, 400)
    print('     데이터', info, f'{time.monotonic() - t:.0f}초')
    n = run_isolated(lambda: NEW.check_ports())
    R['timing'].append({'작업': f"check_ports IP 15,000 · MAC 테이블 {info['macentry']:,}행", '0.5.5 초': None,
                        '0.6.3 초': n['secs']})
    ok(n['secs'] < 60, f"포트 대조 {n['secs']}초 (IP 15,000)", n['ret'])
    big = [(f'10.200.{k // 250}.{k % 250 + 2}', B.mac(k), 'Vlan10') for k in range(15000)]
    n = run_isolated(lambda: NEW.ingest_arp('bench-core', big, now=timezone.now()))
    R['timing'].append({'작업': 'ingest_arp 항목 15,000 (코어 스위치 1대)', '0.5.5 초': None, '0.6.3 초': n['secs']})
    ok(n['secs'] < 30, f"ARP 적재 15,000건 {n['secs']}초", n['ret'])
    # 0.5.5 추정: IP 1,000 개만 돌려 비례 계산
    sub = list(IPAddress.objects.filter(address__net_host_contained=B.NET).values_list('pk', flat=True)[1000:])
    with transaction.atomic():
        IPAddress.objects.filter(pk__in=sub).update(status='reserved')
        t = time.monotonic()
        OLD.check_ports(fill_empty=False)
        s1 = time.monotonic() - t
        transaction.set_rollback(True)
    est = round(s1 * 15, 0)
    R['timing'].append({'작업': 'check_ports IP 15,000 — 0.5.5 추정(IP 1,000 실측 × 15)', '0.5.5 초': est, '0.6.3 초': None})
    print(f'     0.5.5 포트 대조: IP 1,000 → {s1:.1f}초, 15,000 이면 약 {est:.0f}초 ({est / 60:.0f}분)')
    ok(est > 20 * max(R['timing'][-3]['0.6.3 초'], 1), f'IP 15,000 포트 대조: 0.5.5 약 {est:.0f}초 → 0.6.3 '
       f"{R['timing'][-3]['0.6.3 초']}초 (IP·MAC 테이블이 늘수록 0.5.5 는 비례해 길어짐)", est)
finally:
    B.drop()
json.dump({'rows': R['rows'], 'timing': R['timing']}, open('/tmp/claude_perf_report.json', 'w'), ensure_ascii=False, indent=1)
print(f"\n결과: PASS {R['p']} / FAIL {R['f']}")
