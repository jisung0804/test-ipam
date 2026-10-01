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
        ('운영', (
            PluginMenuItem(link='plugins:netbox_ip_request:discrepancy_list', link_text='대장 불일치'),
            PluginMenuItem(link='extras:script_list', link_text='엑셀 가져오기',
                           permissions=['extras.view_script']),
        )),
    ),
)
