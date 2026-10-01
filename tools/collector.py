"""ARP/MAC 수집기: 장비에서 읽기 전용 명령으로 테이블을 가져와 NetBox 플러그인 API로 전송.
cron 또는 systemd timer로 5분마다 실행:  */5 * * * * python collector.py devices.yaml
devices.yaml 예)
  - {name: core-ex9208, host: 10.0.0.1, platform: juniper_junos, role: l3}
  - {name: acc-sw-01,   host: 10.0.0.11, platform: juniper_junos, role: l2}
"""
import os, sys
from concurrent.futures import ThreadPoolExecutor

from nb import S, URL
from netparse import parse_cisco_arp, parse_junos_arp, parse_junos_mac

CMDS = {
    'juniper_junos': {'l3': ('show arp no-resolve', parse_junos_arp), 'l2': ('show ethernet-switching table', parse_junos_mac)},
    'cisco_ios': {'l3': ('show ip arp', parse_cisco_arp)},
}


def push_arp(device, entries):
    r = S.post(URL + '/plugins/ip-request/arp-ingest/', json={'device': device, 'entries': entries})
    r.raise_for_status(); return r.json()


def push_mac(device, entries):
    r = S.post(URL + '/plugins/ip-request/mac-ingest/', json={'device': device, 'entries': entries})
    r.raise_for_status(); return r.json()


def poll(dev):
    from netmiko import ConnectHandler  # pip install netmiko
    cmd, parser = CMDS[dev['platform']][dev['role']]
    with ConnectHandler(device_type=dev['platform'], host=dev['host'], username=os.environ['NET_USER'],
                        password=os.environ['NET_PASS'], timeout=30) as c:
        rows = parser(c.send_command(cmd))
    return dev['name'], dev['role'], rows


if __name__ == '__main__':
    import yaml
    devices = yaml.safe_load(open(sys.argv[1]))
    with ThreadPoolExecutor(8) as ex:
        for fut in [ex.submit(poll, d) for d in devices]:
            try:
                name, role, rows = fut.result()
                print(name, push_arp(name, rows) if role == 'l3' else push_mac(name, rows))
            except Exception as e:  # 장비 1대 실패가 전체를 멈추지 않게
                print('수집 실패', e, file=sys.stderr)
