import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('dcim', '0003_squashed_0130'),
        ('netbox_ip_request', '0004_request_contact'),
    ]

    operations = [
        migrations.AddField(
            model_name='iprequest', name='building',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+',
                                    to='dcim.location', verbose_name='건물'),
        ),
        migrations.AlterField(
            model_name='iprequest', name='room',
            field=models.CharField(blank=True, help_text='예: 101 (건물을 고른 경우) 또는 9-101. 모르면 비워 두세요',
                                   max_length=50, verbose_name='호실번호'),
        ),
        migrations.AlterField(
            model_name='iprequest', name='prefix',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='+',
                                    to='ipam.prefix', verbose_name='발급 대역(VLAN)',
                                    help_text='비우면 건물·호실로 자동 매칭, 못 찾으면 관리자가 선택'),
        ),
        migrations.AddField(
            model_name='iprequest', name='match_note',
            field=models.CharField(blank=True, max_length=200, verbose_name='대역 자동 매칭'),
        ),
    ]
