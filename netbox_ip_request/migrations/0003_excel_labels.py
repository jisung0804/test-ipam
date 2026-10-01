"""사용자 정의 필드 화면 이름을 원본 엑셀 컬럼명으로 맞춤 (예전 기본 이름일 때만 — 직접 바꾼 이름은 그대로 둔다)"""
from django.db import migrations

RENAME = {  # 필드: (예전 기본 이름, 엑셀 컬럼명)
    'ip_user': ('사용자', '관리자'),
    'host_mac': ('MAC(단말)', 'MAC ADDRESS'),
    'room': ('호실', '호관호실'),
    'switch': ('연결 스위치', '스위치IP ADDRESS'),
    'switch_port': ('스위치 포트', '스위치 포트 번호'),
    'outlet_no': ('아웃렛 번호', 'OUTLET NO'),
    'patch_panel': ('패치 번호', '패치번호'),
    'patch_port': ('패치 포트', '패치포트번호'),
    'manager_phone': ('관리자 전화', '전화번호'),
    'legacy_seq': ('엑셀 연번', '연번'),
}


def forward(apps, schema_editor):
    CustomField = apps.get_model('extras', 'CustomField')
    for name, (old, new) in RENAME.items():
        CustomField.objects.filter(name=name, label=old).update(label=new)


def backward(apps, schema_editor):
    CustomField = apps.get_model('extras', 'CustomField')
    for name, (old, new) in RENAME.items():
        CustomField.objects.filter(name=name, label=new).update(label=old)


class Migration(migrations.Migration):
    dependencies = [('netbox_ip_request', '0002_snmp_tags'), ('extras', '0001_squashed')]
    operations = [migrations.RunPython(forward, backward)]
