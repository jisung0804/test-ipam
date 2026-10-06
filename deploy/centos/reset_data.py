"""IPAM 데이터 초기화 — 처음부터 다시 쌓기 위해 '데이터'만 지운다

지우는 것 : IP 신청, 수집 이력(ARP·MAC), 불일치 알림, IP 주소, 대역(Prefix), IP 범위, VLAN, VLAN 그룹,
            장비(인터페이스 포함), 위치(건물·호실), 소속(테넌트)
남기는 것 : 사용자·그룹·권한·API 토큰, 업로드한 스크립트, 사용자 정의 필드, 태그, 사이트, 장비 종류·제조사·역할
선택      : PURGE_CHANGELOG=1 이면 변경 로그도 비운다

실행 (/opt/netbox-docker, 5분 수집 작업이 끼어들지 않게 작업자를 먼저 멈춘다):
  docker compose stop netbox-worker
  # ① 건수만 보기 (아무것도 지우지 않음)
  docker compose exec -T netbox /opt/netbox/venv/bin/python /opt/netbox/netbox/manage.py shell \
      < /opt/test-ipam/deploy/centos/reset_data.py
  # ② 실제 삭제
  docker compose exec -T -e CONFIRM=DELETE-ALL netbox /opt/netbox/venv/bin/python /opt/netbox/netbox/manage.py shell \
      < /opt/test-ipam/deploy/centos/reset_data.py
"""
import os

from django.apps import apps
from django.db import transaction

CONFIRM = os.environ.get('CONFIRM') == 'DELETE-ALL'
PURGE_CHANGELOG = os.environ.get('PURGE_CHANGELOG') == '1'

# 지우는 순서 = 참조 관계 순서 (신청이 대역을 보호하므로 신청 먼저, 장비 전에 IP, 대역 전에 VLAN 연결 해제)
TARGETS = [
    ('netbox_ip_request.IPRequest', 'IP 신청'),
    ('netbox_ip_request.Discrepancy', '불일치 알림'),
    ('netbox_ip_request.ArpEntry', 'ARP 수집 이력'),
    ('netbox_ip_request.MacEntry', 'MAC 수집 이력'),
    ('ipam.IPAddress', 'IP 주소'),
    ('dcim.Device', '장비(인터페이스 포함)'),
    ('ipam.IPRange', 'IP 범위'),
    ('ipam.Prefix', '대역'),
    ('ipam.VLAN', 'VLAN'),
    ('ipam.VLANGroup', 'VLAN 그룹'),
    ('dcim.Location', '위치(건물·호실)'),
    ('tenancy.Tenant', '소속'),
]
KEEP = [
    ('users.User', '사용자'), ('users.Group', '그룹'), ('users.ObjectPermission', '권한'), ('users.Token', 'API 토큰'),
    ('extras.ScriptModule', '스크립트 파일'), ('extras.CustomField', '사용자 정의 필드'), ('extras.Tag', '태그'),
    ('dcim.Site', '사이트'),
]


def model(label):
    try:
        return apps.get_model(label)
    except LookupError:
        return None


print('== 현재 건수')
for label, name in TARGETS + ([('core.ObjectChange', '변경 로그')] if PURGE_CHANGELOG else []):
    m = model(label)
    if m is not None:
        print(f'  지움  {name:<18} {m.objects.count():>8}')
for label, name in KEEP:
    m = model(label)
    if m is not None:
        print(f'  유지  {name:<18} {m.objects.count():>8}')

if not CONFIRM:
    print('\n건수만 확인했습니다. 실제로 지우려면 -e CONFIRM=DELETE-ALL 을 붙여 다시 실행하세요.')
else:
    print('\n== 삭제')
    with transaction.atomic():
        Device = model('dcim.Device')
        Device.objects.update(primary_ip4=None, primary_ip6=None, oob_ip=None)
        model('ipam.Prefix').objects.update(vlan=None)
        for label, name in TARGETS:
            m = model(label)
            if m is None:
                continue
            if label == 'dcim.Location':           # 트리 구조: 아래(호실)부터
                n = 0
                while m.objects.exists():
                    leaves = m.objects.filter(children__isnull=True)
                    n += leaves.count()
                    leaves.delete()
            else:
                n = m.objects.count()
                m.objects.all().delete()
            print(f'  {name:<18} {n:>8} 삭제')
        if PURGE_CHANGELOG:
            oc = model('core.ObjectChange')
            n = oc.objects.count(); oc.objects.all().delete()
            print(f'  변경 로그          {n:>8} 삭제')
    left = {name: model(label).objects.count() for label, name in TARGETS if model(label) is not None}
    bad = {k: v for k, v in left.items() if v}
    print('\n완료 — 남은 데이터 없음' if not bad else f'\n※ 남은 데이터: {bad}')
    print('다음: docker compose start netbox-worker  (IPAM_AUTO_REGISTER=0, IPAM_L2_OVERWRITE=0 상태인지 먼저 확인)')
