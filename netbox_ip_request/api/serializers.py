from rest_framework import serializers

from dcim.api.serializers import LocationSerializer
from ipam.api.serializers import IPAddressSerializer, PrefixSerializer
from netbox.api.serializers import NetBoxModelSerializer
from tenancy.api.serializers import TenantSerializer

from ..models import Discrepancy, IPRequest


class IPRequestSerializer(NetBoxModelSerializer):
    url = serializers.HyperlinkedIdentityField(view_name='plugins-api:netbox_ip_request-api:iprequest-detail')
    prefix = PrefixSerializer(nested=True, required=False, allow_null=True)
    building = LocationSerializer(nested=True, required=False, allow_null=True)
    tenant = TenantSerializer(nested=True, required=False, allow_null=True)
    ip_address = IPAddressSerializer(nested=True, read_only=True)

    class Meta:
        model = IPRequest
        fields = ('id', 'url', 'display', 'requester', 'requester_name', 'requester_dept', 'requester_email',
                  'requester_phone', 'building', 'room', 'room_name', 'tenant', 'prefix', 'match_note', 'mac', 'hostname', 'purpose',
                  'period_days', 'status', 'approver', 'reason', 'ip_address', 'expires_on', 'notify_result',
                  'created', 'last_updated')
        read_only_fields = ('match_note', 'requester', 'status', 'approver', 'reason', 'ip_address', 'expires_on', 'notify_result')
        brief_fields = ('id', 'url', 'display', 'status')


    def validate(self, data):
        # 신청자는 입력받지 않고 로그인 계정으로 고정 (모델 검증 전에 주입)
        user = self.context['request'].user
        if self.instance is None:
            data['requester'] = user.username
        if not user.has_perm('netbox_ip_request.change_iprequest'):
            from netbox.plugins import get_plugin_config   # 신청자는 기한 고정
            data['period_days'] = get_plugin_config('netbox_ip_request', 'default_period_days') or 180
            if self.instance is None:
                data.pop('prefix', None)     # 신청자는 대역을 고르지 않음 → 건물·호실로 자동 매칭
        return super().validate(data)


class DiscrepancySerializer(NetBoxModelSerializer):
    url = serializers.HyperlinkedIdentityField(view_name='plugins-api:netbox_ip_request-api:discrepancy-detail')

    class Meta:
        model = Discrepancy
        fields = ('id', 'url', 'display', 'kind', 'ip', 'detail', 'resolved', 'created', 'last_updated')
        brief_fields = ('id', 'url', 'display', 'kind', 'ip')
