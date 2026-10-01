import django_tables2 as tables

from netbox.tables import NetBoxTable, columns

from .models import Discrepancy, IPRequest


class IPRequestTable(NetBoxTable):
    pk = columns.ToggleColumn()
    id = tables.Column(linkify=True, verbose_name='신청번호')
    status = columns.ChoiceFieldColumn()
    prefix = tables.Column(linkify=True)
    tenant = tables.Column(linkify=True)
    ip_address = tables.Column(linkify=True)

    class Meta(NetBoxTable.Meta):
        model = IPRequest
        fields = ('pk', 'id', 'status', 'requester', 'tenant', 'prefix', 'mac', 'hostname', 'purpose',
                  'ip_address', 'approver', 'created')
        default_columns = ('id', 'status', 'requester', 'tenant', 'prefix', 'mac', 'purpose', 'ip_address', 'created')


class DiscrepancyTable(NetBoxTable):
    pk = columns.ToggleColumn()
    kind = columns.ChoiceFieldColumn()
    ip = tables.Column(linkify=True)
    resolved = columns.BooleanColumn()

    class Meta(NetBoxTable.Meta):
        model = Discrepancy
        fields = ('pk', 'id', 'kind', 'ip', 'detail', 'resolved', 'created')
        default_columns = ('kind', 'ip', 'detail', 'resolved', 'created')
