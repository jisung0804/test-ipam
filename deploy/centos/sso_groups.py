"""SSO·일반 로그인 공통 사용자 그룹 2개를 만든다 (여러 번 실행해도 같은 결과)

  IP신청자      : IP 발급 신청 보기·작성  (SSO 로 처음 들어온 사용자는 REMOTE_AUTH_DEFAULT_GROUPS=IP신청자 로 자동 가입)
  IP발급관리자   : 신청 발급·반려·수정, IP 주소·대역·VLAN·장비·위치 관리, 도구 스크립트 실행

실행 (이관 서버, /opt/netbox-docker):
  docker compose exec -T netbox /opt/netbox/venv/bin/python /opt/netbox/netbox/manage.py shell \
      < /opt/test-ipam/deploy/centos/sso_groups.py
관리자 지정: NetBox > 관리자 > 사용자 > (사용자) > 편집 > 그룹에 'IP발급관리자' 추가
"""
from core.models import ObjectType
from users.models import Group, ObjectPermission

ALL = ['view', 'add', 'change', 'delete']
GROUPS = {
    'IP신청자': [
        ('ipr-requester', ['view', 'add'], ['netbox_ip_request.iprequest']),
    ],
    'IP발급관리자': [
        ('ipr-admin-requests', ALL, ['netbox_ip_request.iprequest', 'netbox_ip_request.discrepancy']),
        ('ipr-admin-ipam', ALL, ['ipam.ipaddress', 'ipam.prefix', 'ipam.vlan', 'ipam.vlangroup']),
        ('ipr-admin-dcim', ALL, ['dcim.device', 'dcim.interface', 'dcim.location', 'dcim.site',
                                 'dcim.devicetype', 'dcim.manufacturer', 'dcim.devicerole', 'dcim.platform']),
        ('ipr-admin-misc', ALL, ['tenancy.tenant', 'extras.tag']),
        ('ipr-admin-scripts', ['view', 'run'], ['extras.script']),
        ('ipr-admin-jobs', ['view'], ['core.job']),
    ],
}

for gname, perms in GROUPS.items():
    g, created = Group.objects.get_or_create(name=gname)
    for pname, actions, models in perms:
        p, _ = ObjectPermission.objects.get_or_create(name=pname, defaults={'actions': actions})
        p.actions, p.enabled = actions, True
        p.save()
        types = []
        for m in models:
            app, model = m.split('.')
            try:
                types.append(ObjectType.objects.get(app_label=app, model=model))
            except ObjectType.DoesNotExist:
                print(f'  (건너뜀: {m} 없음)')
        p.object_types.set(types)
        p.groups.add(g)
    print(f"{'생성' if created else '갱신'}: {gname} — 권한 {len(perms)}개")
print('완료')
