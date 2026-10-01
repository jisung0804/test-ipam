from netbox.plugins import PluginMenu, PluginMenuButton, PluginMenuItem

menu = PluginMenu(
    label='IP 신청',
    icon_class='mdi mdi-ip-network',
    groups=(
        ('신청', (
            PluginMenuItem(link='plugins:netbox_ip_request:iprequest_list', link_text='IP 발급 신청',
                           buttons=(PluginMenuButton('plugins:netbox_ip_request:iprequest_add', '신청하기',
                                                     'mdi mdi-plus-thick'),)),
        )),
        ('IP 자원', (
            PluginMenuItem(link='plugins:netbox_ip_request:resources', link_text='IP 자원 현황 (VLAN별)',
                           permissions=['ipam.view_prefix']),
            PluginMenuItem(link='plugins:netbox_ip_request:recon', link_text='장비 대사 결과·판정',
                           permissions=['ipam.view_ipaddress']),
        )),
        ('운영', (
            PluginMenuItem(link='plugins:netbox_ip_request:discrepancy_list', link_text='대장 불일치'),
            PluginMenuItem(link='extras:script_list', link_text='도구(장비 등록·엑셀·판정 반영)',
                           permissions=['extras.view_script']),
        )),
    ),
)
