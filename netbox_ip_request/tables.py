import django_tables2 as tables

from netbox.tables import NetBoxTable, columns

from .models import Discrepancy, IPRequest


class IPRequestTable(NetBoxTable):
    pk = columns.ToggleColumn()
    id = tables.Column(linkify=True, verbose_name='신청번호')
    status = columns.ChoiceFieldColumn()
    prefix = tables.Column(linkify=True, verbose_name='발급 대역(VLAN)')
    building = tables.Column(linkify=True, verbose_name='건물')
    tenant = tables.Column(linkify=True)
    ip_address = tables.Column(linkify=True)

    class Meta(NetBoxTable.Meta):
        model = IPRequest
        fields = ('pk', 'id', 'status', 'requester', 'requester_name', 'requester_dept', 'requester_email',
                  'requester_phone', 'building', 'room', 'room_name', 'tenant', 'prefix', 'match_note', 'mac', 'hostname', 'purpose',
                  'period_days', 'expires_on', 'ip_address', 'approver', 'notify_result', 'created')
        default_columns = ('id', 'status', 'requester_name', 'requester_dept', 'building', 'room', 'prefix', 'mac', 'purpose',
                           'ip_address', 'expires_on', 'notify_result', 'created')


class DiscrepancyTable(NetBoxTable):
    pk = columns.ToggleColumn()
    kind = columns.ChoiceFieldColumn()
    ip = tables.Column(linkify=True)
    resolved = columns.BooleanColumn()

    class Meta(NetBoxTable.Meta):
        model = Discrepancy
        fields = ('pk', 'id', 'kind', 'ip', 'detail', 'resolved', 'created')
        default_columns = ('kind', 'ip', 'detail', 'resolved', 'created')
