from django.db import migrations

TAGS = (('ipam-arp', 'IPAM ARP 수집', '2196f3', 'SNMP로 ARP 테이블 수집(L3 장비)'),
        ('ipam-mac', 'IPAM MAC 수집', '4caf50', 'SNMP로 MAC 테이블 수집(L2 스위치)'))


def create_tags(apps, schema_editor):
    Tag = apps.get_model('extras', 'Tag')
    for slug, name, color, desc in TAGS:
        if not Tag.objects.filter(slug=slug).exists() and not Tag.objects.filter(name=name).exists():
            Tag.objects.create(slug=slug, name=name, color=color, description=desc)


class Migration(migrations.Migration):
    dependencies = [('netbox_ip_request', '0001_initial'), ('extras', '0001_squashed')]
    operations = [migrations.RunPython(create_tags, migrations.RunPython.noop)]
