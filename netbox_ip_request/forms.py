from django import forms

from ipam.models import Prefix
from netbox.forms import NetBoxModelForm
from tenancy.models import Tenant
from utilities.forms.fields import DynamicModelChoiceField

from .models import Discrepancy, IPRequest


class IPRequestForm(NetBoxModelForm):
    prefix = DynamicModelChoiceField(queryset=Prefix.objects.all(), label='사용 위치(대역)',
                                     help_text='건물·층 대역을 선택하세요')
    tenant = DynamicModelChoiceField(queryset=Tenant.objects.all(), required=False, label='부서')

    class Meta:
        model = IPRequest
        fields = ('prefix', 'tenant', 'mac', 'hostname', 'purpose', 'period_days', 'tags')


class RejectForm(forms.Form):
    reason = forms.CharField(max_length=200, label='반려 사유')


class DiscrepancyForm(NetBoxModelForm):
    class Meta:
        model = Discrepancy
        fields = ('resolved', 'detail', 'tags')
