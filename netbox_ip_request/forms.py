import re
from django import forms

from dcim.models import Location
from ipam.models import Prefix
from netbox.forms import NetBoxModelForm
from tenancy.models import Tenant
from utilities.forms.fields import DynamicModelChoiceField
from utilities.forms.rendering import FieldSet

from .models import Discrepancy, IPRequest


class RoomInput(forms.TextInput):
    """호실번호 입력 + 전체 호실 목록(콤보박스). 건물을 고르면 그 건물 호실만 보여 줌. 목록에 없으면 직접 입력."""

    def render(self, name, value, attrs=None, renderer=None):
        import json
        from .match import buildings
        attrs = dict(attrs or {}, list='ipr-room-list', autocomplete='off', placeholder='호실번호 — 목록에서 고르거나 입력')
        html = super().render(name, value, attrs, renderer)
        blds = {b.pk: b for b in buildings()}
        data = {}
        for pk, nm, par, desc in Location.objects.filter(parent_id__in=list(blds)).order_by('name') \
                .values_list('pk', 'name', 'parent_id', 'description'):
            data.setdefault(str(par), []).append([nm, (desc or '')[:40]])
        js = json.dumps(data, ensure_ascii=False).replace('</', '<\\/')
        from django.utils.safestring import mark_safe
        return mark_safe(html + f'''<datalist id="ipr-room-list"></datalist>
<div class="form-text" id="ipr-room-count"></div>
<script>(function(){{
  const D = {js};
  const dl = document.getElementById('ipr-room-list'), cnt = document.getElementById('ipr-room-count');
  const b = document.getElementById('id_building');
  function fill(){{
    const k = b ? b.value : '';
    const rows = k ? (D[k] || []) : Object.values(D).flat();
    dl.innerHTML = rows.map(r => '<option value="' + r[0].replace(/"/g,'&quot;') + '">' + (r[1] ? r[1].replace(/</g,'&lt;') : '') + '</option>').join('');
    cnt.textContent = (k ? '이 건물 호실 ' : '전체 호실 ') + rows.length + '개 — 칸을 누르면 목록이 열립니다. 목록에 없으면 직접 입력';
  }}
  if (b) b.addEventListener('change', fill);
  fill();
}})();</script>''')


def _is_admin(user):
    return bool(user and user.is_authenticated and user.has_perm('netbox_ip_request.change_iprequest'))


class IPRequestForm(NetBoxModelForm):
    # 일반 선택 칸(ModelChoiceField 아님): NetBox 가 사용자 권한으로 목록을 거르므로, 위치 보기 권한이 없는 신청자도 고를 수 있게
    building = forms.ChoiceField(required=False, label='건물')
    prefix = DynamicModelChoiceField(queryset=Prefix.objects.all(), required=False, label='발급 대역(VLAN)',
                                     help_text='관리자 전용. 비우면 건물·호실로 자동 매칭합니다.')

    fieldsets = (
        FieldSet('requester_name', 'requester_dept', 'requester_email', 'requester_phone', name='신청자'),
        FieldSet('building', 'room', 'room_name', 'prefix', name='사용 위치'),
        FieldSet('ip_count', 'mac', 'hostname', 'purpose', name='단말'),
        FieldSet('period_days', name='사용 기한'),
    )

    class Meta:
        model = IPRequest
        fields = ('requester_name', 'requester_dept', 'requester_email', 'requester_phone', 'building', 'room',
                  'room_name', 'prefix', 'ip_count', 'mac', 'hostname', 'purpose', 'period_days', 'tags')
        widgets = {'room': RoomInput()}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from netbox.context import current_request
        from netbox.plugins import get_plugin_config
        req = current_request.get()
        days = get_plugin_config('netbox_ip_request', 'default_period_days') or 180
        from .match import buildings
        b = self.fields['building']
        b.choices = [('', '모름 / 목록에 없음')] + [(str(x.pk), x.name) for x in buildings()]     # 숫자 순(1호관, 2호관 … 10호관)
        if self.instance.pk and self.instance.building_id:
            self.initial['building'] = str(self.instance.building_id)
        b.help_text = '건물을 고르면 아래 호실 목록이 그 건물 호실로 좁혀집니다. 호실로 맞는 VLAN 대역을 자동으로 찾습니다.'
        r = self.fields['room']
        r.required = True
        r.error_messages['required'] = '호실번호는 필수입니다 — 목록에서 고르거나 직접 입력하세요'
        mx = get_plugin_config('netbox_ip_request', 'max_ip_count') or 10
        c = self.fields['ip_count']
        c.widget = forms.Select(choices=[(i, f'{i}개') for i in range(1, mx + 1)], attrs={'class': 'form-select'})
        c.initial = self.initial.get('ip_count') or 1
        c.required = False
        c.help_text = f'필요한 IP 개수 (최대 {mx}개). 2개 이상이면 MAC 은 비워 두세요 — 발급 후 자동 수집으로 채워집니다.'
        f = self.fields['period_days']
        if not _is_admin(getattr(req, 'user', None)):
            # 신청자는 대역(VLAN)을 고르지 않는다 — 건물·호실로 자동 매칭, 못 찾으면 관리자가 선택
            del self.fields['prefix']
            self.fieldsets = (
                FieldSet('requester_name', 'requester_dept', 'requester_email', 'requester_phone', name='신청자'),
                FieldSet('building', 'room', 'room_name', name='사용 위치'),
                FieldSet('ip_count', 'mac', 'hostname', 'purpose', name='단말'),
                FieldSet('period_days', name='사용 기한'),
            )
            # 신청자는 기한을 바꿀 수 없음 (화면에서 잠그고, 보내온 값도 무시)
            f.disabled = True
            f.initial = days
            f.help_text = f'사용 기한은 {days}일로 고정됩니다. 변경이 필요하면 관리자와 협의하세요.'
        else:
            f.help_text = f'기본 {days}일. 관리자 권한으로 변경할 수 있습니다(비우면 기한 없음).'
        if not self.instance.pk:
            # ModelForm 은 빈 인스턴스 값('')을 초기값으로 쓰므로 self.initial 에 직접 넣는다
            if not self.initial.get('period_days'):
                self.initial['period_days'] = days
            if req is not None and getattr(req.user, 'is_authenticated', False):
                u = req.user
                full = f'{u.last_name}{u.first_name}'.strip() if re.search('[가-힣]', u.last_name + u.first_name) \
                    else (u.get_full_name() or '').strip()      # 한글 이름은 성+이름 (홍길동)
                if not self.initial.get('requester_name') and full:
                    self.initial['requester_name'] = full
                if not self.initial.get('requester_email') and u.email:
                    self.initial['requester_email'] = u.email


    def clean_ip_count(self):
        return self.cleaned_data.get('ip_count') or 1

    def clean_building(self):
        v = self.cleaned_data.get('building')
        return Location.objects.filter(pk=v).first() if v else None


class RejectForm(forms.Form):
    reason = forms.CharField(max_length=200, label='반려 사유')


class ApproveForm(forms.Form):
    """관리자 발급: 자동(범위 안 첫 빈 IP) 또는 수동 지정. 대역(VLAN) 선택·변경·기한 변경 가능"""
    prefix = forms.ModelChoiceField(queryset=Prefix.objects.all(), required=False, label='발급 대역(VLAN)')
    mode = forms.ChoiceField(choices=(('auto', '자동 발급'), ('manual', '수동 지정')), initial='auto', label='방식',
                             widget=forms.RadioSelect)
    ip = forms.CharField(required=False, label='수동 IP', help_text='여러 개면 쉼표·줄바꿈으로 구분')
    count = forms.IntegerField(required=False, min_value=1, max_value=50, label='발급 개수')
    period_days = forms.IntegerField(required=False, min_value=1, max_value=3650, label='사용 기한(일)')
    force = forms.BooleanField(required=False, label='최근 사용 중으로 보인 IP라도 발급')


class DiscrepancyForm(NetBoxModelForm):
    class Meta:
        model = Discrepancy
        fields = ('resolved', 'detail', 'tags')
