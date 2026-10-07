"""snmp_bulk_config.py 검증 — 실제 장비 대신 가짜 SSH 연결 + SNMP 시뮬레이터 사용 (개발용)"""
import os, re, sys, tempfile
import pandas as pd
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import snmp_bulk_config as B

R = {'p': 0, 'f': 0}
def ok(c, n, x=''):
    R['p' if c else 'f'] += 1; print(('  PASS ' if c else '  FAIL ') + n + ('' if c else f'  [{x}]'))

os.environ.update(IPAM_SNMP_USER='ipam-ro', IPAM_SNMP_AUTH='Auth#Pass2026', IPAM_SNMP_PRIV='Priv#Pass2026',
                  COLLECTOR_IPS='165.246.12.104,165.246.1.50', NET_USER='netadmin', NET_PASS='SshPass!9', SNMP_V2C_COMMUNITY='public')
CONFIGURED, SESSIONS, HOSTCFG = set(), [], {}
real_v3_ok = B.v3_ok
def fake_v3_ok(host, c):
    if host in CONFIGURED:
        return 'sw-' + host, None
    return real_v3_ok(host, c)
B.v3_ok = fake_v3_ok

class FakeConn:
    def __init__(self, **kw):
        self.kw, self.sent, self.mode = kw, [], None
        SESSIONS.append(self)
    def __enter__(self): return self
    def __exit__(self, *a): pass
    def enable(self):
        self.sent.append('<enable>')
        if self.kw['host'] == '127.0.0.46':
            raise ValueError('Failed to enter enable mode. Please ensure you pass the \'secret\' argument')
        self.en = True
    def check_enable_mode(self): return getattr(self, 'en', False)
    def send_command(self, cmd):
        self.sent.append('<show> ' + cmd)
        if cmd == 'show ip authorized-managers':
            return ' IPV4 Authorized Managers\n Address : 165.246.1.10\n Mask    : 255.255.255.255\n Access  : Manager' if self.kw['host'] == '127.0.0.21' else ' IPV4 Authorized Managers\n'
        if 'control-plane' in cmd:
            return 'apply access-list ip MGMT control-plane vrf default' if self.kw['host'] == '127.0.0.22' else ''
        h = self.kw['host']
        if h in HOSTCFG and cmd in HOSTCFG[h]:
            return HOSTCFG[h][cmd]
        conf = h in CONFIGURED
        if cmd.startswith('show snmp user'):
            return 'User name: ipam-ro\nEngine ID: 800000090300\nGroup-name: IPAM-RO' if conf else ''
        if cmd == 'show running-config | include snmp-server group':
            return 'snmp-server group IPAM-RO v3 priv read IPAM-VIEW access IPAM-SNMP' if conf else ''
        if cmd == 'show ip access-lists IPAM-SNMP':
            return 'Standard IP access list IPAM-SNMP\n    10 permit 165.246.12.104\n    20 permit 165.246.1.50'
        if cmd == 'show configuration snmp | display set':
            return 'set snmp v3 usm local-engine user ipam-ro authentication-sha authentication-key "$9$x"' if conf else ''
        if cmd == 'display version':
            return 'Comware Software, Version 7.1.045, Release 3208P05'
        if cmd == 'display current-configuration | include snmp':
            return ' snmp-agent usm-user v3 ipam-ro IPAM-RO cipher authentication-mode sha $c$3$x privacy-mode aes128 $c$3$y acl 2999' if conf else ''
        return 'snmp-server community xxxx RO'
    def config_mode(self): self.sent.append('<config>')
    def exit_config_mode(self): self.sent.append('<end>')
    def send_command_timing(self, cmd, **kw):
        self.sent.append(cmd)
        if cmd == 'snmpv3 enable':
            return 'SNMPv3 Initialization process.\nCreating user \'initial\'\nWould you like to create a user that uses SHA? [y/n]'
        if cmd == 'n' and self.sent.count('n') == 1:
            return 'User creation is done. SNMPv3 is now functional.\nWould you like to restrict SNMPv1 and SNMPv2c messages to have read only access (you can set this later by the command \'snmp restrict-access\')? [y/n]'
        return 'switch(config)#'
    def send_config_set(self, cmds, exit_config_mode=True):
        if self.kw['host'] == '127.0.0.47':
            raise ValueError('Failed to enter configuration mode.')
        self.sent.extend(cmds)
        h = self.kw['host']
        if h == '127.0.0.49' and any(' simple ' in x for x in cmds):
            x = next(x for x in cmds if ' simple ' in x)
            return f"[V170-2]{x}\n" + ' ' * (8 + x.index(' simple ') + 1) + "^\n % Unrecognized command found at '^' position."
        if h == '127.0.0.51' and any('usm-user' in x for x in cmds):
            x = next(x for x in cmds if 'usm-user' in x)
            return f"[SW51]{x}\n" + ' ' * (6 + x.index('aes128')) + "^\n % Unrecognized command found at '^' position."
        if self.kw['host'] == '127.0.0.48' and any(x.startswith('acl basic') for x in cmds):
            return "system-view\nSystem View: return to User View with Ctrl+Z.\n[SW48]acl basic 2999\n             ^\n % Unrecognized command found at '^' position."

        if self.kw['host'] == '127.0.0.30':
            return '% Invalid input detected at \'^\' marker.'
        return 'ok'
    def commit(self, and_quit=False): self.sent.append('<commit>'); CONFIGURED.add(self.kw['host']); return 'commit complete'
    def save_config(self): self.sent.append('<save>'); CONFIGURED.add(self.kw['host']); return 'saved'

inv = pd.DataFrame([
    {'ip': '127.0.0.11', 'name': 'core-ex9208', 'vendor': ''},          # 이미 SNMPv3 응답 → 건너뜀
    {'ip': '127.0.0.17', 'name': 'sw-cisco-a', 'vendor': ''},           # v3 실패 → v2c 로 Cisco 판별 → 적용
    {'ip': '127.0.0.20', 'name': 'sw-junos', 'vendor': 'juniper_junos'},
    {'ip': '127.0.0.21', 'name': 'sw-procurve', 'vendor': 'hp_procurve'},
    {'ip': '127.0.0.22', 'name': 'sw-cx', 'vendor': 'aruba_aoscx'},
    {'ip': '127.0.0.23', 'name': 'sw-comware', 'vendor': 'hp_comware'},
    {'ip': '127.0.0.30', 'name': 'sw-reject', 'vendor': 'cisco_ios'},   # 장비가 명령 거부
    {'ip': '127.0.0.31', 'name': 'sw-odd', 'vendor': 'extreme_exos'},   # 지원 안 함
])
d = tempfile.mkdtemp(); os.chdir(d)
inv.to_excel('devices.xlsx', index=False)

print('== 점검 모드 (장비 변경 없음)')
res = {r['ip']: r for r in B.main(['devices.xlsx', '--snmp-port', '1161'], connect=FakeConn)}
ok(not SESSIONS, '점검 모드는 SSH 접속 자체를 하지 않음', len(SESSIONS))
ok(res['127.0.0.11']['status'] == '이미 설정됨', '이미 SNMPv3 응답하는 장비는 건너뜀', res['127.0.0.11'])
ok(res['127.0.0.17']['vendor'] == 'cisco_ios' and 'v2c' in res['127.0.0.17']['how'], '목록에 벤더가 없으면 v2c sysObjectID로 Cisco 판별', res['127.0.0.17'])
ok(res['127.0.0.17']['status'] == '점검(미적용)' and 'permit 165.246.12.104' in res['127.0.0.17']['commands']
   and 'context vlan- match prefix' in res['127.0.0.17']['commands'], 'Cisco 명령 미리보기: ACL·VLAN context 포함')
ok('Auth#Pass2026' not in ''.join(r['commands'] + r['detail'] for r in res.values()), '결과 파일에 비밀번호가 남지 않음(****)')
ok(res['127.0.0.31']['status'] == '지원 안 함', '지원하지 않는 장비는 표시만')
out = sorted(f for f in os.listdir('.') if f.startswith('result_'))
ok(out and len(pd.read_excel(out[-1])) == 8, '결과 엑셀 파일 생성(8행)', out)

print('== 적용 모드')
res = {r['ip']: r for r in B.main(['devices.xlsx', '--apply', '--snmp-port', '1161', '--workers', '4'], connect=FakeConn)}
by = {s.kw['host']: s for s in SESSIONS}
ok(res['127.0.0.17']['status'] == '적용 완료' and '<save>' in by['127.0.0.17'].sent, 'Cisco: 적용 → 저장 → SNMPv3 확인', res['127.0.0.17'])
ok('<commit>' in by['127.0.0.20'].sent and res['127.0.0.20']['status'] == '적용 완료', 'Juniper: set 명령 → commit', by['127.0.0.20'].sent)
pc = by['127.0.0.21'].sent
ok(pc.count('n') == 2 and res['127.0.0.21']['status'] == '적용 완료', "ProCurve: 'snmpv3 enable' 질문 2개에 n 으로 답하고 진행", pc)
ok('snmp-server vrf default' in by['127.0.0.22'].sent and 'snmp-server vrf mgmt' in by['127.0.0.22'].sent, 'Aruba CX: default·mgmt VRF 에 SNMP 켜기')
ok(any('privacy-mode aes128' in x for x in by['127.0.0.23'].sent), 'Comware: usm-user v3 명령')
ok(res['127.0.0.30']['status'] == '오류' and '거부' in res['127.0.0.30']['detail'] and '<save>' not in by['127.0.0.30'].sent,
   '장비가 명령을 거부하면 저장하지 않고 오류로 표시', res['127.0.0.30'])
ok('127.0.0.11' not in by, '이미 설정된 장비에는 접속하지 않음')
bk = [f for f in os.listdir('.') if f.startswith('backup_')]
ok(bk and os.path.exists(os.path.join(bk[0], '127.0.0.17.txt')), '적용 전 기존 snmp 설정을 장비별 파일로 백업')

print('== ACL')
r17 = res['127.0.0.17']
ok('ip access-list standard IPAM-SNMP' in by['127.0.0.17'].sent and ' permit 165.246.1.50' in by['127.0.0.17'].sent, 'Cisco: 수집 IP 2개를 표준 ACL 에 추가, group/user 에 연결')
ok('ip authorized-managers 165.246.12.104 255.255.255.255 access operator' in by['127.0.0.21'].sent
   and 'authorized-managers 사용 중' in res['127.0.0.21']['detail'], 'ProCurve: 허용 관리자 목록을 쓰는 장비면 수집 IP 추가', res['127.0.0.21'])
ok('control-plane ACL 사용 중' in res['127.0.0.22']['detail'], 'Aruba CX: control-plane ACL 사용 중이면 수동 추가 안내', res['127.0.0.22'])
ok('rule permit source 165.246.12.104 0' in by['127.0.0.23'].sent and any(x.endswith(' acl 2999') for x in by['127.0.0.23'].sent),
   'Comware: ACL 2999 + usm-user ... acl 2999', by['127.0.0.23'].sent)

print('== 실패분만 다시')
last = sorted(f for f in os.listdir('.') if f.startswith('result_'))[-1]
SESSIONS.clear()
res = B.main(['devices.xlsx', '--apply', '--snmp-port', '1161', '--only-failed', last], connect=FakeConn)
ok(sorted(r['ip'] for r in res) == ['127.0.0.30', '127.0.0.31'], '--only-failed: 완료·이미 설정 장비는 제외하고 재시도', [r['ip'] for r in res])

print('== ACL 만 추가 (--acl-only --force)')
SESSIONS.clear()
os.environ['COLLECTOR_IPS'] = '10.9.9.9'
res = {r['ip']: r for r in B.main(['devices.xlsx', '--apply', '--acl-only', '--force', '--snmp-port', '1161',
                                     '--junos-prefix-list', 'SNMP-CLIENTS'], connect=FakeConn)}
by = {s.kw['host']: s for s in SESSIONS}
ok(' permit 10.9.9.9' in by['127.0.0.17'].sent and not any('snmp-server user' in x for x in by['127.0.0.17'].sent),
   'Cisco: SNMP 계정은 건드리지 않고 ACL 만', by['127.0.0.17'].sent)
ok('set policy-options prefix-list SNMP-CLIENTS 10.9.9.9/32' in by['127.0.0.20'].sent and '<commit>' in by['127.0.0.20'].sent,
   'Juniper: 지정한 prefix-list 에 수집 IP 추가 후 commit', by['127.0.0.20'].sent)
ok(res['127.0.0.11']['status'] != '이미 설정됨', '--force 면 이미 응답하는 장비도 건너뛰지 않음', res['127.0.0.11'])
os.environ['COLLECTOR_IPS'] = '165.246.12.104,165.246.1.50'

print('== 입력 점검')
os.environ['IPAM_SNMP_AUTH'] = 'short'
try:
    B.main(['devices.xlsx']); ok(False, '짧은 비밀번호 거부')
except SystemExit as e:
    ok('8자 이상' in str(e), '짧은/특수문자 비밀번호는 시작 전에 거부')
print('== 실제 현장 조건: SNMP 계정 환경변수 없이 --apply --acl-only (가짜 확인 없이 실제 v3_ok 사용)')
class _C: user, auth, priv, port = 'ipam-ro', '', '', 1161
_r = real_v3_ok('127.0.0.9', _C())
ok(_r[0] is None and '없음' in _r[1], '빈 비밀번호 → SNMPv3 확인 생략, ZeroDivisionError 없음', _r)
_C.auth = _C.priv = 'short'
_r = real_v3_ok('127.0.0.9', _C())
ok(_r[0] is None and '8자' in _r[1], '8자 미만 비밀번호 → 안내만(예외 없음)', _r)
for k in ('IPAM_SNMP_USER', 'IPAM_SNMP_AUTH', 'IPAM_SNMP_PRIV'):
    os.environ.pop(k, None)
B.v3_ok = real_v3_ok
os.environ['COLLECTOR_IPS'] = '10.9.9.9'
res = B.main(['devices.xlsx', '--apply', '--acl-only', '--force', '--snmp-port', '1161'], connect=FakeConn)
by = {r['ip']: r for r in res}
ok(not any('ZeroDivision' in r['detail'] for r in res), 'ZeroDivisionError 없음', [(r['ip'], r['detail'][:60]) for r in res])
ok(all(by[h]['status'] == 'ACL 적용(확인 생략)' for h in ('127.0.0.17', '127.0.0.20', '127.0.0.21', '127.0.0.22', '127.0.0.23')),
   'Cisco·Juniper·HP·Aruba·Comware ACL 적용 → 상태 = ACL 적용(확인 생략)', [(h, by[h]['status']) for h in by])

print('== 현장 오류 대응 (2026-10-07): 장비의 실제 계정·ACL·버전을 읽고 명령을 정함')
os.environ.update(IPAM_SNMP_USER='ipam-ro', IPAM_SNMP_AUTH='Auth#Pass2026', IPAM_SNMP_PRIV='Priv#Pass2026', COLLECTOR_IPS='10.9.9.9')
B.time.sleep = lambda *_: None
V3 = {}
B.v3_ok = lambda host, c: V3.get(host, (None, 'No SNMP response received before timeout'))
HOSTCFG.update({
    '127.0.0.40': {'display version': 'H3C Comware Platform Software\nComware Software, Version 5.20, Release 2222P02',
                   'display current-configuration | include snmp':
                       ' snmp-agent group v3 OLD-RO privacy read-view iso-view\n snmp-agent usm-user v3 ipam-ro OLD-RO acl 2000',
                   'display acl 2000': 'Basic ACL  2000, 2 rules\nACL\'s step is 5\n rule 0 permit source 165.246.1.10 0\n rule 5 deny (12 times matched)'},
    '127.0.0.41': {'show snmp user ipam-ro': 'User name: ipam-ro\nEngine ID: 800000090300\nstorage-type: nonvolatile\t active\taccess-list: SNMP-USR\nAuthentication Protocol: SHA\nPrivacy Protocol: AES128\nGroup-name: NMS',
                   'show ip access-lists SNMP-USR': 'Standard IP access list SNMP-USR\n    10 permit 165.246.1.10',
                   'show running-config | include snmp-server group': 'snmp-server group NMS v3 priv read ALL access 10',
                   'show ip access-lists 10': 'Standard IP access list 10\n    10 permit 165.246.1.10\n    20 deny   any log'},
    '127.0.0.42': {'show configuration snmp | display set': 'set snmp community public authorization read-only',
                   'show configuration interfaces lo0 | display set': 'set interfaces lo0 unit 0 family inet filter input PROTECT-RE',
                   'show configuration firewall | display set':
                       'set firewall family inet filter PROTECT-RE term ssh from source-prefix-list MGMT\n'
                       'set firewall family inet filter PROTECT-RE term ssh from protocol tcp\n'
                       'set firewall family inet filter PROTECT-RE term ssh from port ssh\n'
                       'set firewall family inet filter PROTECT-RE term ssh then accept\n'
                       'set firewall family inet filter PROTECT-RE term snmp from source-prefix-list SNMP-MGR\n'
                       'set firewall family inet filter PROTECT-RE term snmp from protocol udp\n'
                       'set firewall family inet filter PROTECT-RE term snmp from port snmp\n'
                       'set firewall family inet filter PROTECT-RE term snmp then accept'},
    '127.0.0.43': {'show snmp user ipam-ro': ''},
})
inv2 = pd.DataFrame([{'ip': '127.0.0.40', 'name': 'cw5', 'vendor': 'hp_comware'},
                     {'ip': '127.0.0.41', 'name': 'xe', 'vendor': 'cisco_xe'},
                     {'ip': '127.0.0.42', 'name': 'jx', 'vendor': 'juniper_junos'},
                     {'ip': '127.0.0.44', 'name': 'no-ssh', 'vendor': 'cisco_ios'}])
inv2.to_excel('dev2.xlsx', index=False)

class TcpFail(Exception):
    pass
TELNET_USED = []
class FakeConn2(FakeConn):
    def __init__(self, **kw):
        if kw['host'] == '127.0.0.44' and not kw['device_type'].endswith('_telnet'):
            raise TcpFail('TCP connection to device failed.')
        if kw['device_type'].endswith('_telnet'):
            TELNET_USED.append(kw['device_type'])
        super().__init__(**kw)

_ps = B.port_state
B.port_state = lambda h, p, timeout=3: ('응답 없음' if p == 22 else '열림') if h == '127.0.0.44' else _ps(h, p, timeout)
SESSIONS.clear()
V3['127.0.0.41'] = (None, 'Unknown USM user name')
res = {r['ip']: r for r in B.main(['dev2.xlsx', '--apply', '--acl-only', '--force', '--snmp-port', '1161'], connect=FakeConn2)}
by = {s.kw['host']: s for s in SESSIONS}
cw = by['127.0.0.40'].sent
ok('acl number 2000' in cw and not any(x.startswith('acl basic') for x in cw), "Comware 5: 'acl basic' 대신 'acl number' (system-view 뒤 명령 거부 원인)", cw)
ok(any(re.fullmatch(r'rule [1-4] permit source 10\.9\.9\.9 0', x) for x in cw),
   "Comware: 사용 중인 ACL 2000 의 'rule 5 deny' 앞 번호로 추가", cw)
ok(not any('usm-user' in x for x in cw), 'Comware: 계정 있음 → 계정 명령 안 넣음', cw)
xe = by['127.0.0.41'].sent
ok('ip access-list standard 10' in xe and any(re.fullmatch(r' 1[1-9] permit 10\.9\.9\.9', x) for x in xe),
   "Cisco XE: 그룹이 실제로 쓰는 ACL 10 의 'deny any log' 앞 순번에 추가", xe)
ok(not any('IPAM-SNMP' in x for x in xe), 'Cisco XE: 쓰지 않는 IPAM-SNMP ACL 은 만들지 않음', xe)
ok('ip access-list standard SNMP-USR' in xe and ' permit 10.9.9.9' in xe, "Cisco XE: 계정에 직접 걸린 ACL(show snmp user 의 access-list)에도 추가", xe)
d41 = res['127.0.0.41']['detail']
ok(res['127.0.0.41']['status'] == '적용(서버에서 확인 필요)' and '이 PC' in d41 and '--verify-only' in d41,
   'Unknown USM user: 확인 요청을 보낸 PC 가 수집 서버가 아니면 그 사실과 서버에서 확인하는 방법 안내', d41)
jx = by['127.0.0.42'].sent
ok('set policy-options prefix-list SNMP-MGR 10.9.9.9/32' in jx, 'Juniper: lo0 필터에서 SNMP 허용 prefix-list(SNMP-MGR)를 찾아 추가', jx)
ok(any('usm local-engine user ipam-ro' in x for x in jx) and '<commit>' in jx and '계정도 추가' in res['127.0.0.42']['detail'],
   "Juniper: 계정이 없으면(Unknown USM user·'넣을 명령 없음' 원인) 계정도 추가 후 commit", res['127.0.0.42'])
ok(res['127.0.0.44']['status'] == '접속 불가' and '22번 응답 없음' in res['127.0.0.44']['detail']
   and 'vty' in res['127.0.0.44']['detail'] and '--telnet' in res['127.0.0.44']['detail'],
   'TCP 접속 실패: 포트 상태(22 응답 없음=필터, 23 열림)로 원인 구분 + --telnet 안내', res['127.0.0.44'])

SESSIONS.clear(); TELNET_USED.clear()
V3['127.0.0.44'] = ('sw-44', None)
res = {r['ip']: r for r in B.main(['dev2.xlsx', '--apply', '--acl-only', '--force', '--snmp-port', '1161', '--telnet',
                                     '--limit', '4'], connect=FakeConn2)}
ok(TELNET_USED == ['cisco_ios_telnet'] and res['127.0.0.44']['status'] == '적용 완료' and '텔넷' in res['127.0.0.44']['detail'],
   '--telnet: SSH 가 안 되는 장비만 텔넷으로 적용', (TELNET_USED, res['127.0.0.44']))

print('== 계정 없는 장비 + 계정 환경변수 없음')
for k in ('IPAM_SNMP_AUTH', 'IPAM_SNMP_PRIV'):
    os.environ.pop(k)
pd.DataFrame([{'ip': '127.0.0.43', 'name': 'xe2', 'vendor': 'cisco_xe'}]).to_excel('dev3.xlsx', index=False)
res = B.main(['dev3.xlsx', '--apply', '--acl-only', '--force', '--snmp-port', '1161'], connect=FakeConn2)
ok(res[0]['status'] == '조치 필요' and '--acl-only 없이' in res[0]['detail'], '계정이 없는 장비에 ACL 만 넣지 않고 조치 안내', res[0])

print('== 벤더 판별 보강')
ok(B.guess_from_version('Cisco IOS XE Software, Version 17.09.04a') == 'cisco_xe', 'show version → IOS-XE')
ok(B.guess_from_version('JUNOS Base OS boot [21.4R3]') == 'juniper_junos', 'show version → Junos')
ok(B.guess_from_version('HPE Comware Software, Version 7.1.070') == 'hp_comware', 'display version → Comware')
ok(B.guess_from_version('Image stamp: /ws/swbuildm/... ArubaOS-Switch') == 'hp_procurve', 'show version → ProCurve/ArubaOS-Switch')
class VerConn(FakeConn):
    probe = True
    def send_command_timing(self, cmd, **kw):
        self.sent.append(cmd)
        return '% Unrecognized command found at \'^\' position.' if cmd == 'show version' else \
            'H3C Comware Software, Version 5.20, Release 1808P21'
class _C2: ssh_user, ssh_pass, secret, telnet = 'u', 'p', '', False
ok(B._probe_version('127.0.0.50', _C2(), VerConn) == 'hp_comware', "SSHDetect 실패 시 'display version' 으로 Comware 판별")
prev = pd.DataFrame([{'ip': '127.0.0.40', 'vendor': 'hp_comware', 'status': '오류'}]); prev.to_excel('prev.xlsx', index=False)
pd.DataFrame([{'ip': '127.0.0.40', 'name': 'cw5'}]).to_excel('dev4.xlsx', index=False)
class _A: from_netbox = False; inventory = 'dev4.xlsx'; only_failed = 'prev.xlsx'; limit = 0
ok(B.load_inventory(_A())[0]['vendor'] == 'hp_comware', '--only-failed: 지난 결과의 장비 종류를 다시 판별하지 않고 사용')

print('== 현장 오류 대응 2: 설정 모드 진입 실패 · Comware acl 문법')
os.environ.update(IPAM_SNMP_AUTH='Auth#Pass2026', IPAM_SNMP_PRIV='Priv#Pass2026', COLLECTOR_IPS='10.9.9.9')
HOSTCFG['127.0.0.48'] = {'display version': 'Copyright (c) 2004-2015 Hewlett-Packard Development Company\nHP 1920-48G Switch',
                         'display current-configuration | include snmp': ' snmp-agent usm-user v3 ipam-ro IPAM-RO acl 2999'}
pd.DataFrame([{'ip': '127.0.0.46', 'name': 'xe-noen', 'vendor': 'cisco_xe'},
              {'ip': '127.0.0.47', 'name': 'xe-lock', 'vendor': 'cisco_xe'},
              {'ip': '127.0.0.48', 'name': 'cw-old', 'vendor': 'hp_comware'}]).to_excel('dev5.xlsx', index=False)
SESSIONS.clear()
V3['127.0.0.48'] = ('sw-48', None)
res = {r['ip']: r for r in B.main(['dev5.xlsx', '--apply', '--acl-only', '--force', '--snmp-port', '1161'], connect=FakeConn2)}
by = {s.kw['host']: s for s in SESSIONS}
ok(res['127.0.0.46']['status'] == '조치 필요' and 'NET_SECRET' in res['127.0.0.46']['detail'],
   "Cisco '>' 모드에서 enable 실패 → 'Failed to enter configuration mode' 대신 NET_SECRET·privilege 15 안내", res['127.0.0.46'])
ok(by['127.0.0.46'].kw.get('secret') == os.environ['NET_PASS'], 'NET_SECRET 이 없으면 로그인 비밀번호로 enable 시도', by['127.0.0.46'].kw)
ok(res['127.0.0.47']['status'] == '조치 필요' and 'configuration lock' in res['127.0.0.47']['detail'],
   '설정 모드 진입 실패 → 원인(enable·설정 잠금·AAA 권한) 안내', res['127.0.0.47'])
cw = by['127.0.0.48'].sent
ok('acl basic 2999' in cw and 'acl number 2999' in cw and res['127.0.0.48']['status'] == '적용 완료'
   and 'acl number' in res['127.0.0.48']['detail'], "Comware: 'acl basic' 거부되면 'acl number' 로 자동 재적용", (cw, res['127.0.0.48']))
ok('<save>' in cw, 'Comware: 재적용 성공 후 저장', cw)

print('== 현장 오류 대응 3: Comware 계정 문법·거부 위치·목록 빈 줄·서버 확인')
for h in ('127.0.0.49', '127.0.0.51'):
    HOSTCFG[h] = {'display version': 'HPE Comware Software, Version 7.1.045, Release 3208',
                  'display current-configuration | include snmp': ''}
pd.DataFrame([{'ip': '127.0.0.49', 'name': 'V170-2', 'vendor': 'hp_comware'},
              {'ip': '127.0.0.51', 'name': 'cw-noaes', 'vendor': 'hp_comware'},
              {'ip': '', 'name': '빈줄', 'vendor': ''},
              {'ip': '165.246.1.300', 'name': '오타', 'vendor': 'cisco_ios'}]).to_excel('dev6.xlsx', index=False)
SESSIONS.clear()
V3['127.0.0.49'] = ('V170-2', None)
res = B.main(['dev6.xlsx', '--apply', '--acl-only', '--force', '--snmp-port', '1161'], connect=FakeConn2)
byip = {r['ip']: r for r in res}
by = {s.kw['host']: s for s in SESSIONS}
s49 = by['127.0.0.49'].sent
ok(byip['127.0.0.49']['status'] == '적용 완료' and any('usm-user v3 ipam-ro IPAM-RO authentication-mode sha' in x for x in s49)
   and "'simple'" in byip['127.0.0.49']['detail'], "Comware 계정 명령 거부 → 'simple' 키워드를 바꿔 계정 줄만 다시 적용", (s49, byip['127.0.0.49']))
d51 = byip['127.0.0.51']['detail']
ok(byip['127.0.0.51']['status'] == '오류' and "'aes128' 부분" in d51 and 'AES' in d51 and 'Priv#Pass2026' not in d51,
   "거부 위치('^')의 단어를 알려 줌 — AES 미지원 장비 안내, 비밀번호는 가림", d51)
ok('' not in byip and len(res) == 3, '목록의 빈 줄은 제외 (Either ip or host must be set 방지)', list(byip))
ok(byip['165.246.1.300']['status'] == '목록 오류', '잘못된 IP 는 접속하지 않고 목록 오류로 표시', byip['165.246.1.300'])

print('== --verify-only (수집 서버에서 SNMPv3 응답만 확인)')
B.v3_ok = real_v3_ok
pd.DataFrame([{'ip': '127.0.0.11', 'name': 'core'}, {'ip': '127.0.0.99', 'name': 'none'}]).to_excel('dev7.xlsx', index=False)
SESSIONS.clear()
os.environ.pop('NET_USER'); os.environ.pop('NET_PASS')
res = {r['ip']: r for r in B.main(['dev7.xlsx', '--verify-only', '--snmp-port', '1161'], connect=FakeConn2)}
ok(res['127.0.0.11']['status'] == '응답 확인' and res['127.0.0.99']['status'] == '응답 없음' and not SESSIONS,
   '--verify-only: SSH 접속 없이 SNMPv3 응답 여부만 (SSH 계정 불필요)', res)

print('== SSH 접속 불가 원인 구분 (EX3300 등)')
B.port_state = lambda h, p, timeout=3: {22: '응답 없음', 23: '응답 없음'}[p]
ok('lo0' in B._tcp_hint('1.1.1.1', 'juniper_junos') and 'prefix-list' in B._tcp_hint('1.1.1.1', 'juniper_junos'),
   'Juniper + 22번 응답 없음 → lo0 필터 SSH 허용 목록 안내')
B.port_state = lambda h, p, timeout=3: {22: '거부', 23: '열림'}[p]
ok("system services ssh" in B._tcp_hint('1.1.1.1', 'juniper_junos') and '--telnet' in B._tcp_hint('1.1.1.1', 'juniper_junos'),
   '22번 거부 → SSH 서비스 꺼짐, 텔넷 열림이면 --telnet')
B.port_state = _ps
import socket as _so
_srv = _so.socket(); _srv.bind(('127.0.0.1', 0)); _srv.listen()
ok(B.port_state('127.0.0.1', _srv.getsockname()[1]) == '열림' and B.port_state('127.0.0.1', 1) == '거부', '실제 포트 상태 판별(열림·거부)')

print('== 수집 서버 ACL: Juniper term 직접 주소·여러 prefix-list, 서버 출발 IP')
B.v3_ok = lambda host, c: V3.get(host, (None, 'No SNMP response received before timeout'))
os.environ.update(NET_USER='netadmin', NET_PASS='SshPass!9', COLLECTOR_IPS='10.9.9.9')
HOSTCFG['127.0.0.52'] = {'show configuration snmp | display set': 'set snmp v3 usm local-engine user ipam-ro authentication-sha',
    'show configuration interfaces lo0 | display set': 'set interfaces lo0 unit 0 family inet filter input-list RE-1\nset interfaces lo0 unit 0 family inet filter input-list RE-2',
    'show configuration firewall | display set':
        'set firewall family inet filter RE-1 term nms from source-address 165.246.1.10/32\n'
        'set firewall family inet filter RE-1 term nms from protocol udp\n'
        'set firewall family inet filter RE-1 term nms from destination-port 161\n'
        'set firewall family inet filter RE-1 term nms then accept\n'
        'set firewall filter RE-2 term snmp from source-prefix-list NMS-A\n'
        'set firewall filter RE-2 term snmp from source-prefix-list NMS-B\n'
        'set firewall filter RE-2 term snmp from port snmp\n'
        'set firewall filter RE-2 term snmp then accept'}
pd.DataFrame([{'ip': '127.0.0.52', 'name': 'ex3300', 'vendor': 'juniper_junos'}]).to_excel('dev8.xlsx', index=False)
SESSIONS.clear()
res = B.main(['dev8.xlsx', '--apply', '--acl-only', '--force', '--snmp-port', '1161'], connect=FakeConn2)
jx = SESSIONS[0].sent
ok('set firewall family inet filter RE-1 term nms from source-address 10.9.9.9/32' in jx,
   'Juniper: SNMP term 에 주소를 직접 적은 필터 → 그 term 에 수집 서버 주소 추가', jx)
ok('set policy-options prefix-list NMS-A 10.9.9.9/32' in jx and 'set policy-options prefix-list NMS-B 10.9.9.9/32' in jx
   and not any('usm local-engine' in x for x in jx), 'Juniper: SNMP 허용 prefix-list 가 여럿이면 모두에 추가(계정 있으면 계정은 그대로)', jx)
B.v3_ok = real_v3_ok
os.environ.update(IPAM_SNMP_USER='ipam-ro', IPAM_SNMP_AUTH='Auth#Pass2026', IPAM_SNMP_PRIV='Priv#Pass2026')
if os.path.exists('collector_ips.txt'):
    os.remove('collector_ips.txt')
res = B.main(['dev7.xlsx', '--verify-only', '--snmp-port', '1161'])
ok(open('collector_ips.txt').read().strip() == '127.0.0.1' and res[0].get('src_ip') == '127.0.0.1',
   '--verify-only: 이 서버가 장비로 보내는 출발 IP 를 collector_ips.txt 로 남김', open('collector_ips.txt').read())
print(f"\n결과: PASS {R['p']} / FAIL {R['f']}")
