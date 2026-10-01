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
    vals, err = _snmp_get(host, c.port, ['1.3.6.1.2.1.1.5.0'], cred=c)
    return (str(vals[0]), None) if vals else (None, err)


def detect(host, c, given=''):
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
            if t:
                return t, f'SNMP v2c sysObjectID(기업번호 {ent})'
    if c.ssh_user:
        try:
            from netmiko import SSHDetect
            g = SSHDetect(device_type='autodetect', host=host, username=c.ssh_user, password=c.ssh_pass,
                          secret=c.secret, timeout=20).autodetect()
            if g:
                return g, 'SSH 자동 판별'
        except Exception as e:
            return None, f'SSH 자동 판별 실패: {e}'
    return None, '벤더를 알 수 없음 (목록에 vendor 를 적거나 SNMP_V2C_COMMUNITY 지정)'


# --------------------------------------------------------------------- 적용
def _answer_prompts(conn, out, limit=4):
    """ProCurve 'snmpv3 enable' 같은 대화형 질문에는 n 으로 답한다."""
    for _ in range(limit):
        if not re.search(r'\[y/n\]|\(y/n\)|\? *$', out, re.I | re.M):
            break
        out = conn.send_command_timing('n', strip_prompt=False, strip_command=False)
    return out


def push(host, dtype, cmds, c, backup_dir, connect=None):
    if connect is None:
        from netmiko import ConnectHandler as connect
    params = dict(device_type=dtype, host=host, username=c.ssh_user, password=c.ssh_pass, timeout=30,
                  fast_cli=False)
    if c.secret:
        params['secret'] = c.secret
    log = []
    with connect(**params) as conn:
        if c.secret and dtype.startswith('cisco'):
            conn.enable()
        try:
            bak = conn.send_command(BACKUP_CMD[dtype])
            os.makedirs(backup_dir, exist_ok=True)
            with open(os.path.join(backup_dir, f'{host}.txt'), 'w', encoding='utf-8') as f:
                f.write(bak)
        except Exception as e:
            log.append(f'백업 실패(계속 진행): {e}')
        extra, note = dynamic_acl(conn, dtype, c)
        if note:
            log.append('[ACL] ' + note)
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
            return '\n'.join(log) + '\n(넣을 명령 없음)'
        out = '\n'.join(log)
        if re.search(r'% ?Invalid|% ?Incomplete|syntax error|Unrecognized command|unknown command|Error:|Wrong parameter', out, re.I):
            raise RuntimeError('장비가 명령을 거부: ' + out[-400:])
        if dtype == 'juniper_junos':
            out += conn.commit(and_quit=True)
        else:
            out += conn.save_config()
    return out


def handle(row, c, backup_dir, connect=None):
    host, name = str(row['ip']).strip(), str(row.get('name') or '').strip()
    res = {'ip': host, 'name': name, 'vendor': '', 'how': '', 'status': '', 'detail': '', 'commands': ''}
    try:
        if not c.force and c.auth and c.priv:
            sysname, _ = v3_ok(host, c)
            if sysname is not None:
                res.update(status='이미 설정됨', detail=f'SNMPv3 응답: {sysname}')
                return res
        dtype, how = detect(host, c, str(row.get('vendor') or '').strip())
        res.update(vendor=dtype or '', how=how)
        if not dtype:
            res.update(status='판별 실패', detail=how)
            return res
        if dtype not in SUPPORTED:
            res.update(status='지원 안 함', detail=f'{dtype} — 수동 설정 필요')
            return res
        cmds = commands(dtype, c)
        res['commands'] = mask('\n'.join(cmds), c)
        if dtype in ('hp_procurve', 'aruba_osswitch', 'aruba_procurve', 'aruba_aoscx'):
            res['commands'] += ('\n' if cmds else '') + '(ACL: 적용할 때 장비의 기존 접근 제한을 읽어 필요하면 수집 IP 추가)'
        if dtype == 'juniper_junos' and not c.junos_prefix_list:
            res['commands'] += '\n(ACL: lo0 필터가 SNMP 를 막고 있다면 --junos-prefix-list 로 그 prefix-list 이름 지정)'
        if not c.apply:
            res.update(status='점검(미적용)', detail='--apply 로 실행하면 위 명령을 넣음')
            return res
        out = push(host, dtype, cmds, c, backup_dir, connect)
        acl_note = '; '.join(l[6:] for l in out.splitlines() if l.startswith('[ACL] '))
        for i in range(4):  # 장비가 계정을 반영하는 데 몇 초 걸릴 수 있음
            time.sleep(2 + i * 2)
            sysname, err = v3_ok(host, c)
            if sysname is not None:
                res.update(status='적용 완료', detail=f'SNMPv3 응답 확인: {sysname}' + (f' / {acl_note}' if acl_note else ''))
                return res
            if c.acl_only and not (c.auth and c.priv):
                break
        if c.acl_only and not (c.auth and c.priv):
            res.update(status='ACL 적용(확인 생략)', detail=acl_note or 'SNMP 계정 환경변수가 없어 SNMPv3 확인은 건너뜀')
            return res
        res.update(status='적용 후 확인 실패', detail=f'SNMPv3 응답 없음({err}) — ACL·방화벽·VRF 확인. '
                   + (acl_note + '. ' if acl_note else '') + '장비 출력: ' + mask(out[-300:], c))
    except Exception as e:
        res.update(status='오류', detail=mask(f'{type(e).__name__}: {e}', c)[:500])
    return res


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
        df = df[df['ip'].isin(retry)]
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
