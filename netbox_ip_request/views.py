from django.contrib import messages
from django.contrib.auth.mixins import PermissionRequiredMixin
from django.shortcuts import get_object_or_404, redirect
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
        return {'reject_form': forms.RejectForm(),
                'can_decide': request.user.has_perm('netbox_ip_request.change_iprequest')
                and instance.status == 'submitted'}


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
                ip = logic.approve(req.pk, request.user.username)
                messages.success(request, f'{req} 승인 — {ip} 발급')
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
