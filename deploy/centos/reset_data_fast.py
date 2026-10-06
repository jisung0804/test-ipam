"""IPAM 데이터 빠른 초기화 — reset_data.py 와 같은 대상을 SQL TRUNCATE 로 몇 초 안에 지운다

왜 빠른가: reset_data.py 는 NetBox 방식(객체마다 변경 로그·검색 색인·연결 객체 확인)으로 하나씩 지워
           IP·인터페이스가 수만 건이면 몇 시간이 걸린다. 이 스크립트는 테이블을 통째로 비운다(TRUNCATE).
차이     : 지운 객체의 '변경 로그(삭제 기록)'는 남지 않는다. 전부 한 트랜잭션 — 중간에 실패하면 아무것도 안 지워진다.

지우는 것 : IP 신청, 불일치 알림, ARP·MAC 수집 이력, IP 주소, 장비(인터페이스·케이블 등 장비 부품 포함),
            IP 범위, 대역, VLAN, VLAN 그룹, 위치(건물·호실), 소속(테넌트) + 이들에 붙은 태그·검색 색인·저널·북마크
남기는 것 : 사용자·그룹·권한·API 토큰, 스크립트, 사용자 정의 필드, 태그(정의), 사이트, 장비 종류·제조사·역할·플랫폼
안전장치  : 남겨야 할 테이블(사이트·사용자 등)에 데이터가 있는데 함께 비워질 상황이면 아무것도 하지 않고 멈춘다

실행 (/opt/netbox-docker):
  docker compose stop netbox-worker
  # ① 건수·비울 테이블만 보기
  docker compose exec -T netbox /opt/netbox/venv/bin/python /opt/netbox/netbox/manage.py shell \
      < /opt/test-ipam/deploy/centos/reset_data_fast.py
  # ② 실제 삭제
  docker compose exec -T -e CONFIRM=DELETE-ALL netbox /opt/netbox/venv/bin/python /opt/netbox/netbox/manage.py shell \
      < /opt/test-ipam/deploy/centos/reset_data_fast.py
  docker compose start netbox-worker
"""
import os
import time

from django.apps import apps
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.db import connection, transaction

CONFIRM = os.environ.get('CONFIRM') == 'DELETE-ALL'
PURGE_CHANGELOG = os.environ.get('PURGE_CHANGELOG') == '1'

TRUNCATE = ['netbox_ip_request.IPRequest', 'netbox_ip_request.Discrepancy', 'netbox_ip_request.ArpEntry',
            'netbox_ip_request.MacEntry', 'ipam.IPAddress', 'dcim.Device', 'ipam.IPRange', 'ipam.Prefix',
            'ipam.VLAN', 'ipam.VLANGroup', 'dcim.Location']
TENANT = 'tenancy.Tenant'      # 사이트 등이 참조하므로 TRUNCATE 하지 않고 참조를 비운 뒤 DELETE
# 함께 비워지면 안 되는 테이블(데이터가 있을 때). 접두어 또는 정확한 이름
PROTECT_PREFIX = ('auth_', 'users_', 'extras_', 'core_', 'django_', 'taggit_', 'circuits_', 'virtualization_',
                  'wireless_', 'vpn_', 'tenancy_')
PROTECT_EXACT = {'dcim_site', 'dcim_region', 'dcim_sitegroup', 'dcim_manufacturer', 'dcim_devicetype',
                 'dcim_moduletype', 'dcim_devicerole', 'dcim_platform', 'dcim_rack', 'dcim_rackrole',
                 'dcim_racktype', 'dcim_rackreservation', 'dcim_powerpanel', 'dcim_powerfeed',
                 'ipam_vrf', 'ipam_rir', 'ipam_aggregate', 'ipam_role', 'ipam_asn', 'ipam_routetarget'}
# 지운 객체를 가리키는 '붙임' 행(일반 참조) — 함께 지움
ATTACH = {'extras.TaggedItem', 'taggit.TaggedItem', 'extras.CachedValue', 'extras.JournalEntry',
          'extras.ImageAttachment', 'extras.Bookmark', 'extras.Notification', 'extras.Subscription',
          'tenancy.ContactAssignment', 'dcim.MACAddress', 'ipam.FHRPGroupAssignment', 'ipam.Service',
          'dcim.InventoryItem', 'dcim.CableTermination', 'vpn.L2VPNTermination', 'vpn.TunnelTermination'}
SCOPE_NULL = {'virtualization.Cluster', 'wireless.WirelessLAN'}   # 지운 위치를 범위로 쓰면 범위만 비움


def model(label):
    try:
        return apps.get_model(label)
    except LookupError:
        return None


def q(sql, args=None):
    with connection.cursor() as c:
        c.execute(sql, args)
        return c.fetchall() if c.description else None


def rows(table):
    return q(f'SELECT count(*) FROM "{table}"')[0][0]


def cascade_set(tables):
    """TRUNCATE ... CASCADE 가 실제로 비우게 될 테이블 전체 (외래키를 따라 재귀)"""
    got = q("""
        WITH RECURSIVE t(oid) AS (
            SELECT c.oid FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
             WHERE n.nspname = current_schema() AND c.relname = ANY(%s)
            UNION
            SELECT con.conrelid FROM pg_constraint con JOIN t ON con.confrelid = t.oid WHERE con.contype = 'f')
        SELECT relname FROM pg_class WHERE oid IN (SELECT oid FROM t) ORDER BY relname""", [list(tables)])
    return [r[0] for r in got]


targets = [m for m in (model(x) for x in TRUNCATE) if m is not None]
tenant = model(TENANT)
base = [m._meta.db_table for m in targets]
allt = cascade_set(base)
table2model = {m._meta.db_table: m for m in apps.get_models(include_auto_created=True)}

print('== 지울 데이터 건수')
for m in targets + [tenant]:
    print(f'  {m._meta.verbose_name:<22} {rows(m._meta.db_table):>9}')
extra = [t for t in allt if t not in base]
filled = [(t, rows(t)) for t in extra]
print(f'\n== 함께 비워지는 부품 테이블 {len(extra)}개 (데이터 있는 것만)')
for t, n in filled:
    if n:
        print(f'  {t:<40} {n:>9}')
blocked = [(t, n) for t, n in filled if n and (t.startswith(PROTECT_PREFIX) or t in PROTECT_EXACT)]
if blocked:
    print('\n※ 남겨야 할 테이블에 데이터가 있어 함께 비워질 수 있습니다 — 아무것도 지우지 않고 멈춥니다:')
    for t, n in blocked:
        print(f'  {t}  {n}건')
    print('  이 데이터가 필요 없으면 NetBox 화면에서 먼저 지우거나, reset_data.py(느린 방식)를 쓰세요.')
    raise SystemExit

# 일반 참조(태그·검색 색인 등) 정리 대상: 비워지는 테이블 + 소속 의 ContentType
gone_models = {table2model[t] for t in allt if t in table2model} | {tenant}
gone_ct = [ContentType.objects.get_for_model(m, for_concrete_model=False).pk for m in gone_models
           if not m._meta.auto_created]

if not CONFIRM:
    print('\n건수만 확인했습니다. 실제로 지우려면 -e CONFIRM=DELETE-ALL 을 붙여 다시 실행하세요.')
else:
    t0 = time.time()
    print('\n== 삭제')
    with transaction.atomic():
        q("SET LOCAL lock_timeout = '15s'")      # 이전 삭제 작업이 아직 잠그고 있으면 15초 뒤 실패(기다리지 않음)
        q('TRUNCATE ' + ', '.join(f'"{t}"' for t in base) + ' CASCADE')
        print(f'  테이블 {len(allt)}개 비움')
        # 소속: 남는 테이블(사이트 등)의 참조를 비운 뒤 삭제
        tt = tenant._meta.db_table
        for tbl, col, nullable in q("""
                SELECT cl.relname, a.attname, NOT a.attnotnull
                  FROM pg_constraint con
                  JOIN pg_class cl ON cl.oid = con.conrelid
                  JOIN pg_attribute a ON a.attrelid = con.conrelid AND a.attnum = con.conkey[1]
                 WHERE con.contype = 'f' AND con.confrelid = %s::regclass""", [tt]):
            if tbl in allt or tbl == tt:
                continue
            n = q(f'SELECT count(*) FROM "{tbl}" WHERE "{col}" IS NOT NULL')[0][0]
            if n:
                if not nullable:
                    raise RuntimeError(f'{tbl}.{col} 가 소속을 필수로 참조 — 소속 삭제 불가')
                q(f'UPDATE "{tbl}" SET "{col}" = NULL WHERE "{col}" IS NOT NULL')
                print(f'  {tbl}.{col}: 소속 연결 {n}건 해제')
        n = rows(tt)
        q(f'DELETE FROM "{tt}"')
        print(f'  소속 {n}건 삭제')
        # 붙임 행(태그·검색 색인·저널 등)
        for m in apps.get_models():
            label = m._meta.label
            gfks = [f for f in m._meta.private_fields if isinstance(f, GenericForeignKey)]
            if not gfks or m._meta.db_table in allt:
                continue
            for f in gfks:
                col = m._meta.get_field(f.ct_field).column
                tbl = m._meta.db_table
                if label in ATTACH or (PURGE_CHANGELOG and label == 'core.ObjectChange'):
                    n = q(f'WITH d AS (DELETE FROM "{tbl}" WHERE "{col}" = ANY(%s) RETURNING 1) SELECT count(*) FROM d',
                          [gone_ct])[0][0]
                    if n:
                        print(f'  {label}: {n}건 삭제')
                elif label in SCOPE_NULL:
                    fk = m._meta.get_field(f.fk_field).column
                    n = q(f'WITH d AS (UPDATE "{tbl}" SET "{col}" = NULL, "{fk}" = NULL WHERE "{col}" = ANY(%s) '
                          f'RETURNING 1) SELECT count(*) FROM d', [gone_ct])[0][0]
                    if n:
                        print(f'  {label}: 범위 {n}건 비움')
        if PURGE_CHANGELOG:
            oc = model('core.ObjectChange')
            n = rows(oc._meta.db_table)
            q(f'TRUNCATE "{oc._meta.db_table}"')
            print(f'  변경 로그 {n}건 삭제')
    left = {m._meta.verbose_name: rows(m._meta.db_table) for m in targets + [tenant]}
    bad = {k: v for k, v in left.items() if v}
    print(f'\n완료 ({time.time() - t0:.1f}초) — 남은 데이터 없음' if not bad else f'\n※ 남은 데이터: {bad}')
    print('다음: docker compose start netbox-worker  (IPAM_AUTO_REGISTER=0, IPAM_L2_OVERWRITE=0 상태인지 먼저 확인)')
