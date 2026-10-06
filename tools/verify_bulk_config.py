"""snmp_bulk_config.py 검증 — 실제 장비 대신 가짜 SSH 연결 + SNMP 시뮬레이터 사용 (개발용)"""
import os, sys, tempfile
import pandas as pd
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import snmp_bulk_config as B

R = {'p': 0, 'f': 0}
def ok(c, n, x=''):
    R['p' if c else 'f'] += 1; print(('  PASS ' if c else '  FAIL ') + n + ('' if c else f'  [{x}]'))

os.environ.update(IPAM_SNMP_USER='ipam-ro', IPAM_SNMP_AUTH='Auth#Pass2026', IPAM_SNMP_PRIV='Priv#Pass2026',
                  COLLECTOR_IPS='165.246.12.104,165.246.1.50', NET_USER='netadmin', NET_PASS='SshPass!9', SNMP_V2C_COMMUNITY='public')
CONFIGURED, SESSIONS = set(), []
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
    def enable(self): self.sent.append('<enable>')
    def send_command(self, cmd):
        self.sent.append('<show> ' + cmd)
        if cmd == 'show ip authorized-managers':
            return ' IPV4 Authorized Managers\n Address : 165.246.1.10\n Mask    : 255.255.255.255\n Access  : Manager' if self.kw['host'] == '127.0.0.21' else ' IPV4 Authorized Managers\n'
        if 'control-plane' in cmd:
            return 'apply access-list ip MGMT control-plane vrf default' if self.kw['host'] == '127.0.0.22' else ''
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
        self.sent.extend(cmds)
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
print(f"\n결과: PASS {R['p']} / FAIL {R['f']}")
