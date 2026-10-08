from django.db import migrations, models


def fill_ip_addresses(apps, schema_editor):
    """예전 신청(발급 IP 1개)도 '발급 IP 목록'에 넣어 둔다"""
    IPRequest = apps.get_model('netbox_ip_request', 'IPRequest')
    for r in IPRequest.objects.filter(ip_address__isnull=False):
        r.ip_addresses.add(r.ip_address_id)


class Migration(migrations.Migration):
    dependencies = [
        ('ipam', '0001_squashed'),
        ('netbox_ip_request', '0005_building_match'),
    ]

    operations = [
        migrations.AddField(
            model_name='iprequest', name='ip_addresses',
            field=models.ManyToManyField(blank=True, related_name='+', to='ipam.ipaddress', verbose_name='발급 IP 목록'),
        ),
        migrations.AddField(
            model_name='iprequest', name='ip_count',
            field=models.PositiveSmallIntegerField(default=1, verbose_name='IP 개수'),
        ),
        migrations.RunPython(fill_ip_addresses, migrations.RunPython.noop),
    ]
