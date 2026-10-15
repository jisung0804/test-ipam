# 수집 관측(ARP·MAC) 조회 인덱스 — 포트 대조·ARP 적재를 장비·포트·회차 기준으로 빠르게 찾기 위함
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('netbox_ip_request', '0006_ip_count'),
    ]

    operations = [
        migrations.AddIndex(model_name='arpentry',
                            index=models.Index(fields=['device', 'ip'], name='ipreq_arp_dev_ip')),
        migrations.AddIndex(model_name='arpentry',
                            index=models.Index(fields=['last_seen'], name='ipreq_arp_seen')),
        migrations.AddIndex(model_name='macentry',
                            index=models.Index(fields=['device', 'port', 'last_seen'], name='ipreq_mac_dev_port_seen')),
        migrations.AddIndex(model_name='macentry',
                            index=models.Index(fields=['last_seen'], name='ipreq_mac_seen')),
    ]
