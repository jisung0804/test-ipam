from django.contrib import messages
from django.contrib.auth.mixins import PermissionRequiredMixin
from django.shortcuts import get_object_or_404, redirect
from django.utils import timezone
from django.views import View

from netbox.views import generic
from utilities.views import register_model_view

from . import filtersets, forms, logic, tables
from .models import Discrepancy, IPRequest


@register_model_view(IPRequest, 'list', path='', detail=False)
class IPRequestListView(generic.ObjectListView):
    queryset = IPRequest.objects.all()
    table = tables.IPRequestTable
    filterset = filtersets.IPRequestFilterSet


@register_model_view(IPRequest)
class IPRequestView(generic.ObjectView):
    queryset = IPRequest.objects.all()

    def get_extra_context(self, request, instance):
        can = request.user.has_perm('netbox_ip_request.change_iprequest')
        ctx = {'reject_form': forms.RejectForm(), 'can_decide': can and instance.status == 'submitted',
               'can_resend': can and instance.status == 'allocated'}
        if ctx['can_decide']:
            from ipam.models import Prefix
            from .match import match
            pfx = instance.prefix
            if request.GET.get('prefix'):   # 승인 화면에서 대역을 고르거나 바꿔 미리보기
                pfx = Prefix.objects.filter(pk=request.GET['prefix']).first() or pfx
            m = match(instance.building, instance.room)       # 관리자 판단용: 이 건물·호실이 쓰는 대역 후보
            cands = [(p, n) for p, n in m['candidates']]
            ctx.update({'approve_form': forms.ApproveForm(initial={'prefix': pfx, 'period_days': instance.period_days}),
                        'preview_prefix': pfx, 'candidates': cands,
                        'all_prefixes': Prefix.objects.filter(vrf__isnull=True).select_related('vlan').order_by('prefix'),
                        'alloc_min': logic.cfg('alloc_host_min'), 'alloc_max': logic.cfg('alloc_host_max')})
            if pfx is not None:
                nxt, free, outside, busy = logic.available_preview(pfx)
                ctx.update({'next_ip': nxt, 'free_ips': free, 'outside': outside, 'busy': busy})
        if instance.status == 'allocated' and instance.ip_address:
            ctx['mail_subject'], ctx['mail_body'] = logic.mail_text(instance)
        return ctx


@register_model_view(IPRequest, 'add', detail=False)
@register_model_view(IPRequest, 'edit')
class IPRequestEditView(generic.ObjectEditView):
    queryset = IPRequest.objects.all()
    form = forms.IPRequestForm

    def alter_object(self, obj, request, url_args, url_kwargs):
        if not obj.pk:
            obj.requester = request.user.username  # 신청자는 로그인 계정으로 고정
        return obj


@register_model_view(IPRequest, 'delete')
class IPRequestDeleteView(generic.ObjectDeleteView):
    queryset = IPRequest.objects.all()


class IPRequestDecisionView(PermissionRequiredMixin, View):
    permission_required = 'netbox_ip_request.change_iprequest'

    def post(self, request, pk, decision):
        req = get_object_or_404(IPRequest, pk=pk)
        try:
            if decision == 'approve':
                f = forms.ApproveForm(request.POST)
                if not f.is_valid():
                    messages.error(request, f'입력 확인: {f.errors.as_text()}')
                    return redirect(req.get_absolute_url())
                d = f.cleaned_data
                if d.get('prefix') is None and req.prefix_id is None:
                    messages.error(request, '발급할 대역(VLAN)을 먼저 고르고 [미리보기]를 누르세요')
                    return redirect(req.get_absolute_url())
                if d['mode'] == 'manual' and not (d.get('ip') or '').strip():
                    messages.error(request, '수동 지정을 골랐다면 IP를 입력하세요')
                    return redirect(req.get_absolute_url())
                ip = logic.approve(req.pk, request.user.username, prefix=d['prefix'],
                                   ip=d['ip'] if d['mode'] == 'manual' else None,
                                   period_days=d.get('period_days'), force=d.get('force', False))
                req.refresh_from_db()
                messages.success(request, f'{req} 발급 완료 — {ip}. 안내 메일: {req.notify_result or "발송 대기"}')
            elif decision == 'resend':
                ok = logic.notify(req.pk)
                req.refresh_from_db()
                (messages.success if ok else messages.error)(request, f'안내 메일: {req.notify_result}')
            else:
                form = forms.RejectForm(request.POST)
                logic.reject(req.pk, request.user.username, form.data.get('reason', ''))
                messages.warning(request, f'{req} 반려')
        except logic.AllocationError as e:
            messages.error(request, str(e))
        return redirect(req.get_absolute_url())


@register_model_view(Discrepancy, 'list', path='', detail=False)
class DiscrepancyListView(generic.ObjectListView):
    queryset = Discrepancy.objects.all()
    table = tables.DiscrepancyTable
    filterset = filtersets.DiscrepancyFilterSet


@register_model_view(Discrepancy)
class DiscrepancyView(generic.ObjectView):
    queryset = Discrepancy.objects.all()


@register_model_view(Discrepancy, 'edit')
class DiscrepancyEditView(generic.ObjectEditView):
    queryset = Discrepancy.objects.all()
    form = forms.DiscrepancyForm


@register_model_view(Discrepancy, 'delete')
class DiscrepancyDeleteView(generic.ObjectDeleteView):
    queryset = Discrepancy.objects.all()


# --------------------------------------------------------------------- IP 자원 현황
from django.contrib.auth.mixins import LoginRequiredMixin  # noqa: E402
from django.http import HttpResponse  # noqa: E402
from django.shortcuts import render  # noqa: E402
from django.urls import reverse  # noqa: E402
from urllib.parse import urlencode  # noqa: E402


class ResourcesView(LoginRequiredMixin, View):
    """VLAN(대역)별 전체·대장·실사용·가용. ?q=검색어 &days=30 &sort=util &export=xlsx"""

    def get(self, request):
        from . import resources
        from netbox.plugins import get_plugin_config
        q = request.GET.get('q', '').strip()
        try:
            days = int(request.GET.get('days') or get_plugin_config('netbox_ip_request', 'recon_days') or 30)
        except ValueError:
            days = 30
        sort = request.GET.get('sort', 'prefix')
        rows, tot = resources.compute(days=days, q=q)
        key = sort.lstrip('-')
        if key in dict(resources.COLUMNS) or key == 'prefix':
            import ipaddress
            kf = (lambda r: ipaddress.ip_network(r['prefix'])) if key == 'prefix' else \
                 (lambda r: (r[key] == '', r[key] if not isinstance(r[key], str) else r[key].lower()))
            rows.sort(key=kf, reverse=sort.startswith('-'))
        if request.GET.get('export') == 'xlsx':
            resp = HttpResponse(resources.to_excel(rows),
                                content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
            resp['Content-Disposition'] = f'attachment; filename="ip_resources_{timezone.localdate():%Y%m%d}.xlsx"'
            return resp
        ip_list = reverse('ipam:ipaddress_list')
        for r in rows:
            base = {'parent': r['prefix']}
            r['link_prefix'] = reverse('ipam:prefix', args=[r['pk']])
            r['link_reg'] = f"{ip_list}?{urlencode(base)}"
            r['link_unseen'] = f"{ip_list}?{urlencode({**base, 'cf_recon_state': 'unseen'})}"
            r['link_unreg'] = f"{ip_list}?{urlencode({**base, 'cf_recon_state': 'discovered'})}"
            r['bar'] = 'danger' if r['util'] >= 90 else 'warning' if r['util'] >= 70 else 'success'
        cols = [(k, l, ('-' + k) if sort == k else k) for k, l in resources.COLUMNS]
        return render(request, 'netbox_ip_request/resources.html', {
            'rows': rows, 'tot': tot, 'q': q, 'days': days, 'sort': sort, 'cols': cols,
            'export_qs': urlencode({'q': q, 'days': days, 'sort': sort, 'export': 'xlsx'}),
        })


class ReconSummaryView(LoginRequiredMixin, View):
    """대사 결과·관리자 판정 건수 → 클릭하면 IP 주소 목록으로"""

    def get(self, request):
        from django.db.models import Count
        from ipam.models import IPAddress
        from .room_import import RECON_STATES, REVIEW_CHOICES
        ip_list = reverse('ipam:ipaddress_list')
        st = dict(IPAddress.objects.order_by().values_list('custom_field_data__recon_state').annotate(n=Count('id')))
        rv = dict(IPAddress.objects.order_by().values_list('custom_field_data__review').annotate(n=Count('id')))
        states = [(code, label, color, st.get(code, 0), f'{ip_list}?cf_recon_state={code}') for code, label, color in RECON_STATES]
        reviews = [(code, label, color, rv.get(code, 0), f'{ip_list}?cf_review={code}') for code, label, color in REVIEW_CHOICES]
        return render(request, 'netbox_ip_request/recon.html', {
            'states': states, 'reviews': reviews, 'unreviewed': rv.get(None, 0), 'ip_list': ip_list})
