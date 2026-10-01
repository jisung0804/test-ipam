"""화면 표시 이름(라벨) 바꾸기 — NetBox 코어 코드는 건드리지 않고 시작할 때 이름만 덮어쓴다.

바꿀 이름은 configuration/plugins.py 의 PLUGINS_CONFIG['netbox_ip_request']['field_labels'] 에서 정한다.
  예) {'tenant': '소속', 'description': '호실명'}
적용 범위: IP 주소 목록의 열 제목, 테이블 설정의 열 이름, 추가/편집/일괄편집/필터/가져오기 화면의 칸 이름.
(IP 상세 화면의 왼쪽 표 제목은 NetBox 템플릿에 고정돼 있어 바뀌지 않는다)
사용자 정의 필드(호관호실, MAC ADDRESS 등)의 이름은 NetBox 화면 > 사용자 정의 > 사용자 정의 필드 에서 바꾼다.
"""
import logging

logger = logging.getLogger('netbox_ip_request')


def apply_labels(labels):
    if not labels:
        return
    from ipam import models, tables
    from ipam.forms import bulk_edit, bulk_import, filtersets, model_forms

    targets = [('IPAddress', tables.IPAddressTable,
                [model_forms.IPAddressForm, model_forms.IPAddressBulkAddForm, bulk_edit.IPAddressBulkEditForm,
                 bulk_import.IPAddressImportForm, filtersets.IPAddressFilterForm])]
    for model_name, table, forms in targets:
        model = getattr(models, model_name)
        for field, label in labels.items():
            try:
                model._meta.get_field(field).verbose_name = label
            except Exception:
                pass
            col = table.base_columns.get(field)
            if col is not None:
                col.verbose_name = label
            for form in forms:
                for name in (field, f'{field}_id'):
                    if name in form.base_fields:
                        form.base_fields[name].label = label
    logger.info('IPAM 화면 라벨 적용: %s', labels)


def patch_ip_search():
    """IP 주소 목록의 '빠른 검색'을 '앞뒤 상관없이 포함' 검색으로 바꾼다.
    대상: IP 주소(아무 부분), 호스트 이름, 호실명(설명), 비고, 소속 이름, 사용자 정의 필드 전체(관리자·전화·MAC·용도 등),
          호관호실·스위치 이름(연결된 객체 이름), MAC 은 구분자(:, -, .) 없이 쳐도 찾음"""
    import re
    from django.db.models import Q
    from django.db.models.expressions import RawSQL
    from ipam.filtersets import IPAddressFilterSet

    def search(self, queryset, name, value):
        v = (value or '').strip()
        if not v:
            return queryset
        like = '%' + v.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'
        q = (Q(dns_name__icontains=v) | Q(description__icontains=v) | Q(comments__icontains=v) |
             Q(tenant__name__icontains=v) |
             Q(pk__in=RawSQL('SELECT id FROM ipam_ipaddress WHERE host(address) LIKE %s', [like])) |
             Q(pk__in=RawSQL('SELECT t.id FROM ipam_ipaddress t, jsonb_each_text(t.custom_field_data) e '
                             'WHERE e.value ILIKE %s', [like])))
        hexv = re.sub(r'[^0-9a-fA-F]', '', v)
        if len(hexv) >= 4 and re.fullmatch(r'[0-9a-fA-F:.\- ]+', v):
            q |= Q(pk__in=RawSQL("SELECT id FROM ipam_ipaddress WHERE replace(custom_field_data->>'host_mac', ':', '') "
                                 "ILIKE %s OR replace(custom_field_data->>'obs_mac', ':', '') ILIKE %s",
                                 ['%' + hexv + '%', '%' + hexv + '%']))
        from dcim.models import Device, Location
        loc_ids = list(Location.objects.filter(name__icontains=v).values_list('pk', flat=True)[:500])
        dev_ids = list(Device.objects.filter(name__icontains=v).values_list('pk', flat=True)[:500])
        if loc_ids:
            q |= Q(custom_field_data__room__in=loc_ids)
        if dev_ids:
            q |= Q(custom_field_data__switch__in=dev_ids)
        return queryset.filter(q)

    IPAddressFilterSet.search = search
