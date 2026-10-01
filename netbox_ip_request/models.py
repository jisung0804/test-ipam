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
    requester = models.CharField(max_length=100)
    tenant = models.ForeignKey('tenancy.Tenant', on_delete=models.PROTECT, null=True, blank=True,
                               related_name='+', verbose_name='부서')
    prefix = models.ForeignKey('ipam.Prefix', on_delete=models.PROTECT, related_name='+', verbose_name='요청 대역')
    mac = models.CharField(max_length=17, null=True, blank=True, verbose_name='MAC')
    hostname = models.CharField(max_length=100, blank=True, verbose_name='호스트명')
    purpose = models.CharField(max_length=200, verbose_name='용도')
    period_days = models.PositiveIntegerField(null=True, blank=True, verbose_name='사용 기간(일, 비우면 영구)')
    status = models.CharField(max_length=20, choices=RequestStatusChoices, default=RequestStatusChoices.SUBMITTED)
    approver = models.CharField(max_length=100, blank=True)
    reason = models.CharField(max_length=200, blank=True, verbose_name='반려 사유')
    ip_address = models.ForeignKey('ipam.IPAddress', on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name='+', verbose_name='발급 IP')

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

    def get_absolute_url(self):
        return reverse('plugins:netbox_ip_request:iprequest', args=[self.pk])

    def get_status_color(self):
        return RequestStatusChoices.colors.get(self.status)

    def clean(self):
        super().clean()
        from .logic import norm_mac, mac_in_use
        try:
            self.mac = norm_mac(self.mac)
        except ValueError as e:
            raise ValidationError({'mac': str(e)})
        if self._state.adding and self.mac and mac_in_use(self.mac):
            raise ValidationError({'mac': f'이 MAC은 이미 IP를 발급받았습니다: {mac_in_use(self.mac)}'})


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
