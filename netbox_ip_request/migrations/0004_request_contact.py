from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('netbox_ip_request', '0003_excel_labels')]

    operations = [
        migrations.AddField('iprequest', 'requester_name',
                            models.CharField(default='', max_length=50, verbose_name='신청자 이름'), preserve_default=False),
        migrations.AddField('iprequest', 'requester_dept',
                            models.CharField(default='', max_length=100, verbose_name='소속'), preserve_default=False),
        migrations.AddField('iprequest', 'requester_email',
                            models.EmailField(default='', max_length=254, verbose_name='이메일',
                                              help_text='발급되면 이 주소로 IP 정보가 자동 발송됩니다'), preserve_default=False),
        migrations.AddField('iprequest', 'requester_phone',
                            models.CharField(default='', max_length=30, verbose_name='연락처',
                                             help_text='예: 032-860-0000, 010-0000-0000'), preserve_default=False),
        migrations.AddField('iprequest', 'room',
                            models.CharField(default='', max_length=50, verbose_name='호실번호', help_text='예: 9-101'),
                            preserve_default=False),
        migrations.AddField('iprequest', 'room_name',
                            models.CharField(blank=True, max_length=100, verbose_name='호실명', help_text='예: 교수연구실')),
        migrations.AddField('iprequest', 'expires_on', models.DateField(blank=True, null=True, verbose_name='사용 기한(만료일)')),
        migrations.AddField('iprequest', 'notified_at', models.DateTimeField(blank=True, null=True, verbose_name='안내 메일 발송')),
        migrations.AddField('iprequest', 'notify_result', models.CharField(blank=True, max_length=300, verbose_name='메일 발송 결과')),
        migrations.AlterField('iprequest', 'requester', models.CharField(max_length=100, verbose_name='신청 계정')),
        migrations.AlterField('iprequest', 'period_days',
                              models.PositiveIntegerField(blank=True, default=180, null=True, verbose_name='사용 기한(일)',
                                                          help_text='사용 기한은 180일로 고정됩니다. 변경이 필요하면 관리자와 협의하세요.')),
    ]
