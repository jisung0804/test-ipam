from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.urls import reverse

from netbox.models import NetBoxModel
from utilities.choices import ChoiceSet


class RequestStatusChoices(ChoiceSet):
    SUBMITTED, APPROVED, ALLOCATED, REJECTED, CANCELLED = 'submitted', 'approved', 'allocated', 'rejected', 'cancelled'
    CHOICES = [
        (SUBMITTED, '접수', 'blue'),
        (APPROVED, '승인', 'cyan'),
        (ALLOCATED, '발급 완료', 'green'),
        (REJECTED, '반려', 'red'),
        (CANCELLED, '취소', 'gray'),
    ]


class DiscrepancyKindChoices(ChoiceSet):
    CHOICES = [
        ('unregistered_use', '미등록 사용', 'orange'),
        ('mac_mismatch', 'MAC 불일치', 'yellow'),
        ('ip_conflict', 'IP 충돌', 'red'),
        ('port_mismatch', '스위치 포트 불일치', 'purple'),
    ]


class IPRequest(NetBoxModel):
    requester = models.CharField(max_length=100, verbose_name='신청 계정')
    requester_name = models.CharField(max_length=50, verbose_name='신청자 이름')
    requester_dept = models.CharField(max_length=100, verbose_name='소속')
    requester_email = models.EmailField(verbose_name='이메일', help_text='발급되면 이 주소로 IP 정보가 자동 발송됩니다')
    requester_phone = models.CharField(max_length=30, verbose_name='연락처', help_text='예: 032-860-0000, 010-0000-0000')
    building = models.ForeignKey('dcim.Location', on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
                                 verbose_name='건물')
    room = models.CharField(max_length=50, blank=True, verbose_name='호실번호',
                            help_text='필수. 목록에서 고르거나 직접 입력 (예: 101 — 건물을 고른 경우, 또는 9-101)')
    room_name = models.CharField(max_length=100, blank=True, verbose_name='호실명', help_text='예: 교수연구실')
    tenant = models.ForeignKey('tenancy.Tenant', on_delete=models.PROTECT, null=True, blank=True,
                               related_name='+', verbose_name='부서')
    prefix = models.ForeignKey('ipam.Prefix', on_delete=models.PROTECT, null=True, blank=True, related_name='+',
                               verbose_name='발급 대역(VLAN)', help_text='비우면 건물·호실로 자동 매칭, 못 찾으면 관리자가 선택')
    match_note = models.CharField(max_length=200, blank=True, verbose_name='대역 자동 매칭')
    mac = models.CharField(max_length=17, null=True, blank=True, verbose_name='MAC')
    hostname = models.CharField(max_length=100, blank=True, verbose_name='호스트명')
    ip_count = models.PositiveSmallIntegerField(default=1, verbose_name='IP 개수',
                                                help_text='필요한 IP 수. 2개 이상이면 MAC 은 비워 두세요(발급 후 자동 수집으로 채움)')
    purpose = models.CharField(max_length=200, verbose_name='용도')
    period_days = models.PositiveIntegerField(default=180, null=True, blank=True, verbose_name='사용 기한(일)',
                                              help_text='사용 기한은 180일로 고정됩니다. 변경이 필요하면 관리자와 협의하세요.')
    status = models.CharField(max_length=20, choices=RequestStatusChoices, default=RequestStatusChoices.SUBMITTED)
    approver = models.CharField(max_length=100, blank=True)
    reason = models.CharField(max_length=200, blank=True, verbose_name='반려 사유')
    ip_address = models.ForeignKey('ipam.IPAddress', on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name='+', verbose_name='발급 IP')
    ip_addresses = models.ManyToManyField('ipam.IPAddress', blank=True, related_name='+', verbose_name='발급 IP 목록')
    expires_on = models.DateField(null=True, blank=True, verbose_name='사용 기한(만료일)')
    notified_at = models.DateTimeField(null=True, blank=True, verbose_name='안내 메일 발송')
    notify_result = models.CharField(max_length=300, blank=True, verbose_name='메일 발송 결과')

    class Meta:
        ordering = ('-pk',)
        verbose_name = 'IP 발급 신청'
        constraints = [
            # 같은 MAC으로 처리 중인 신청이 2건 존재할 수 없음
            models.UniqueConstraint(fields=['mac'], name='netbox_ip_request_open_mac',
                                    condition=Q(status__in=['submitted', 'approved']) & Q(mac__isnull=False)),
        ]

    def __str__(self):
        return f'REQ-{self.pk}'

    def issued_ips(self):
        """발급된 IP 전체 (예전 1개 발급 신청은 ip_address 만 있음)"""
        ips = list(self.ip_addresses.all().order_by('address')) if self.pk else []
        if not ips and self.ip_address_id:
            ips = [self.ip_address]
        return ips

    def get_absolute_url(self):
        return reverse('plugins:netbox_ip_request:iprequest', args=[self.pk])

    def get_status_color(self):
        return RequestStatusChoices.colors.get(self.status)

    def save(self, *args, **kwargs):
        if self._state.adding:
            from .match import full_room, match
            self.room = full_room(self.building, self.room)
            if self.prefix_id is None:       # 사용자는 대역을 고르지 않음 → 건물·호실로 매칭
                r = match(self.building, self.room)
                self.prefix, self.match_note = r['prefix'], r['note'][:200]
                if self.building is None and r['building'] is not None:
                    self.building = r['building']
        super().save(*args, **kwargs)

    def clean(self):
        super().clean()
        from .logic import norm_mac, mac_in_use
        try:
            self.mac = norm_mac(self.mac)
        except ValueError as e:
            raise ValidationError({'mac': str(e)})
        from netbox.plugins import get_plugin_config
        mx = get_plugin_config('netbox_ip_request', 'max_ip_count') or 10
        if not self.ip_count or self.ip_count < 1 or self.ip_count > mx:
            raise ValidationError({'ip_count': f'IP 개수는 1~{mx}개'})
        if self.ip_count > 1 and self.mac:
            raise ValidationError({'mac': 'IP 를 2개 이상 신청할 때는 MAC 을 비워 두세요 (발급 후 자동 수집으로 채워짐)'})
        if self._state.adding and not (self.room or '').strip():
            raise ValidationError({'room': '호실번호는 필수입니다 — 목록에서 고르거나 직접 입력하세요'})
        if self._state.adding and self.mac and mac_in_use(self.mac):
            raise ValidationError({'mac': f'이 MAC은 이미 IP를 발급받았습니다: {mac_in_use(self.mac)}'})
        if self.mac and self.status in ('submitted', 'approved'):
            dup = IPRequest.objects.filter(mac=self.mac, status__in=['submitted', 'approved']).exclude(pk=self.pk).first()
            if dup:
                raise ValidationError({'mac': f'이 MAC으로 처리 중인 신청이 이미 있습니다: {dup} (중복 신청 불가)'})


class Discrepancy(NetBoxModel):
    kind = models.CharField(max_length=30, choices=DiscrepancyKindChoices)
    ip = models.GenericIPAddressField()
    detail = models.CharField(max_length=300, blank=True)
    resolved = models.BooleanField(default=False)

    class Meta:
        ordering = ('resolved', '-pk')
        verbose_name = '대장 불일치'
        verbose_name_plural = '대장 불일치'
        constraints = [
            models.UniqueConstraint(fields=['kind', 'ip'], condition=Q(resolved=False), name='netbox_ip_request_open_disc'),
        ]

    def __str__(self):
        return f'{self.get_kind_display()} {self.ip}'

    def get_absolute_url(self):
        return reverse('plugins:netbox_ip_request:discrepancy', args=[self.pk])

    def get_kind_color(self):
        return DiscrepancyKindChoices.colors.get(self.kind)


class ArpEntry(models.Model):
    """L3 ARP 관측 (고빈도 적재 → 변경 이력 미기록 일반 모델)"""
    ip = models.GenericIPAddressField()
    mac = models.CharField(max_length=17)
    device = models.CharField(max_length=100)
    interface = models.CharField(max_length=100, blank=True)
    first_seen = models.DateTimeField()
    last_seen = models.DateTimeField()

    class Meta:
        constraints = [models.UniqueConstraint(fields=['ip', 'mac', 'device'], name='netbox_ip_request_arp_uniq')]
        indexes = [models.Index(fields=['ip', 'last_seen'])]


class MacEntry(models.Model):
    """L2 MAC 테이블 관측"""
    mac = models.CharField(max_length=17)
    device = models.CharField(max_length=100)
    port = models.CharField(max_length=100)
    vlan = models.CharField(max_length=50, blank=True)
    first_seen = models.DateTimeField()
    last_seen = models.DateTimeField()

    class Meta:
        constraints = [models.UniqueConstraint(fields=['mac', 'device', 'port'], name='netbox_ip_request_mac_uniq')]
