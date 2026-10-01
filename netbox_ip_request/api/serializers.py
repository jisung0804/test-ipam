from rest_framework import serializers

from ipam.api.serializers import IPAddressSerializer, PrefixSerializer
from netbox.api.serializers import NetBoxModelSerializer
from tenancy.api.serializers import TenantSerializer

from ..models import Discrepancy, IPRequest


class IPRequestSerializer(NetBoxModelSerializer):
    url = serializers.HyperlinkedIdentityField(view_name='plugins-api:netbox_ip_request-api:iprequest-detail')
    prefix = PrefixSerializer(nested=True)
    tenant = TenantSerializer(nested=True, required=False, allow_null=True)
    ip_address = IPAddressSerializer(nested=True, read_only=True)

    class Meta:
        model = IPRequest
        fields = ('id', 'url', 'display', 'requester', 'tenant', 'prefix', 'mac', 'hostname', 'purpose',
                  'period_days', 'status', 'approver', 'reason', 'ip_address', 'created', 'last_updated')
        read_only_fields = ('requester', 'status', 'approver', 'reason', 'ip_address')
        brief_fields = ('id', 'url', 'display', 'status')


    def validate(self, data):
        # 신청자는 입력받지 않고 로그인 계정으로 고정 (모델 검증 전에 주입)
        if self.instance is None:
            data['requester'] = self.context['request'].user.username
        return super().validate(data)


class DiscrepancySerializer(NetBoxModelSerializer):
    url = serializers.HyperlinkedIdentityField(view_name='plugins-api:netbox_ip_request-api:discrepancy-detail')

    class Meta:
        model = Discrepancy
        fields = ('id', 'url', 'display', 'kind', 'ip', 'detail', 'resolved', 'created', 'last_updated')
        brief_fields = ('id', 'url', 'display', 'kind', 'ip')
