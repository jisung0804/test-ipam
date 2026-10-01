"""장비 출력 파서 + MAC 정규화 (NetBox 없이 단독 실행 가능 — 수집기에서 사용)"""
import re

_MAC = re.compile(r'^[0-9a-f]{12}$')


def norm_mac(v):
    if v is None or str(v).strip() in ('', 'nan', '-'):
        return None
    h = re.sub(r'[^0-9a-fA-F]', '', str(v)).lower()
    if not _MAC.match(h):
        raise ValueError(f'MAC 형식 오류: {v}')
    if h in ('000000000000', 'ffffffffffff') or int(h[1], 16) & 1:
        raise ValueError(f'사용할 수 없는 MAC: {v}')
    return ':'.join(h[i:i + 2] for i in range(0, 12, 2))


def parse_junos_arp(text):
    """Junos 'show arp no-resolve' → [(ip, mac, interface)]"""
    out = []
    for line in text.splitlines():
        m = re.match(r'^\s*([0-9a-f:]{17})\s+(\d+\.\d+\.\d+\.\d+)\s+(\S+)', line, re.I)
        if m:
            out.append((m.group(2), norm_mac(m.group(1)), m.group(3)))
    return out


def parse_cisco_arp(text):
    """Cisco IOS 'show ip arp' → [(ip, mac, interface)] (Incomplete 제외)"""
    out = []
    for line in text.splitlines():
        m = re.match(r'^\s*Internet\s+(\d+\.\d+\.\d+\.\d+)\s+\S+\s+([0-9a-f.]{14})\s+\S+\s+(\S+)', line, re.I)
        if m:
            out.append((m.group(1), norm_mac(m.group(2)), m.group(3)))
    return out


def parse_junos_mac(text):
    """Junos 'show ethernet-switching table' → [(mac, vlan, port)]"""
    out = []
    for line in text.splitlines():
        m = re.match(r'^\s*(\S+)\s+([0-9a-f:]{17})\s+\S+\s+\S+\s+(\S+)', line, re.I)
        if m:
            try:
                out.append((norm_mac(m.group(2)), m.group(1), m.group(3)))
            except ValueError:
                pass
    return out
