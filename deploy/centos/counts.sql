-- 이관 전·후 건수 비교용 (export_old_server.sh / restore_new_server.sh / post_check.sh 가 사용)
select 'ip_addresses', count(*) from ipam_ipaddress
union all select 'prefixes', count(*) from ipam_prefix
union all select 'vlans', count(*) from ipam_vlan
union all select 'devices', count(*) from dcim_device
union all select 'interfaces', count(*) from dcim_interface
union all select 'ip_requests', count(*) from netbox_ip_request_iprequest
union all select 'arp_entries', count(*) from netbox_ip_request_arpentry
union all select 'users', count(*) from users_user
union all select 'tokens', count(*) from users_token
union all select 'scripts', count(*) from extras_script;
