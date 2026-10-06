"""스위치 수백 대에 SNMPv3 읽기 전용 계정을 한 번에 넣는 도구 (Cisco·Juniper·HP/Aruba·Comware 혼재)

순서(장비마다, 동시에 10대씩)
  1) 이미 SNMPv3 로 응답하면 건너뜀 (이미 설정됨)
  2) 벤더 판별: 목록의 vendor 칸 → 없으면 기존 SNMP v2c 의 sysObjectID → 없으면 SSH 자동 판별
  3) 벤더별 설정 명령 생성 (점검 모드에서는 여기까지만 하고 결과 파일에 명령 미리보기)
  4) --apply 일 때만: SSH 접속 → 기존 snmp 설정 백업 → 명령 적용 → 저장
  5) SNMPv3 로 다시 물어봐 성공/실패 확인
  결과: result_YYYYMMDD_HHMM.xlsx (장비별 상태·사유·넣은 명령, 비밀번호는 **** 로 가림)

장비 목록: 엑셀/CSV (열: ip, name(선택), vendor(선택: cisco_ios, cisco_xe, cisco_nxos, juniper_junos,
          hp_procurve, aruba_aoscx, hp_comware, aruba_os)) 또는 --from-netbox (NetBox 장치 목록)

환경변수 (비밀번호는 파일에 적지 않는다)
  NET_USER, NET_PASS, NET_SECRET(enable 비밀번호, 없으면 생략)      ← 장비 SSH 로그인
  IPAM_SNMP_USER, IPAM_SNMP_AUTH, IPAM_SNMP_PRIV                    ← 새로 만들 SNMPv3 계정
  COLLECTOR_IPS=165.246.12.104,165.246.1.50                         ← SNMP 를 허용할 수집 서버 IP(쉼표)
  SNMP_V2C_COMMUNITY (선택)                                          ← 벤더 판별용 기존 v2c community
  NETBOX_URL, NETBOX_TOKEN (--from-netbox 일 때)

ACL(수집 서버 IP 허용)도 같이 넣는다 — 벤더별 방식은 acl_commands() / dynamic_acl() 설명 참고.
  서버 IP 를 나중에 추가·변경할 때:  COLLECTOR_IPS=새IP python snmp_bulk_config.py devices.xlsx --apply --acl-only --force

예)
  python snmp_bulk_config.py devices.xlsx                 # 점검만 (장비 변경 없음)
  python snmp_bulk_config.py devices.xlsx --apply --limit 3   # 앞 3대만 실제 적용 (벤더별 시범)
  python snmp_bulk_config.py devices.xlsx --apply         # 전체 적용
  python snmp_bulk_config.py --from-netbox --apply --only-failed result_20261001_1030.xlsx
"""
import argparse
import asyncio
import datetime as dt
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import pandas as pd

ENTERPRISE_TO_TYPE = {9: 'cisco_ios', 2636: 'juniper_junos', 11: 'hp_procurve', 25506: 'hp_comware',
                      47196: 'aruba_aoscx', 14823: 'aruba_os'}
SUPPORTED = ('cisco_ios', 'cisco_xe', 'cisco_nxos', 'juniper_junos', 'hp_procurve', 'aruba_osswitch',
             'aruba_procurve', 'aruba_aoscx', 'hp_comware', 'aruba_os')


# --------------------------------------------------------------------- 설정
class Cfg:
    def __init__(self, a):
        e = os.environ
        self.user, self.auth, self.priv = e.get('IPAM_SNMP_USER', 'ipam-ro'), e.get('IPAM_SNMP_AUTH', ''), e.get('IPAM_SNMP_PRIV', '')
        self.collectors = [x.strip() for x in e.get('COLLECTOR_IPS', '').split(',') if x.strip()]
        self.v2c = e.get('SNMP_V2C_COMMUNITY', '')
        self.ssh_user, self.ssh_pass, self.secret = e.get('NET_USER', ''), e.get('NET_PASS', ''), e.get('NET_SECRET', '')
        self.apply, self.force, self.port = a.apply, a.force, a.snmp_port
        self.aoscx_vrfs = [v.strip() for v in a.aoscx_vrf.split(',') if v.strip()]
        self.acl_only = a.acl_only
        self.cisco_acl = a.cisco_acl
        self.junos_prefix_list = a.junos_prefix_list
        self.comware_acl = a.comware_acl
        self.telnet = getattr(a, 'telnet', False)

    def check(self):
        bad = []
        if len(self.auth) < 8 or len(self.priv) < 8:
            bad.append('IPAM_SNMP_AUTH / IPAM_SNMP_PRIV 는 8자 이상')
        for pw in (self.auth, self.priv):
            if re.search(r'[\s?"\'\\]', pw):
                bad.append('SNMP 비밀번호에 공백·?·따옴표·\\ 는 쓰지 말 것 (장비 CLI 가 잘못 받아들임)')
                break
        if not self.collectors:
            bad.append('COLLECTOR_IPS 에 수집 서버(또는 PC) IP 를 넣을 것')
        if self.acl_only:
            bad = [b for b in bad if 'IPAM_SNMP_AUTH' not in b and '비밀번호에' not in b]
        if self.apply and not (self.ssh_user and self.ssh_pass):
            bad.append('--apply 에는 NET_USER / NET_PASS 필요')
        return bad


# --------------------------------------------------------------------- 벤더별 명령
def acl_commands(dtype, c):
    """수집 서버 IP 를 SNMP 허용 목록에 넣는 명령 (벤더마다 SNMP 접근 제한 방식이 다름)
    - Cisco IOS/XE : 표준 ACL(기본 IPAM-SNMP, --cisco-acl 로 기존 ACL 번호/이름 지정 가능) — group/user 에 연결
    - Cisco NX-OS  : ip access-list + snmp-server user ... use-ipv4acl
    - Juniper      : SNMPv3 는 lo0 방화벽 필터로만 제한 → 그 필터가 쓰는 prefix-list 이름(--junos-prefix-list)에 추가
    - HPE Comware  : 기본 ACL(--comware-acl, 기본 2999) + usm-user ... acl
    - ProCurve·Aruba CX : 장비에서 기존 제한을 읽어 판단 (push 단계) — 여기서는 명령 없음"""
    ips = c.collectors
    if dtype in ('cisco_ios', 'cisco_xe'):
        return [f'ip access-list standard {c.cisco_acl}'] + [f' permit {ip}' for ip in ips] + ['exit']
    if dtype == 'cisco_nxos':
        return ['ip access-list IPAM-SNMP'] + [f'permit ip {ip}/32 any' for ip in ips] + ['exit']
    if dtype == 'juniper_junos' and c.junos_prefix_list:
        return [f'set policy-options prefix-list {c.junos_prefix_list} {ip}/32' for ip in ips]
    if dtype == 'hp_comware' and c.comware_acl:
        return [f'acl basic {c.comware_acl}'] + [f'rule permit source {ip} 0' for ip in ips] + ['quit']
    return []


def account_commands(dtype, c, f=None):
    """SNMPv3 계정 명령 (ACL 제외). f = 장비에서 읽은 사실(inspect) — Comware 버전·Cisco ACL 이름 반영"""
    f = f or {}
    full = commands(dtype, _replace(c, acl_only=False, cisco_acl=f.get('acl') or c.cisco_acl,
                                     comware_acl=f.get('acl') or c.comware_acl))
    acl = acl_commands(dtype, _replace(c, cisco_acl=f.get('acl') or c.cisco_acl, comware_acl=f.get('acl') or c.comware_acl))
    cmds = full[len(acl):]
    if dtype == 'hp_comware' and f.get('ver') == 5:     # Comware 5 에는 'simple' 키워드가 없음(평문이 기본)
        cmds = [x.replace(' simple authentication-mode', ' authentication-mode') for x in cmds]
    return cmds


class _replace:
    """Cfg 를 복사하며 일부 값만 바꾼 읽기 전용 보기"""
    def __init__(self, base, **kw):
        self.__dict__.update(base.__dict__); self.__dict__.update(kw)


def commands(dtype, c):
    """넣을 명령 전체 = ACL(허용 IP) + SNMPv3 계정. --acl-only 면 ACL 만."""
    acl = acl_commands(dtype, c)
    if c.acl_only:
        return acl
    u, A, P = c.user, c.auth, c.priv
    if dtype in ('cisco_ios', 'cisco_xe'):
        n = c.cisco_acl
        return acl + ['snmp-server view IPAM-VIEW iso included',
                      f'snmp-server group IPAM-RO v3 priv read IPAM-VIEW access {n}',
                      f'snmp-server group IPAM-RO v3 priv context vlan- match prefix read IPAM-VIEW access {n}',
                      f'snmp-server user {u} IPAM-RO v3 auth sha {A} priv aes 128 {P} access {n}']
    if dtype == 'cisco_nxos':
        return acl + [f'snmp-server user {u} network-operator auth sha {A} priv aes-128 {P}',
                      f'snmp-server user {u} use-ipv4acl IPAM-SNMP']
    if dtype == 'juniper_junos':
        return acl + ['set snmp view IPAM-VIEW oid .1 include',
                      f'set snmp v3 usm local-engine user {u} authentication-sha authentication-password "{A}"',
                      f'set snmp v3 usm local-engine user {u} privacy-aes128 privacy-password "{P}"',
                      f'set snmp v3 vacm security-to-group security-model usm security-name {u} group IPAM-RO',
                      'set snmp v3 vacm access group IPAM-RO default-context-prefix security-model usm '
                      'security-level privacy read-view IPAM-VIEW']
    if dtype in ('hp_procurve', 'aruba_osswitch', 'aruba_procurve'):
        return ['snmpv3 enable', f'snmpv3 user {u} auth sha {A} priv aes {P}',
                f'snmpv3 group operatorpriv user {u} sec-model ver3']
    if dtype == 'aruba_aoscx':
        return [f'snmp-server vrf {v}' for v in c.aoscx_vrfs] + [
            f'snmpv3 user {u} auth sha auth-pass plaintext {A} priv aes priv-pass plaintext {P}']
    if dtype == 'hp_comware':
        tail = f' acl {c.comware_acl}' if c.comware_acl else ''
        return acl + ['snmp-agent', 'snmp-agent sys-info version v2c v3', 'snmp-agent mib-view included IPAM-VIEW iso',
                      'snmp-agent group v3 IPAM-RO privacy read-view IPAM-VIEW',
                      f'snmp-agent usm-user v3 {u} IPAM-RO simple authentication-mode sha {A} privacy-mode aes128 {P}{tail}']
    if dtype == 'aruba_os':
        return [] if c.acl_only else [f'snmp-server user {u} auth-prot sha {A} priv-prot AES {P}']
    raise ValueError(f'지원하지 않는 장비 종류: {dtype}')


def dynamic_acl(conn, dtype, c):
    """장비에 이미 걸린 관리 접근 제한을 읽어, 있으면 수집 서버 IP 를 추가할 명령/안내를 돌려준다.
    반환 (추가 명령 목록, 안내 문구)"""
    if dtype in ('hp_procurve', 'aruba_osswitch', 'aruba_procurve'):
        out = conn.send_command('show ip authorized-managers')
        if re.search(r'\d+\.\d+\.\d+\.\d+', out):   # 이미 허용 관리자 목록을 쓰는 장비 → 추가해야 SNMP 가 막히지 않음
            have = set(re.findall(r'\d+\.\d+\.\d+\.\d+', out))
            add = [f'ip authorized-managers {ip} 255.255.255.255 access operator' for ip in c.collectors if ip not in have]
            return add, f'authorized-managers 사용 중 → 수집 IP {len(add)}개 추가'
        return [], 'authorized-managers 미사용(제한 없음) → 추가 안 함'
    if dtype == 'aruba_aoscx':
        out = conn.send_command('show running-config | include control-plane')
        if 'access-list' in out:
            return [], '주의: control-plane ACL 사용 중 — 수집 IP 허용 줄을 그 ACL 의 deny 앞에 수동으로 추가해야 함'
        return [], 'control-plane ACL 미사용(제한 없음)'
    return [], ''


BACKUP_CMD = {'cisco_ios': 'show running-config | include snmp', 'cisco_xe': 'show running-config | include snmp',
              'cisco_nxos': 'show running-config snmp', 'juniper_junos': 'show configuration snmp | display set',
              'hp_procurve': 'show running-config | include snmp', 'aruba_osswitch': 'show running-config | include snmp',
              'aruba_procurve': 'show running-config | include snmp',
              'aruba_aoscx': 'show running-config | include snmp', 'hp_comware': 'display current-configuration | include snmp',
              'aruba_os': 'show running-config | include snmp'}


def mask(text, c):
    for s in (c.auth, c.priv, c.ssh_pass, c.secret, c.v2c):
        if s:
            text = text.replace(s, '****')
    return text


# --------------------------------------------------------------------- SNMP 확인/판별
def _snmp_get(host, port, oids, cred=None, community=None, timeout=2, retries=1):
    from pysnmp.hlapi.v3arch import asyncio as h

    async def go():
        eng = h.SnmpEngine()
        try:
            auth = h.CommunityData(community, mpModel=1) if community else h.UsmUserData(
                cred.user, cred.auth, cred.priv, authProtocol=h.usmHMACSHAAuthProtocol, privProtocol=h.usmAesCfb128Protocol)
            tgt = await h.UdpTransportTarget.create((host, port), timeout=timeout, retries=retries)
            err, st, _, vbs = await h.get_cmd(eng, auth, tgt, h.ContextData(),
                                              *[h.ObjectType(h.ObjectIdentity(o)) for o in oids], lookupMib=False)
            if err or st:
                return None, str(err or st.prettyPrint())
            return [v for _, v in vbs], None
        finally:
            eng.close_dispatcher()
    return asyncio.run(go())


def v3_ok(host, c):
    # 계정 비밀번호가 비어 있으면 pysnmp 가 키 계산 중 ZeroDivisionError 를 낸다 → 확인 자체를 하지 않음
    if not (c.user and c.auth and c.priv):
        return None, 'SNMP 계정 환경변수(IPAM_SNMP_USER/AUTH/PRIV) 없음 — SNMPv3 확인 생략'
    if len(c.auth) < 8 or len(c.priv) < 8:
        return None, 'SNMP 비밀번호는 8자 이상이어야 함 (IPAM_SNMP_AUTH/PRIV)'
    vals, err = _snmp_get(host, c.port, ['1.3.6.1.2.1.1.5.0'], cred=c)
    return (str(vals[0]), None) if vals else (None, err)


VERSION_PATTERNS = [   # 'show version' / 'display version' 출력 → 장비 종류 (위에서부터 먼저 맞는 것)
    (r'NX-OS', 'cisco_nxos'), (r'IOS[ -]XE', 'cisco_xe'), (r'Cisco IOS', 'cisco_ios'),
    (r'JUNOS|Junos', 'juniper_junos'), (r'Comware', 'hp_comware'),
    (r'ArubaOS-CX|AOS-CX|Aruba CX', 'aruba_aoscx'), (r'ArubaOS-Switch|ProCurve|HP J\d{4}|Aruba J\d{4}|Image stamp', 'hp_procurve'),
    (r'ArubaOS', 'aruba_os'),
]


def guess_from_version(text):
    for pat, t in VERSION_PATTERNS:
        if re.search(pat, text or ''):
            return t
    return None


def _probe_version(host, c, connect=None, telnet=False):
    """SSHDetect 가 못 알아낸 장비: 그냥 접속해 'show version' / 'display version' 을 보내 출력으로 판별"""
    if connect is None:
        from netmiko import ConnectHandler as connect
    params = dict(device_type='generic_telnet' if telnet else 'generic', host=host, username=c.ssh_user,
                  password=c.ssh_pass, timeout=20, fast_cli=False)
    with connect(**params) as conn:
        for cmd in ('show version', 'display version'):
            out = conn.send_command_timing(cmd, read_timeout=15)
            t = guess_from_version(out)
            if t:
                return t
            conn.send_command_timing('q', read_timeout=3)     # --More-- 에 걸렸으면 빠져나옴
    return None


def detect(host, c, given='', connect=None):
    if given:
        return given, '목록에 적힌 값'
    if c.v2c:
        vals, err = _snmp_get(host, c.port, ['1.3.6.1.2.1.1.2.0', '1.3.6.1.2.1.1.1.0'], community=c.v2c)
        if vals:
            oid, descr = tuple(vals[0]), str(vals[1])
            ent = oid[6] if len(oid) > 6 else None
            t = ENTERPRISE_TO_TYPE.get(ent)
            if t == 'cisco_ios' and 'NX-OS' in descr:
                t = 'cisco_nxos'
            if t == 'hp_procurve' and 'Comware' in descr:
                t = 'hp_comware'
            t = t or guess_from_version(descr)
            if t:
                return t, f'SNMP v2c sysObjectID(기업번호 {ent})'
    if not c.ssh_user:
        return None, '벤더를 알 수 없음 — NET_USER/NET_PASS 가 없어 SSH 판별도 못 함 (목록에 vendor 를 적거나 SNMP_V2C_COMMUNITY 지정)'
    err = ''
    try:
        from netmiko import SSHDetect
        g = SSHDetect(device_type='autodetect', host=host, username=c.ssh_user, password=c.ssh_pass,
                      secret=c.secret, timeout=20).autodetect()
        if g:
            return g, 'SSH 자동 판별'
    except Exception as e:
        err = f'{type(e).__name__}: {e}'
    # SSHDetect 가 모르는 화면(배너·프롬프트)이거나 실패 → 직접 version 명령으로 한 번 더
    tcp_dead = bool(err) and _tcp_fail(Exception(err))
    tries = ([False] if not tcp_dead else []) + ([True] if c.telnet and tcp_dead else [])
    probe_conn = connect if connect is not None and getattr(connect, 'probe', False) else None
    for tel in tries:
        try:
            t = _probe_version(host, c, probe_conn, telnet=tel)
            if t:
                return t, 'version 명령으로 판별' + (' (텔넷)' if tel else '')
            err = err or '접속은 됐지만 show/display version 출력으로 종류를 알 수 없음'
        except Exception as e:
            err = err or f'{type(e).__name__}: {e}'
    msg = f'SSH 자동 판별 실패: {err}' if err else 'SSH 자동 판별 실패: 장비 종류를 알 수 없음'
    if 'SSHException' in msg or 'Incompatible ssh' in msg:
        msg = LEGACY_SSH_HINT + ' | ' + msg
    elif _tcp_fail(Exception(msg)):
        msg = TCP_FAIL_HINT + ' | ' + msg
    else:
        msg += ' → 목록 엑셀의 vendor 칸에 적어 주세요 (cisco_ios, cisco_xe, juniper_junos, hp_comware, hp_procurve, aruba_aoscx …)'
    return None, msg


# --------------------------------------------------------------------- 적용
def _answer_prompts(conn, out, limit=4):
    """ProCurve 'snmpv3 enable' 같은 대화형 질문에는 n 으로 답한다."""
    for _ in range(limit):
        if not re.search(r'\[y/n\]|\(y/n\)|\? *$', out, re.I | re.M):
            break
        out = conn.send_command_timing('n', strip_prompt=False, strip_command=False)
    return out


TELNET = {'cisco_ios': 'cisco_ios_telnet', 'cisco_xe': 'cisco_xe_telnet', 'cisco_nxos': 'cisco_nxos_telnet',
          'juniper_junos': 'juniper_junos_telnet', 'hp_comware': 'hp_comware_telnet', 'hp_procurve': 'hp_procurve_telnet',
          'aruba_osswitch': 'hp_procurve_telnet', 'aruba_procurve': 'aruba_procurve_telnet'}
TCP_FAIL_HINT = ('SSH(22) 접속 자체가 안 됨 — 이 PC 가 장비 vty ACL(line vty access-class / Juniper lo0 필터 / '
                 'Comware user-interface acl)에 없거나, 장비에 SSH 가 꺼져 있거나, 경로·방화벽 문제. '
                 '예전에 접속되던 PC 에서 실행하거나, 텔넷만 되는 장비면 --telnet')


def _tcp_fail(e):
    m = f'{type(e).__name__}: {e}'
    return 'TCP connection' in m or 'NetmikoTimeoutException' in m or 'timed out' in m.lower()


def _open(connect, dtype, host, c):
    """SSH 로 접속, 안 되고 --telnet 이면 텔넷으로 다시. 반환 (연결, 실제 방식)"""
    params = dict(device_type=dtype, host=host, username=c.ssh_user, password=c.ssh_pass, timeout=30, fast_cli=False)
    if c.secret:
        params['secret'] = c.secret
    try:
        return connect(**params), 'ssh'
    except Exception as e:
        if c.telnet and _tcp_fail(e) and dtype in TELNET:
            return connect(**dict(params, device_type=TELNET[dtype])), 'telnet'
        raise


# ---- 장비에서 현재 SNMP·ACL 상태 읽기 ----------------------------------------------------------
def _seq_insert(lines, ips, kind):
    """ACL 출력에서 'deny any' 앞 빈 순번을 찾아 (순번, ip) 목록. 자리가 없으면 None.
    kind: 'cisco' ('    10 permit 1.2.3.4' / '    20 deny   any') · 'comware' (' rule 5 deny')"""
    if kind == 'cisco':
        ent = [(int(m.group(1)), m.group(2)) for m in re.finditer(r'^\s*(\d+)\s+(permit|deny)\b(.*)$', lines, re.M)]
        deny = [n for n, (sq, act) in enumerate(ent) if act == 'deny' and re.search(
            rf'^\s*{sq}\s+deny\s+(any|0\.0\.0\.0 255\.255\.255\.255)\b', lines, re.M)]
    else:
        ent = [(int(m.group(1)), m.group(2)) for m in re.finditer(r'^\s*rule (\d+) (permit|deny)\b', lines, re.M)]
        deny = [n for n, (sq, act) in enumerate(ent) if act == 'deny' and re.search(
            rf'^\s*rule {sq} deny(\s+source any)?\s*(\(|$|counting|logging)', lines, re.M)]
    if not deny:
        return [(None, ip) for ip in ips]                  # 끝에 붙여도 됨(명시 deny 없음 → 암묵 deny 앞)
    d = deny[0]
    hi = ent[d][0]
    lo = ent[d - 1][0] if d else 0
    free = [n for n in range(lo + 1, hi)]
    if len(free) < len(ips):
        return None
    step = max(1, len(free) // (len(ips) + 1))
    return [(free[min(len(free) - 1, step * (i + 1) - 1)], ip) for i, ip in enumerate(ips)]


def inspect(conn, dtype, c, bak):
    """반환 dict: user(계정 있음 True/False/None=모름), acl(쓰는 ACL 이름/번호), acl_text, ver, prefix_list, notes"""
    u, f = c.user, {'user': None, 'acl': None, 'acl_text': '', 'ver': None, 'prefix_list': None, 'notes': []}
    if dtype in ('cisco_ios', 'cisco_xe'):
        out = conn.send_command(f'show snmp user {u}')
        f['user'] = bool(re.search(rf'User name:\s*{re.escape(u)}\s*$', out, re.M))
        g = re.search(r'Group-?name:\s*(\S+)', out)
        run = conn.send_command('show running-config | include snmp-server group')
        grp = g.group(1) if g else 'IPAM-RO'
        acls = []
        um = re.search(r'(?<!IPv6 )access-list:\s*(\S+)', out)             # 사용자에 직접 걸린 ACL
        if um and um.group(1).lower() not in ('none', 'n/a'):
            acls.append(um.group(1))
        m = re.search(rf'^snmp-server group {re.escape(grp)} v3 \S+.*\baccess (\S+)', run, re.M)   # 그룹 ACL
        if m and m.group(1) != 'ipv6':
            acls.append(m.group(1))
        acls = list(dict.fromkeys(acls))
        f['acl'] = acls[0] if acls else None
        f['acls'] = acls
        f['acl_text'] = {a: conn.send_command(f'show ip access-lists {a}') for a in acls}
        if f['user'] and not acls:
            f['notes'].append(f'계정 {u}·그룹 {grp} 에 ACL 없음(SNMP 접근 제한 없음)')
    elif dtype == 'juniper_junos':
        f['user'] = bool(re.search(rf'usm local-engine user {re.escape(u)}(\s|$)', bak))
        if c.junos_prefix_list:
            f['prefix_list'] = c.junos_prefix_list
        else:
            lo = conn.send_command('show configuration interfaces lo0 | display set')
            filters = re.findall(r'family inet filter (?:input|input-list) (\S+)', lo)
            if not filters:
                f['notes'].append('lo0 필터 없음(SNMP 접근 제한 없음)')
            else:
                fw = conn.send_command('show configuration firewall | display set')
                terms = {}
                for m in re.finditer(r'^set firewall (?:family inet )?filter (\S+) term (\S+) (.*)$', fw, re.M):
                    if m.group(1) in filters:
                        terms.setdefault((m.group(1), m.group(2)), []).append(m.group(3))
                cand = set()
                for parts in terms.values():
                    t = ' '.join(parts)
                    if re.search(r'(port|destination-port) (snmp|161)\b', t) and 'then accept' in t:
                        cand.update(re.findall(r'from (?:source-prefix-list|prefix-list) (\S+)', t))
                if len(cand) == 1:
                    f['prefix_list'] = cand.pop()
                    f['notes'].append(f"lo0 필터 {','.join(filters)} 의 SNMP 허용 목록 = {f['prefix_list']}")
                else:
                    f['notes'].append(f"lo0 필터 {','.join(filters)} 에서 SNMP 허용 prefix-list 를 "
                                      f"{'여러 개 찾음: ' + ','.join(sorted(cand)) if cand else '못 찾음'} "
                                      '→ --junos-prefix-list 로 지정')
    elif dtype == 'hp_comware':
        v = conn.send_command('display version')
        m = re.search(r'Comware\s+(?:Platform\s+)?Software,?\s+Version\s+(\d+)', v, re.I)
        f['ver'] = int(m.group(1)) if m else 7
        um = re.search(rf'usm-user v3 {re.escape(u)} (\S+)(.*)$', bak, re.M)
        f['user'] = bool(um)
        acls = []
        if um:
            a1 = re.search(r'\bacl (\d+)', um.group(2))
            gm = re.search(rf'snmp-agent group v3 {re.escape(um.group(1))}\b.*\bacl (\d+)', bak)
            acls = [x.group(1) for x in (a1, gm) if x]
        f['acl'] = acls[0] if acls else None
        f['acls'] = sorted(set(acls))
        f['acl_text'] = {a: conn.send_command(f'display acl {a}') for a in f['acls']}
    return f


def plan(dtype, c, f, preview):
    """장비 상태(f)를 보고 실제로 넣을 명령을 정한다. 반환 (명령, 안내)"""
    notes = list(f.get('notes', []))
    ips = c.collectors
    have_keys = bool(c.auth and c.priv)
    need_account = not c.acl_only
    if c.acl_only and f.get('user') is False:
        if not have_keys:
            raise NeedAction(f'장비에 SNMPv3 계정({c.user})이 없음 — ACL 만으로는 응답 불가. '
                             'IPAM_SNMP_USER/AUTH/PRIV 를 넣고 --acl-only 없이 --force 로 실행')
        need_account = True
        notes.append(f'장비에 계정 {c.user} 이 없어 계정도 추가')
    cmds = []
    if dtype in ('cisco_ios', 'cisco_xe'):
        names = f.get('acls') or [c.cisco_acl]
        for name in names:
            text = (f.get('acl_text') or {}).get(name, '')
            add = [ip for ip in ips if not re.search(rf'\b(host )?{re.escape(ip)}(\s|,|$)', text)]
            if not add:
                notes.append(f'ACL {name}: 수집 IP 이미 있음')
                continue
            ext = text.lstrip().startswith('Extended')
            slots = _seq_insert(text, add, 'cisco')
            if slots is None:
                cmds.append(f'ip access-list resequence {name} 10 10')
                text = '\n'.join(f'    {10 * (i + 1)} {m.group(1)}{m.group(2)}' for i, m in
                                 enumerate(re.finditer(r'^\s*\d+\s+(permit|deny)(.*)$', text, re.M)))
                slots = _seq_insert(text, add, 'cisco') or [(None, ip) for ip in add]
                notes.append(f'ACL {name} 순번을 10 단위로 다시 매김')
            cmds += [f'ip access-list {"extended" if ext else "standard"} {name}'] + [
                (f' {sq} ' if sq else ' ') + (f'permit ip host {ip} any' if ext else f'permit {ip}') for sq, ip in slots] + ['exit']
            if any(sq for sq, _ in slots):
                notes.append(f"ACL {name}: 'deny any' 앞에 수집 IP {len(add)}개 추가")
            if name != c.cisco_acl and f.get('acls'):
                notes.append(f'장비의 SNMP 계정·그룹이 실제로 쓰는 ACL {name} 에 추가')
        if need_account:
            cmds += account_commands(dtype, c, f)
    elif dtype == 'juniper_junos':
        pl = f.get('prefix_list')
        if pl:
            cmds += [f'set policy-options prefix-list {pl} {ip}/32' for ip in ips]
        if need_account:
            cmds += account_commands(dtype, c, f)
    elif dtype == 'hp_comware':
        acls = f.get('acls') or ([c.comware_acl] if c.comware_acl else [])
        head = 'acl number' if f.get('ver') == 5 else 'acl basic'
        for a in acls:
            text = (f.get('acl_text') or {}).get(a, '')
            add = [ip for ip in ips if f'source {ip} 0' not in text]
            if not add:
                notes.append(f'ACL {a}: 수집 IP 이미 있음')
                continue
            slots = _seq_insert(text, add, 'comware')
            if slots is None:
                notes.append(f"ACL {a}: 'deny' 앞에 넣을 rule 번호 여유 없음 — 장비에서 직접 추가 필요")
                continue
            cmds += [f'{head} {a}'] + [f'rule {str(sq) + " " if sq is not None else ""}permit source {ip} 0'
                                        for sq, ip in slots] + ['quit']
        if f.get('ver') == 5:
            notes.append('Comware 5 문법(acl number)')
        if need_account:
            cmds += account_commands(dtype, c, dict(f, acl=acls[0] if acls else None))
    else:
        cmds = preview if not c.acl_only or not need_account else acl_commands(dtype, c) + account_commands(dtype, c, f)
    return cmds, notes


class NeedAction(Exception):
    pass


def push(host, dtype, cmds, c, backup_dir, connect=None):
    """접속 → 백업 → 장비 상태 확인(inspect) → 실제 명령 결정(plan) → 적용 → 저장. 반환 (출력, 넣은 명령, 안내)"""
    if connect is None:
        from netmiko import ConnectHandler as connect
    log, notes = [], []
    conn, how = _open(connect, dtype, host, c)
    if how == 'telnet':
        notes.append('SSH 안 됨 → 텔넷으로 접속')
    with conn:
        if c.secret and dtype.startswith('cisco'):
            conn.enable()
        bak = ''
        try:
            bak = conn.send_command(BACKUP_CMD[dtype])
            os.makedirs(backup_dir, exist_ok=True)
            with open(os.path.join(backup_dir, f'{host}.txt'), 'w', encoding='utf-8') as fh:
                fh.write(bak)
        except Exception as e:
            log.append(f'백업 실패(계속 진행): {e}')
        f = inspect(conn, dtype, c, bak)
        cmds, n2 = plan(dtype, c, f, cmds)
        notes += n2
        extra, note = dynamic_acl(conn, dtype, c)
        if note:
            notes.append(note)
        if dtype in ('hp_procurve', 'aruba_osswitch', 'aruba_procurve'):
            conn.config_mode()
            if cmds and cmds[0] == 'snmpv3 enable':
                log.append(_answer_prompts(conn, conn.send_command_timing(cmds[0], strip_prompt=False, strip_command=False)))
                cmds = cmds[1:]
            if cmds + extra:
                log.append(conn.send_config_set(cmds + extra, exit_config_mode=False))
            conn.exit_config_mode()
        elif cmds + extra:
            log.append(conn.send_config_set(cmds + extra, exit_config_mode=dtype != 'juniper_junos'))
        else:
            return '\n'.join(log) + '\n(넣을 명령 없음)', [], notes, f
        out = '\n'.join(log)
        bad = re.search(r'^.*(% ?Invalid|% ?Incomplete|syntax error|Unrecognized command|unknown command|Error:|'
                        r'Wrong parameter|% ?Too many parameters|% ?Ambiguous).*$', out, re.I | re.M)
        if bad:
            # 거부된 줄 바로 앞 명령을 함께 보여 준다
            pre = out[:bad.start()].rstrip().splitlines()[-2:]
            raise RuntimeError('장비가 명령을 거부: ' + ' / '.join(x.strip() for x in pre + [bad.group(0)]))
        if dtype == 'juniper_junos':
            out += conn.commit(and_quit=True)
        else:
            out += conn.save_config()
    return out, cmds + extra, notes, f


def _local_ip(host, port=161):
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect((host, port))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return ''


def handle(row, c, backup_dir, connect=None):
    host, name = str(row['ip']).strip(), str(row.get('name') or '').strip()
    res = {'ip': host, 'name': name, 'vendor': '', 'how': '', 'status': '', 'detail': '', 'commands': ''}
    try:
        if not c.force and c.auth and c.priv:
            sysname, _ = v3_ok(host, c)
            if sysname is not None:
                res.update(status='이미 설정됨', detail=f'SNMPv3 응답: {sysname}')
                return res
        dtype, how = detect(host, c, str(row.get('vendor') or '').strip(), connect)
        res.update(vendor=dtype or '', how=how)
        if not dtype:
            res.update(status='판별 실패', detail=how)
            return res
        if dtype not in SUPPORTED:
            res.update(status='지원 안 함', detail=f'{dtype} — 수동 설정 필요')
            return res
        cmds = commands(dtype, c)
        res['commands'] = mask('\n'.join(cmds), c)
        if not c.apply:
            res['commands'] += '\n(적용할 때 장비의 현재 계정·ACL·버전을 읽고 명령을 다시 정함 — 결과 파일에 실제 넣은 명령이 남음)'
            res.update(status='점검(미적용)', detail='--apply 로 실행하면 위 명령을 넣음')
            return res
        out, used, notes, f = push(host, dtype, cmds, c, backup_dir, connect)
        res['commands'] = mask('\n'.join(used), c) or '(넣을 명령 없음)'
        note = '; '.join(notes)
        if c.acl_only and not (c.auth and c.priv):
            res.update(status='ACL 적용(확인 생략)', detail=note or 'SNMP 계정 환경변수가 없어 SNMPv3 확인은 건너뜀')
            return res
        err = ''
        for i in range(4):  # 장비가 계정을 반영하는 데 몇 초 걸릴 수 있음
            time.sleep(2 + i * 2)
            sysname, err = v3_ok(host, c)
            if sysname is not None:
                res.update(status='적용 완료', detail=f'SNMPv3 응답 확인: {sysname}' + (f' / {note}' if note else ''))
                return res
        me = _local_ip(host, c.port)
        why = []
        if me and me not in c.collectors:
            why.append(f'확인 요청은 이 PC({me})에서 보냄 — 이 PC 는 COLLECTOR_IPS 에 없어 ACL 에 막히는 것이 정상일 수 있음. '
                       '수집 서버(NetBox 스크립트 \'SNMP 수집 점검\' → IP 직접 입력)에서 확인')
        if 'Unknown USM user' in (err or '') or 'unknownUserName' in (err or ''):
            if f.get('user') is False:
                why.append(f'장비에 계정 {c.user} 없음')
            elif f.get('user'):
                why.append(f'장비에 계정 {c.user} 은 있음 → 사용자/그룹 ACL 이 확인 PC 를 막거나 계정이 다른 엔진ID로 만들어짐. '
                           '--acl-only 없이 --force 로 계정을 다시 만들면 해결')
            else:
                why.append(f'이 PC 의 IPAM_SNMP_USER({c.user})가 장비 계정 이름과 다를 수 있음')
        elif err and ('timeout' in err.lower() or 'No SNMP response' in err):
            why.append('응답 없음 — ACL·방화벽·VRF(관리 VRF 로만 SNMP 수신) 확인')
        res.update(status='적용 후 확인 실패', detail=f'SNMPv3 응답 없음({err}). ' + ' / '.join(why)
                   + (f' / {note}' if note else ''))
    except NeedAction as e:
        res.update(status='조치 필요', detail=str(e))
    except Exception as e:
        msg = f'{type(e).__name__}: {e}'
        if _tcp_fail(e):
            msg = TCP_FAIL_HINT + ' | ' + msg
        res.update(status='오류', detail=mask(msg, c)[:600])
    if 'SSHException' in res['detail'] or 'Incompatible ssh' in res['detail']:
        res['detail'] = (LEGACY_SSH_HINT + ' | ' + res['detail'])[:600]
    return res


LEGACY_SSH_HINT = ('SSH 협상 실패 — 구형 장비(diffie-hellman-group1/14-sha1, ssh-rsa, ssh-dss)라면 '
                   "paramiko 3.x 가 필요: pip install 'paramiko<4'")


def _paramiko_note():
    """paramiko 4.0 이상은 구형 스위치가 쓰는 SSH 알고리즘(group1/14-sha1, ssh-rsa, ssh-dss)을 지원하지 않는다"""
    try:
        import paramiko
        major = int(paramiko.__version__.split('.')[0])
    except Exception:
        return ''
    if major >= 4:
        return (f"※ paramiko {paramiko.__version__}: 구형 장비 SSH 접속이 실패할 수 있음 "
                f"→ {sys.executable} -m pip install 'paramiko<4'")
    return ''


# --------------------------------------------------------------------- 목록
def load_inventory(a):
    if a.from_netbox:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from nb import get_all
        rows = []
        for d in get_all('/dcim/devices/', has_primary_ip='true', status='active'):
            rows.append({'ip': d['primary_ip4']['address'].split('/')[0] if d.get('primary_ip4') else '',
                         'name': d['name'], 'vendor': ''})
        df = pd.DataFrame([r for r in rows if r['ip']])
    else:
        p = a.inventory
        df = pd.read_csv(p, dtype=str) if p.lower().endswith('.csv') else pd.read_excel(p, dtype=str)
        df.columns = [str(x).strip().lower() for x in df.columns]
        if 'ip' not in df.columns:
            raise SystemExit("목록 파일에 'ip' 열이 필요합니다")
    df = df.fillna('')
    if a.only_failed:
        prev = pd.read_excel(a.only_failed, dtype=str).fillna('')
        retry = set(prev.loc[~prev['status'].isin(['적용 완료', '이미 설정됨']), 'ip'])
        df = df[df['ip'].isin(retry)].copy()
        if 'vendor' in prev.columns:          # 지난번에 알아낸 장비 종류는 다시 판별하지 않고 그대로 사용
            known = {r['ip']: r['vendor'] for _, r in prev.iterrows() if r.get('vendor')}
            if 'vendor' not in df.columns:
                df['vendor'] = ''
            df['vendor'] = [v or known.get(ip, '') for ip, v in zip(df['ip'], df['vendor'])]
    if a.limit:
        df = df.head(a.limit)
    return df.to_dict('records')


def main(argv=None, connect=None):
    ap = argparse.ArgumentParser(description='SNMPv3 계정 일괄 설정')
    ap.add_argument('inventory', nargs='?', help='장비 목록 엑셀/CSV (열: ip, name, vendor)')
    ap.add_argument('--from-netbox', action='store_true', help='NetBox 장치(기본 IP 있는 활성 장치)를 대상으로')
    ap.add_argument('--apply', action='store_true', help='실제로 장비에 적용 (없으면 점검만)')
    ap.add_argument('--force', action='store_true', help='이미 SNMPv3 응답하는 장비도 다시 적용')
    ap.add_argument('--limit', type=int, default=0, help='앞에서부터 N대만')
    ap.add_argument('--only-failed', metavar='결과파일', help='이전 결과에서 완료되지 않은 장비만 다시')
    ap.add_argument('--workers', type=int, default=10, help='동시 작업 수')
    ap.add_argument('--snmp-port', type=int, default=161)
    ap.add_argument('--aoscx-vrf', default='default,mgmt', help="Aruba CX 에서 SNMP 를 켤 VRF (기본 'default,mgmt')")
    ap.add_argument('--acl-only', action='store_true', help='SNMP 계정은 두고 수집 서버 IP 허용(ACL)만 추가 (서버 IP 추가·변경 시)')
    ap.add_argument('--cisco-acl', default='IPAM-SNMP', help="Cisco 에서 쓸 표준 ACL 이름/번호 (기존 SNMP ACL 을 쓰려면 예: 10)")
    ap.add_argument('--junos-prefix-list', default='', help='Juniper lo0 필터가 SNMP 허용에 쓰는 prefix-list 이름')
    ap.add_argument('--comware-acl', default='2999', help="Comware 기본 ACL 번호 (0 이면 ACL 안 씀)")
    ap.add_argument('--telnet', action='store_true', help='SSH(22) 접속이 안 되는 장비는 텔넷(23)으로 다시 시도 (비밀번호가 평문으로 전송됨)')
    ap.add_argument('--out', default='', help='결과 파일 이름')
    a = ap.parse_args(argv)
    if not a.inventory and not a.from_netbox:
        ap.error('장비 목록 파일 또는 --from-netbox 가 필요합니다')
    if a.comware_acl in ('0', ''):
        a.comware_acl = ''
    c = Cfg(a)
    bad = c.check()
    if bad:
        raise SystemExit('설정 확인:\n  - ' + '\n  - '.join(bad))
    rows = load_inventory(a)
    stamp = dt.datetime.now().strftime('%Y%m%d_%H%M%S')
    out = a.out or f'result_{stamp}.xlsx'
    backup_dir = f'backup_{stamp}'
    print(f"{'적용' if a.apply else '점검'} 대상 {len(rows)}대, 동시 {a.workers}대")
    if _paramiko_note():
        print(_paramiko_note())
    results = []
    with ThreadPoolExecutor(a.workers) as ex:
        for i, r in enumerate(ex.map(lambda row: handle(row, c, backup_dir, connect), rows), 1):
            results.append(r)
            print(f"[{i}/{len(rows)}] {r['ip']:<16} {r['vendor']:<14} {r['status']} {r['detail'][:80]}")
    df = pd.DataFrame(results)
    df.to_excel(out, index=False)
    print('\n상태별:', df['status'].value_counts().to_dict())
    print(f'결과 파일: {out}' + (f' / 적용 전 snmp 설정 백업: {backup_dir}/' if a.apply else ''))
    return results


if __name__ == '__main__':
    main()
