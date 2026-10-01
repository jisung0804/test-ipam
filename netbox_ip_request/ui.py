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
