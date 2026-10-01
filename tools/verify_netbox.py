"""NetBox + netbox_ip_request 검증 스위트
실행: NETBOX_DIR=/opt/netbox/netbox python verify_netbox.py [반복횟수] [라운드필터]
- HTTP(REST)로 실제 운영 경로(여러 gunicorn 워커 동시성)를 검증하고,
- 시간 경계(180일 등)는 Django ORM으로 직접 로직을 호출해 검증한다.
"""
import datetime as dt
import io
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.environ.get('NETBOX_DIR', '/opt/netbox/netbox'))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'netbox.settings')
import warnings; warnings.filterwarnings('ignore')  # noqa: E702
import django  # noqa: E402
django.setup()

import pandas as pd  # noqa: E402
import requests  # noqa: E402
from django.db import connection, transaction  # noqa: E402
from django.utils import timezone  # noqa: E402
from netaddr import IPNetwork  # noqa: E402

from core.models import ObjectChange  # noqa: E402
from dcim.models import Device, DeviceRole, DeviceType, Interface, Manufacturer, Site  # noqa: E402
from ipam.models import VLAN, IPAddress, Prefix  # noqa: E402
from tenancy.models import Tenant  # noqa: E402
from netbox_ip_request import logic  # noqa: E402
from netbox_ip_request.models import ArpEntry, Discrepancy, IPRequest, MacEntry  # noqa: E402
from users.models import ObjectPermission, Token, User  # noqa: E402

import import_excel as IMP  # noqa: E402
import netparse as P  # noqa: E402
from nb import URL, session  # noqa: E402

results = []
NOW = timezone.now().replace(microsecond=0)


def check(name, cond, detail=''):
    results.append((name, bool(cond), detail))
    if not cond:
        print(f'   FAIL {name} {detail}')


def api(path):
    return URL + path


def reset():
    IPRequest.objects.all().delete(); Discrepancy.objects.all().delete()
    ArpEntry.objects.all().delete(); MacEntry.objects.all().delete()
    IPAddress.objects.all().delete(); Prefix.objects.all().delete(); VLAN.objects.all().delete()
    Interface.objects.all().delete(); Device.objects.all().delete()
    from dcim.models import Location; [l.delete() for l in sorted(Location.objects.all(), key=lambda l: -len(str(l.path)))]
    from dcim.models import Platform
    from ipam.models import VLANGroup
    VLANGroup.objects.all().delete()
    for M in (DeviceType, DeviceRole, Platform, Manufacturer, Site, Tenant):
        M.objects.all().delete()
    User.objects.filter(username='kim').delete()


def prefix(cidr):
    return Prefix.objects.create(prefix=cidr, status='active')


def mk_ip(addr, **cf):
    status = cf.pop('status', 'active'); role = cf.pop('role', '')
    o = IPAddress(address=IPNetwork(addr), status=status, role=role or None)
    o.custom_field_data.update({k: v for k, v in cf.items()})
    o.save(); return o


def iso(days, date=False):
    t = NOW - dt.timedelta(days=days)
    return t.date().isoformat() if date else t.isoformat(timespec='seconds')


# ------------------------------------------------------------------ N1 엑셀 이관
def n1_excel():
    path = '/tmp/nb_import.xlsx'
    nets = pd.DataFrame([['10.10.1.0/24', '101', '본관 1층', '10.10.1.1', 'HQ']], columns=list(IMP.NET_COLS))
    rows = [
        ['10.10.1.10', 'AA-BB-CC-00-00-01', 'pc-001', '김철수', '총무팀', '업무PC', '2024-03-02'],
        ['10.10.1.11', 'aabb.cc00.0002', 'pc-002', '이영희', '총무팀', '업무PC', '2025-01-15'],
        ['10.10.1.10', 'aa:bb:cc:00:00:03', 'pc-003', '박민수', '인사팀', '업무PC', ''],
        ['10.10.1.300', 'aa:bb:cc:00:00:04', 'pc-004', '최지훈', '인사팀', '업무PC', ''],
        ['10.10.2.5', 'aa:bb:cc:00:00:05', 'pc-005', '정하나', '인사팀', '업무PC', ''],
        ['10.10.1.255', 'aa:bb:cc:00:00:06', 'pc-006', '한두리', '인사팀', '업무PC', ''],
        ['10.10.1.12', 'zz:bb:cc:00:00:07', 'pc-007', '오세영', '인사팀', '업무PC', ''],
        ['10.10.1.13', '', 'prn-01', '', '인사팀', '프린터', ''],
        ['10.10.1.1', 'aa:bb:cc:00:00:09', 'pc-009', '윤서', '인사팀', '업무PC', ''],
        ['10.10.1.14', 'aa:bb:cc:00:00:01', 'pc-001b', '김철수', '총무팀', 'VM', ''],
    ]

    def write(r):
        with pd.ExcelWriter(path) as w:
            nets.to_excel(w, sheet_name='서브넷', index=False)
            pd.DataFrame(r, columns=list(IMP.IP_COLS)).to_excel(w, sheet_name='IP', index=False)
    write(rows)
    rep = IMP.validate(path)
    lines = {int(line.split('!')[1]) for line, _ in rep['errors']}
    check('N1.1 오류 7종 정확 탐지(중복·형식·범위 밖·브로드캐스트·MAC·사용자 누락·게이트웨이)',
          lines == {4, 5, 6, 7, 8, 9, 10}, str(rep['errors']))
    check('N1.2 동일 MAC 복수 IP는 경고로만 처리', len(rep['warnings']) == 1, str(rep['warnings']))
    check('N1.3 드라이런은 NetBox 무변경', Prefix.objects.count() == 0 and IPAddress.objects.count() == 0)
    try:
        IMP.commit(rep); ok = False
    except SystemExit:
        ok = True
    check('N1.4 오류가 있으면 --commit 거부, 아무것도 반영 안 됨', ok and Prefix.objects.count() == 0)
    clean = [r for i, r in enumerate(rows) if i + 2 not in lines]
    write(clean)
    n = IMP.commit(IMP.validate(path))
    a = IPAddress.objects.filter(address__net_host='10.10.1.11').first()
    check('N1.5 정제본 이관: IP 3건 + 게이트웨이 예약 + 부서(테넌트) 생성',
          n == 3 and IPAddress.objects.filter(status='active').count() == 3
          and IPAddress.objects.filter(status='reserved').count() == 1 and Tenant.objects.filter(name='총무팀').exists())
    check('N1.6 MAC 정규화·발급일이 사용자 정의 필드로 저장',
          a and a.custom_field_data.get('host_mac') == 'aa:bb:cc:00:00:02' and a.custom_field_data.get('assigned_on') == '2025-01-15',
          str(a and a.custom_field_data))
    rep2 = IMP.validate(path)
    check('N1.7 같은 파일 재이관 시 전건 "이미 존재" 오류', len(rep2['errors']) == 3, str(rep2['errors']))
    s = session()
    r = s.post(api('/ipam/ip-addresses/'), json=[{'address': '10.10.1.21/24'}, {'address': '999.1.1.1/24'}])
    check('N1.8 NetBox bulk POST는 원자적(1건 오류 시 정상 건도 미반영)',
          r.status_code == 400 and not IPAddress.objects.filter(address__net_host='10.10.1.21').exists(), r.status_code)


# ------------------------------------------------------------------ N2 중복 발급 방지
def _threads(n, fn):
    bar = threading.Barrier(n); out = [None] * n

    def run(i):
        bar.wait(); out[i] = fn(i)
    ts = [threading.Thread(target=run, args=(i,)) for i in range(n)]
    [t.start() for t in ts]; [t.join() for t in ts]
    return out


def n2_duplicates():
    p = prefix('10.20.0.0/24')
    sess = [session() for _ in range(48)]
    got, lock = [], threading.Lock()

    def take(i):
        r = sess[i % 48].post(api(f'/ipam/prefixes/{p.pk}/available-ips/'), json={'description': f'u{i}'})
        if r.status_code == 201:
            with lock:
                got.append(r.json()['address'])
        return r.status_code
    with ThreadPoolExecutor(48) as ex:
        codes = list(ex.map(take, range(300)))
    check('N2.1 available-ips API 48동시·300요청: 중복 0, /24 가용 254개 정확 소진',
          len(got) == len(set(got)) == 254 and codes.count(201) == 254, f'{len(got)}/{len(set(got))}')
    check('N2.2 소진 후 초과 46건은 거부(409/400)', len([c for c in codes if c != 201]) == 46)

    dup = 0
    for t in range(10):
        addr = f'10.21.{t}.5/24'
        _threads(24, lambda i: sess[i].post(api('/ipam/ip-addresses/'), json={'address': addr}).status_code)
        dup += IPAddress.objects.filter(address__net_host=addr.split('/')[0]).count() > 1
    check('N2.3 같은 IP 직접 POST 24동시 × 10회: 중복 0 (NetBox API 전역 advisory lock)', dup == 0, dup)

    q = prefix('10.22.0.0/27')  # 가용 30
    reqs = [IPRequest.objects.create(requester=f'u{i}', prefix=q, purpose='동시 승인') for i in range(40)]
    codes = _threads(40, lambda i: sess[i % 48].post(api(f'/plugins/ip-request/requests/{reqs[i].pk}/approve/')).status_code)
    ips = [str(x.address.ip) for x in IPAddress.objects.filter(address__net_host_contained='10.22.0.0/27')]
    check('N2.4 플러그인 승인 40건 동시(가용 30): 30건 발급, 중복 0', codes.count(200) == 30 and len(ips) == len(set(ips)) == 30,
          f'200={codes.count(200)} ips={len(ips)} uniq={len(set(ips))}')
    check('N2.5 초과 10건은 409 + 신청 상태 submitted 유지(승인 롤백)',
          codes.count(409) == 10 and IPRequest.objects.filter(prefix=q, status='submitted').count() == 10)
    r1 = IPRequest.objects.create(requester='same', prefix=prefix('10.23.0.0/24'), purpose='같은 신청')
    codes = _threads(8, lambda i: sess[i].post(api(f'/plugins/ip-request/requests/{r1.pk}/approve/')).status_code)
    check('N2.6 같은 신청을 관리자 8명이 동시 승인 → 1건만 발급', codes.count(200) == 1 and
          IPAddress.objects.filter(address__net_host_contained='10.23.0.0/24').count() == 1, codes)

    # 락 없이 ORM으로 직접 쓰면(플러그인·스크립트가 규칙을 어길 때) 중복이 생기는지 확인
    def orm_write(i, locked):
        from django_pg_utils import advisory_lock
        from django.db import connections
        try:
            addr = '10.24.0.9/24'
            if locked:
                bar2.wait()  # 동시에 출발 → 락에서 줄 세워짐
                with advisory_lock(logic.IP_LOCK), transaction.atomic():
                    o = IPAddress(address=IPNetwork(addr)); o.full_clean(); o.save()
            else:
                with transaction.atomic():
                    o = IPAddress(address=IPNetwork(addr)); o.full_clean(); bar2.wait(); o.save()
            return 'ok'
        except Exception as e:
            return type(e).__name__
        finally:
            connections.close_all()
    prefix('10.24.0.0/24')
    bar2 = threading.Barrier(2)
    _threads(2, lambda i: orm_write(i, False))
    n_unlocked = IPAddress.objects.filter(address__net_host='10.24.0.9').count()
    IPAddress.objects.filter(address__net_host='10.24.0.9').delete()
    bar2 = threading.Barrier(2)
    _threads(2, lambda i: orm_write(i, True))
    n_locked = IPAddress.objects.filter(address__net_host='10.24.0.9').count()
    check('N2.7 [반례] 락 없이 검사→저장하는 코드는 실제로 중복 생성(= 락 규칙이 필요한 이유)', n_unlocked == 2, n_unlocked)
    check('N2.8 같은 코드를 advisory lock으로 감싸면 중복 0', n_locked == 1, n_locked)

    # 락 순서: 세션 advisory lock을 트랜잭션 '안'에서 잡으면, 락 해제 후 커밋 전 틈에 다른 요청이 끼어든다.
    # 커밋을 0.5초 지연시켜 그 틈을 결정적으로 재현한다.
    import time
    from django.db import connections
    from django_pg_utils import advisory_lock

    def order_test(lock_outside):
        addr = '10.25.0.9/24'
        released = threading.Event()

        def writer_a():
            try:
                if lock_outside:
                    with advisory_lock(logic.IP_LOCK):
                        with transaction.atomic():
                            o = IPAddress(address=IPNetwork(addr)); o.full_clean(); o.save()
                            released.set(); time.sleep(0.5)      # 락을 쥔 채 커밋 지연
                else:
                    with transaction.atomic():
                        with advisory_lock(logic.IP_LOCK):
                            o = IPAddress(address=IPNetwork(addr)); o.full_clean(); o.save()
                        released.set(); time.sleep(0.5)          # 락은 풀렸고 커밋은 아직
            finally:
                connections.close_all()

        def writer_b():
            try:
                released.wait()
                with advisory_lock(logic.IP_LOCK):
                    with transaction.atomic():
                        o = IPAddress(address=IPNetwork(addr)); o.full_clean(); o.save()
            except Exception:
                pass
            finally:
                connections.close_all()
        ta, tb = threading.Thread(target=writer_a), threading.Thread(target=writer_b)
        ta.start(); tb.start(); ta.join(); tb.join()
        n = IPAddress.objects.filter(address__net_host='10.25.0.9').count()
        IPAddress.objects.filter(address__net_host='10.25.0.9').delete()
        return n
    prefix('10.25.0.0/24')
    n_wrong = order_test(lock_outside=False)
    n_right = order_test(lock_outside=True)
    check('N2.9 [반례] 락을 트랜잭션 안쪽에서 잡으면 커밋 지연 시 중복 발생', n_wrong == 2, n_wrong)
    check('N2.10 락을 트랜잭션 바깥에서 잡으면(플러그인 방식) 같은 조건에서도 중복 0', n_right == 1, n_right)


# ------------------------------------------------------------------ N3 ARP/MAC 수집·대사
JUNOS_ARP = """MAC Address       Address         Interface                Flags
00:50:56:aa:00:10 10.10.1.10      irb.101 [ae0.0]          none
00:50:56:aa:00:11 10.10.1.11      irb.101 [ae0.0]          none
00:50:56:aa:00:50 10.10.1.50      irb.101 [ae0.0]          none
00:50:56:aa:00:51 10.10.1.51      irb.101 [ae0.0]          none
00:50:56:aa:00:52 10.10.1.51      irb.101 [ae1.0]          none
00:50:56:aa:00:99 172.31.0.9      irb.999                  none
Total entries: 6"""
CISCO_ARP = """Protocol  Address          Age (min)  Hardware Addr   Type   Interface
Internet  10.30.0.1               -   0050.56aa.0101  ARPA   Vlan300
Internet  10.30.0.20              3   0050.56aa.0120  ARPA   Vlan300
Internet  10.30.0.21              0   Incomplete      ARPA"""
JUNOS_MAC = """   Vlan                MAC                 MAC      Age    Logical                NH        RTR
   name                address             flags           interface              Index     ID
   v101                00:50:56:aa:00:10   D        -      ge-0/0/5.0             0         0
   v101                00:50:56:aa:00:11   D        -      ge-0/0/6.0             0         0
   v101                00:50:56:aa:00:50   D        -      ae0.0                  0         0
   v101                00:50:56:aa:00:51   D        -      ae0.0                  0         0
   v101                00:50:56:aa:00:52   D        -      ae0.0                  0         0
   v101                00:50:56:aa:00:10   D        -      ae0.0                  0         0"""


def n3_collect():
    arp = P.parse_junos_arp(JUNOS_ARP); ca = P.parse_cisco_arp(CISCO_ARP)
    check('N3.1 Junos ARP 6건 / Cisco ARP 2건(Incomplete 제외, 점 표기 MAC 정규화)',
          len(arp) == 6 and len(ca) == 2 and ca[1][1] == '00:50:56:aa:01:20')
    s = session()
    f = s.post(api('/plugins/ip-request/arp-ingest/'), json={'device': 'core-ex9208', 'entries': arp}).json()
    check('N3.2 대장 없는 IP 사용 탐지 (.50 .51) — 관리 대역 밖 172.31.0.9는 무시', f.get('unregistered_use') == 2, f)
    check('N3.3 대장 MAC ≠ 관측 MAC 탐지 (.10 .11)', f.get('mac_mismatch') == 2, f)
    check('N3.4 한 IP에 MAC 2개 = IP 충돌 탐지 (.51)', f.get('ip_conflict') == 1, f)
    f2 = s.post(api('/plugins/ip-request/arp-ingest/'), json={'device': 'core-ex9208', 'entries': arp}).json()
    check('N3.5 다음 폴링에서 같은 알림을 중복 적재하지 않음', sum(f2.values()) == 0, f2)
    a = IPAddress.objects.get(address__net_host='10.10.1.10')
    check('N3.6 관측 시 사용자 정의 필드 last_seen 갱신', bool(a.custom_field_data.get('last_seen')))
    check('N3.7 last_seen 갱신은 변경 이력을 남기지 않음(5분 폴링 이력 폭증 방지)',
          not ObjectChange.objects.filter(changed_object_id=a.pk, action='update').exists())
    q = prefix('10.10.9.0/29')
    logic.ingest_arp('core-ex9208', [('10.10.9.1', '00:50:56:dd:00:01', 'irb.109'),
                                     ('10.10.9.2', '00:50:56:dd:00:02', 'irb.109')], now=timezone.now() - dt.timedelta(days=29))
    logic.ingest_arp('core-ex9208', [('10.10.9.3', '00:50:56:dd:00:03', 'irb.109')], now=timezone.now() - dt.timedelta(days=31))
    r1 = IPRequest.objects.create(requester='a', prefix=q, purpose='ARP 가드')
    r2 = IPRequest.objects.create(requester='b', prefix=q, purpose='ARP 가드')
    ip1 = s.post(api(f'/plugins/ip-request/requests/{r1.pk}/approve/')).json().get('ip_address')
    ip2 = s.post(api(f'/plugins/ip-request/requests/{r2.pk}/approve/')).json().get('ip_address')
    check('N3.8 대장엔 비었지만 최근 29일 ARP에 보인 .1 .2는 건너뛰고 31일 전만 보인 .3 발급',
          ip1 == '10.10.9.3/29', ip1)
    check('N3.9 다음 발급은 .4', ip2 == '10.10.9.4/29', ip2)
    s.post(api('/plugins/ip-request/mac-ingest/'), json={'device': 'acc-sw-01', 'entries': P.parse_junos_mac(JUNOS_MAC)})
    loc = s.get(api('/plugins/ip-request/locate/10.10.1.10/')).json()
    check('N3.10 IP→MAC→포트 추적: 업링크 ae0.0 배제, 액세스 포트 ge-0/0/5.0', loc.get('port') == 'ge-0/0/5.0', loc)


# ------------------------------------------------------------------ N4 장기 미사용 경계값
def n4_dormant():
    prefix('10.40.0.0/24')
    mk_ip('10.40.0.10/24', last_seen=iso(179)); mk_ip('10.40.0.11/24', last_seen=iso(180))
    mk_ip('10.40.0.12/24', last_seen=iso(181)); mk_ip('10.40.0.13/24', assigned_on=iso(179, True))
    mk_ip('10.40.0.14/24', assigned_on=iso(181, True)); mk_ip('10.40.0.15/24', assigned_on=iso(30, True))
    mk_ip('10.40.0.16/24', last_seen=iso(400), dormant_exempt=True)
    mk_ip('10.40.0.17/24', last_seen=iso(400), role='vip')
    mk_ip('10.40.0.18/24', last_seen=iso(400), status='reserved')
    site = Site.objects.create(name='HQ', slug='hq'); mf = Manufacturer.objects.create(name='Juniper', slug='juniper')
    dtp = DeviceType.objects.create(manufacturer=mf, model='EX9208', slug='ex9208')
    role = DeviceRole.objects.create(name='core', slug='core')
    dev = Device.objects.create(name='core1', site=site, device_type=dtp, role=role)
    itf = Interface.objects.create(device=dev, name='irb.400', type='virtual')
    o = mk_ip('10.40.0.19/24', last_seen=iso(400)); o.assigned_object = itf; o.save()
    got = set(logic.classify_dormant(now=NOW))
    check('N4.1 경계: 179일=사용 중, 180일·181일=미사용', '10.40.0.10' not in got and {'10.40.0.11', '10.40.0.12'} <= got, got)
    check('N4.2 한 번도 관측 안 된 IP는 발급일 기준(179일 유예, 181일 미사용)',
          '10.40.0.13' not in got and '10.40.0.14' in got)
    check('N4.3 최근 발급 IP(30일) 오판 없음', '10.40.0.15' not in got)
    check('N4.4 판정 제외 플래그·VIP 역할·예약 IP는 대상 아님', not ({'10.40.0.16', '10.40.0.17', '10.40.0.18'} & got))
    check('N4.5 장비 인터페이스에 붙은 IP(장비 자기 IP — ARP에 안 보임) 제외', '10.40.0.19' not in got)
    check('N4.6 재실행 멱등(추가 판정 0)', logic.classify_dormant(now=NOW) == [])
    f = logic.ingest_arp('core', [('10.40.0.12', '00:50:56:bb:00:12', 'irb.400')])
    check('N4.7 dormant IP가 다시 관측되면 자동 active 복귀',
          IPAddress.objects.get(address__net_host='10.40.0.12').status == 'active' and f['revived'] == 1)


# ------------------------------------------------------------------ N5 신청 워크플로·권한
def n5_workflow():
    p = prefix('10.50.0.0/28')  # 가용 14
    s = session()
    r = s.post(api('/plugins/ip-request/requests/'), json={'prefix': p.pk, 'purpose': '개발 서버', 'mac': '00:50:56:cc:00:01'})
    rid = r.json().get('id')
    check('N5.1 신청 접수, 신청자는 로그인 계정으로 자동 기록', r.status_code == 201 and r.json()['requester'] == 'admin', r.text[:200])
    r = s.post(api('/plugins/ip-request/requests/'), json={'prefix': p.pk, 'purpose': '중복', 'mac': '00-50-56-CC-00-01'})
    check('N5.2 같은 MAC 진행 중 신청 중복 거부(표기 달라도)', r.status_code == 400, r.status_code)
    r = s.post(api(f'/plugins/ip-request/requests/{rid}/approve/'))
    check('N5.3 승인 → IP 자동 발급, 신청 상태 allocated', r.status_code == 200 and
          IPRequest.objects.get(pk=rid).status == 'allocated', r.text[:200])
    ip = IPAddress.objects.get(pk=r.json()['ip_id'])
    check('N5.4 발급 IP에 사용자·MAC·발급일 기록', ip.custom_field_data.get('host_mac') == '00:50:56:cc:00:01'
          and ip.custom_field_data.get('ip_user') == 'admin' and ip.custom_field_data.get('assigned_on'))
    r = s.post(api('/plugins/ip-request/requests/'), json={'prefix': p.pk, 'purpose': '추가', 'mac': '00:50:56:cc:00:01'})
    check('N5.5 이미 IP를 받은 MAC으로 재신청 거부', r.status_code == 400, r.status_code)
    r2 = IPRequest.objects.create(requester='x', prefix=p, purpose='반려 테스트')
    c1 = s.post(api(f'/plugins/ip-request/requests/{r2.pk}/reject/'), json={'reason': ''}).status_code
    c2 = s.post(api(f'/plugins/ip-request/requests/{r2.pk}/reject/'), json={'reason': '용도 불명확'}).status_code
    c3 = s.post(api(f'/plugins/ip-request/requests/{r2.pk}/approve/')).status_code
    check('N5.6 반려 사유 필수 / 반려된 신청은 승인 불가', c1 == 409 and c2 == 200 and c3 == 409, (c1, c2, c3))
    for i in range(13):
        IPRequest.objects.create(requester=f'f{i}', prefix=p, purpose='채우기')
    for rq in IPRequest.objects.filter(prefix=p, status='submitted'):
        s.post(api(f'/plugins/ip-request/requests/{rq.pk}/approve/'))
    late = IPRequest.objects.create(requester='late', prefix=p, purpose='소진')
    c = s.post(api(f'/plugins/ip-request/requests/{late.pk}/approve/')).status_code
    check('N5.7 대역 소진 시 승인 실패(409) + 신청은 submitted 유지', c == 409 and
          IPRequest.objects.get(pk=late.pk).status == 'submitted', c)
    kim = User.objects.create(username='kim')
    perm = ObjectPermission.objects.create(name='ip-request-user', actions=['view', 'add'])
    perm.object_types.set([__import__('core.models', fromlist=['ObjectType']).ObjectType.objects.get_for_model(IPRequest)])
    perm.users.add(kim)
    tok = Token(user=kim, version=1); tok.save()
    k = requests.Session(); k.headers.update({'Authorization': f'Token {tok.token}', 'Accept': 'application/json'})
    q = prefix('10.51.0.0/28')
    r = k.post(api('/plugins/ip-request/requests/'), json={'prefix': q.pk, 'purpose': '일반 사용자 신청'})
    c = k.post(api(f"/plugins/ip-request/requests/{r.json().get('id')}/approve/")).status_code
    check('N5.8 일반 사용자: 신청 가능(201), 자기 신청 승인 불가(403)', r.status_code == 201 and c == 403, (r.status_code, c))


# ------------------------------------------------------------------ N6 회수·격리
def n6_reclaim():
    ids = {h: IPAddress.objects.get(address__net_host=h).pk for h in ('10.40.0.11', '10.40.0.14', '10.40.0.10')}
    logic.reclaim(ids['10.40.0.11'], now=NOW); logic.reclaim(ids['10.40.0.14'], now=NOW)
    avail = {str(x) for x in Prefix.objects.get(prefix='10.40.0.0/24').get_available_ips()}
    check('N6.1 격리 중 IP는 NetBox 가용 목록에서 제외', not ({'10.40.0.11', '10.40.0.14'} & avail))
    check('N6.2 격리 만료 전 해제 0건', logic.release_quarantine(now=NOW + dt.timedelta(days=29)) == [])
    f = logic.ingest_arp('core', [('10.40.0.11', '00:50:56:bb:00:11', 'irb.400')], now=NOW + dt.timedelta(days=10))
    rel = logic.release_quarantine(now=NOW + dt.timedelta(days=31))
    check('N6.3 격리 중 다시 관측된 .11은 해제 보류, 미관측 .14만 해제', rel == ['10.40.0.14'], rel)
    check('N6.4 격리 중 사용은 불일치 알림으로 통보', f['unregistered_use'] == 1, f)
    check('N6.5 해제된 IP는 다시 가용 목록에 나타남',
          '10.40.0.14' in {str(x) for x in Prefix.objects.get(prefix='10.40.0.0/24').get_available_ips()})
    try:
        logic.reclaim(ids['10.40.0.10'], now=NOW); ok = False
    except logic.AllocationError:
        ok = True
    check('N6.6 dormant가 아닌 IP 회수 시도 거부', ok)


# ------------------------------------------------------------------ N7 감사
def n7_audit():
    n = ObjectChange.objects.filter(changed_object_type__model='ipaddress', action='create', user_name='admin').count()
    check('N7.1 API/승인으로 만든 IP는 NetBox 변경 이력에 사용자와 함께 기록', n >= 280, n)
    appr = ObjectChange.objects.filter(changed_object_type__model='iprequest', action='update', user_name='admin').count()
    check('N7.2 신청 승인·반려도 변경 이력에 기록', appr >= 30, appr)


# ------------------------------------------------------------------ N8 스위치 포트 대사 (호실 대장 → L2 연동)
def n8_ports():
    site = Site.objects.get_or_create(name='P8', slug='p8')[0]
    mf = Manufacturer.objects.get_or_create(name='G8', slug='g8')[0]
    dtp = DeviceType.objects.get_or_create(manufacturer=mf, model='L2', slug='l2-p8')[0]
    role = DeviceRole.objects.get_or_create(name='acc8', slug='acc8')[0]
    sw = Device.objects.create(name='SW-P8', site=site, device_type=dtp, role=role)
    p9 = Interface.objects.create(device=sw, name='9', type='1000base-t')
    prefix('10.80.0.0/24')
    a = mk_ip('10.80.0.10/24', host_mac='00:11:22:33:80:10', switch=sw.pk, switch_port=p9.pk)
    b = mk_ip('10.80.0.11/24', host_mac='00:11:22:33:80:11', switch=sw.pk)
    c = mk_ip('10.80.0.12/24', host_mac='00:11:22:33:80:12')
    up = [('00:50:56:80:00:0%d' % i, 'v80', 'ae0.0') for i in range(4)]
    logic.ingest_mac('SW-P8', [('00:11:22:33:80:10', 'v80', 'ge-0/0/9.0'), ('00:11:22:33:80:11', 'v80', 'ge-0/0/10.0'),
                               ('00:11:22:33:80:12', 'v80', 'ge-0/0/12.0')] + up, now=NOW)
    r = logic.check_ports()
    b.refresh_from_db(); c.refresh_from_db()
    check('N8.1 엑셀 포트 "9" = 관측 ge-0/0/9.0 로 일치 판정', r['matched'] >= 1 and r['mismatch'] == 0, r)
    check('N8.2 포트가 빈 IP는 MAC 테이블 위치로 자동 채움(단위 .0 제거)',
          Interface.objects.get(pk=b.custom_field_data['switch_port']).name == 'ge-0/0/10'
          and c.custom_field_data.get('switch') == sw.pk, (b.custom_field_data, c.custom_field_data))
    logic.ingest_mac('SW-P8', [('00:11:22:33:80:10', 'v80', 'ge-0/0/15.0')], now=NOW + dt.timedelta(minutes=5))
    r2 = logic.check_ports()
    check('N8.3 단말이 다른 포트로 옮겨지면 포트 불일치 알림', r2['mismatch'] == 1 and
          Discrepancy.objects.filter(kind='port_mismatch', ip='10.80.0.10').exists(), r2)
    check('N8.4 재실행 시 같은 알림 중복 없음', logic.check_ports()['mismatch'] == 0)
    d = mk_ip('10.80.0.20/24')  # 엑셀처럼 MAC 없이 이관된 IP
    f = logic.ingest_arp('core', [('10.80.0.20', '00:11:22:33:80:20', 'irb.80')], now=NOW)
    logic.ingest_mac('SW-P8', [('00:11:22:33:80:20', 'v80', 'ge-0/0/20.0')], now=NOW)
    logic.check_ports(); d.refresh_from_db()
    check('N8.5 MAC 없는 IP: ARP로 MAC 채움 → MAC 테이블로 스위치·포트까지 자동 기록',
          f.get('mac_filled') == 1 and d.custom_field_data.get('host_mac') == '00:11:22:33:80:20'
          and Interface.objects.get(pk=d.custom_field_data['switch_port']).name == 'ge-0/0/20', d.custom_field_data)


# ------------------------------------------------------------------ N9 웹 엑셀 가져오기 로직(스크립트가 호출하는 함수)
def n9_room_import():
    from netbox_ip_request import room_import as R
    path = '/tmp/n9.xlsx'
    cols = list(R.COLS.keys())[:13] + ['스위치 포트 번호', '호스트 이름', '비고', 'PIC ID', '관리자', '전화번호']
    base = ['1-001A', '창업지원단', '창업지원단', '사무실']
    rows = [['1'] + base + ['051', '000'] + [''] * 2 + ['1-001A', '', '', '49.2', '9', '', '', '1-0', '김', ''],
            ['2'] + base + ['051', '162'] + [''] * 2 + ['1-001A', '', '', '49.2', '9', '', '', '1-0', '이', ''],
            ['3'] + base + ['051', '163'] + [''] * 2 + ['1-001A', '', '', '49.2', '', '', '', '1-0', '박', '']]
    hdr = ['연번', '호관호실', '학부(과)', '호관호실명칭', '용도', 'SUBNET ADDRESS', 'IP ADDRESS', 'MAC ADDRESS', 'OUTLET NO',
           'RACK 호관호실', '패치번호', '패치포트번호', '스위치IP ADDRESS', '스위치 포트 번호', '호스트 이름', '비고', 'PIC ID', '관리자', '전화번호']
    pd.DataFrame(rows, columns=hdr).to_excel(path, index=False)
    rep = R.validate(open(path, 'rb'))
    check('N9.1 000=미발급, 오류 0, 포트 없는 행은 경고', not rep['errors'] and rep['blank'] == 1 and len(rep['warnings']) == 1, rep['errors'] + rep['warnings'])
    try:
        with transaction.atomic():
            R.commit(rep['rows'], 'N9SITE')
            raise RuntimeError('rollback')
    except RuntimeError:
        pass
    check('N9.2 커밋 안 함(점검) = 아무것도 남지 않음', not IPAddress.objects.filter(address__net_host='165.246.51.162').exists())
    with transaction.atomic():
        c = R.commit(rep['rows'], 'N9SITE')
    o = IPAddress.objects.get(address__net_host='165.246.51.162')
    check('N9.3 반영: IP 2건 + 호실·스위치·포트 연결', c['created'] == 2 and o.custom_field_data.get('room')
          and Interface.objects.get(pk=o.custom_field_data['switch_port']).name == '9', c)
    with transaction.atomic():
        c2 = R.commit(rep['rows'], 'N9SITE')
    check('N9.4 재업로드: 이미 있는 IP는 건너뜀', c2['skipped'] == 2 and c2['created'] == 0, c2)
    rep['rows'][1]['manager'] = '변경'
    with transaction.atomic():
        c3 = R.commit(rep['rows'], 'N9SITE', update_existing=True)
    o.refresh_from_db()
    check('N9.5 갱신 옵션: 엑셀 값으로 기존 IP 수정', c3['updated'] == 2 and o.custom_field_data.get('ip_user') == '변경', c3)
    bad = R.validate(io.BytesIO(b'not an excel file'))
    check('N9.6 엑셀이 아닌 파일은 오류로 안내', bad['errors'] and not bad['rows'])


ROUNDS = [('N1', n1_excel), ('N2', n2_duplicates), ('N3', n3_collect), ('N4', n4_dormant),
          ('N5', n5_workflow), ('N6', n6_reclaim), ('N7', n7_audit), ('N8', n8_ports), ('N9', n9_room_import)]

if __name__ == '__main__':
    reps = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    only = sys.argv[2].split(',') if len(sys.argv) > 2 else None
    summary = []
    for rep in range(1, reps + 1):
        reset(); results.clear()
        for name, fn in ROUNDS:
            if only and name not in only:
                continue
            try:
                fn()
            except Exception as e:
                import traceback; traceback.print_exc()
                check(f'{name} 예외', False, repr(e))
        passed = sum(ok for _, ok, _ in results)
        summary.append((rep, passed, len(results)))
        print(f'[반복 {rep}] {passed}/{len(results)} 통과', flush=True)
    if reps == 1:
        for n, ok, d in results:
            print(('PASS ' if ok else 'FAIL ') + n)
    print('SUMMARY', summary)
