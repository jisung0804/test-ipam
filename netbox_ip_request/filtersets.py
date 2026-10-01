import django_filters

from netbox.filtersets import NetBoxModelFilterSet

from .models import Discrepancy, DiscrepancyKindChoices, IPRequest, RequestStatusChoices


class IPRequestFilterSet(NetBoxModelFilterSet):
    status = django_filters.MultipleChoiceFilter(choices=RequestStatusChoices)

    class Meta:
        model = IPRequest
        fields = ('id', 'status', 'requester', 'mac')

    def search(self, queryset, name, value):
        return queryset.filter(requester__icontains=value) | queryset.filter(mac__icontains=value) | \
            queryset.filter(hostname__icontains=value)


class DiscrepancyFilterSet(NetBoxModelFilterSet):
    kind = django_filters.MultipleChoiceFilter(choices=DiscrepancyKindChoices)

    class Meta:
        model = Discrepancy
        fields = ('id', 'kind', 'ip', 'resolved')

    def search(self, queryset, name, value):
        return queryset.filter(detail__icontains=value)
