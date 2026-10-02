import django_filters

from netbox.filtersets import NetBoxModelFilterSet

from .models import Discrepancy, DiscrepancyKindChoices, IPRequest, RequestStatusChoices


class IPRequestFilterSet(NetBoxModelFilterSet):
    status = django_filters.MultipleChoiceFilter(choices=RequestStatusChoices)

    class Meta:
        model = IPRequest
        fields = ('id', 'status', 'requester', 'mac')

    def search(self, queryset, name, value):
        from django.db.models import Q
        v = value.strip()
        return queryset.filter(Q(requester__icontains=v) | Q(requester_name__icontains=v) | Q(requester_dept__icontains=v)
                               | Q(requester_email__icontains=v) | Q(requester_phone__icontains=v) | Q(room__icontains=v)
                               | Q(room_name__icontains=v) | Q(mac__icontains=v) | Q(hostname__icontains=v)
                               | Q(purpose__icontains=v) | Q(notify_result__icontains=v)
                               | Q(match_note__icontains=v) | Q(building__name__icontains=v))


class DiscrepancyFilterSet(NetBoxModelFilterSet):
    kind = django_filters.MultipleChoiceFilter(choices=DiscrepancyKindChoices)

    class Meta:
        model = Discrepancy
        fields = ('id', 'kind', 'ip', 'resolved')

    def search(self, queryset, name, value):
        return queryset.filter(detail__icontains=value)
