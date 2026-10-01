"""벤더별 SNMP 시뮬레이터(snmpsim) 데이터 생성 + 기동 — verify_*.py 용 (개발·테스트 전용, 포트 1161)"""
import os, subprocess, sys
D = '/tmp/simdata'
SYS = '1.3.6.1.2.1.1'
def mac(h): return h.replace(':', '')
def ipidx(ip): return ip
def rec(lines): return '\n'.join(sorted(lines, key=lambda l: tuple(int(x) for x in l.split('|')[0].split('.')))) + '\n'
def sysrec(descr, oid, name):
    return [f'{SYS}.1.0|4|{descr}', f'{SYS}.2.0|6|{oid}', f'{SYS}.5.0|4|{name}']
def ifn(m): return [f'1.3.6.1.2.1.31.1.1.1.1.{i}|4|{n}' for i, n in m.items()]
def macidx(h): return '.'.join(str(int(h[i:i+2], 16)) for i in range(0, 12, 2))
def dfdb(entries, bp):  # entries [(mac, bridgeport, status)]
    out = [f'1.3.6.1.2.1.17.1.4.1.2.{b}|2|{i}' for b, i in bp.items()]
    for m, p, st in entries:
        out += [f'1.3.6.1.2.1.17.4.3.1.1.{macidx(mac(m))}|4x|{mac(m)}', f'1.3.6.1.2.1.17.4.3.1.2.{macidx(mac(m))}|2|{p}',
                f'1.3.6.1.2.1.17.4.3.1.3.{macidx(mac(m))}|2|{st}']
    return out
def qfdb(entries, bp, fdbmap):  # entries [(fdb, mac, port, status)]
    out = [f'1.3.6.1.2.1.17.1.4.1.2.{b}|2|{i}' for b, i in bp.items()]
    out += [f'1.3.6.1.2.1.17.7.1.4.2.1.3.0.{v}|66|{f}' for v, f in fdbmap.items()]
    for f, m, p, st in entries:
        out += [f'1.3.6.1.2.1.17.7.1.2.2.1.2.{f}.{macidx(mac(m))}|2|{p}', f'1.3.6.1.2.1.17.7.1.2.2.1.3.{f}.{macidx(mac(m))}|2|{st}']
    return out
def arp(entries):  # [(ifindex, ip, mac, type)]
    out = []
    for i, ip, m, t in entries:
        out += [f'1.3.6.1.2.1.4.22.1.2.{i}.{ip}|4x|{mac(m)}', f'1.3.6.1.2.1.4.22.1.4.{i}.{ip}|2|{t}']
    return out
def arp2(entries):
    out = []
    for i, ip, m, t in entries:
        if ':' in ip:
            idx = f'{i}.2.16.' + '.'.join(['254', '128'] + ['0'] * 13 + ['1'])
        else:
            idx = f'{i}.1.4.{ip}'
        out += [f'1.3.6.1.2.1.4.35.1.4.{idx}|4x|{mac(m)}', f'1.3.6.1.2.1.4.35.1.6.{idx}|2|{t}']
    return out

def inv(ifs, addrs=(), vlans=None, cvlans=None, model='', serial=''):
    out = []
    for i, (t, adm, opr, alias, spd) in ifs.items():
        out += [f'1.3.6.1.2.1.2.2.1.3.{i}|2|{t}', f'1.3.6.1.2.1.2.2.1.7.{i}|2|{adm}', f'1.3.6.1.2.1.2.2.1.8.{i}|2|{opr}',
                f'1.3.6.1.2.1.31.1.1.1.18.{i}|4|{alias}', f'1.3.6.1.2.1.31.1.1.1.15.{i}|66|{spd}']
    for ip, mask, i in addrs:
        out += [f'1.3.6.1.2.1.4.20.1.2.{ip}|2|{i}', f'1.3.6.1.2.1.4.20.1.3.{ip}|64|{mask}']
    for v, n in (vlans or {}).items():
        out.append(f'1.3.6.1.2.1.17.7.1.4.3.1.1.{v}|4|{n}')
    for v, n in (cvlans or {}).items():
        out.append(f'1.3.6.1.4.1.9.9.46.1.3.1.1.4.1.{v}|4|{n}')
    if model:
        out += ['1.3.6.1.2.1.47.1.1.1.1.5.1|2|3', f'1.3.6.1.2.1.47.1.1.1.1.13.1|4|{model}', f'1.3.6.1.2.1.47.1.1.1.1.11.1|4|{serial}']
    return out


DEV = {}
# 1) Juniper 코어 (ARP, ipNetToMedia)
DEV['11'] = {'': sysrec('Juniper Networks, Inc. ex9208 , kernel JUNOS 21.4R3', '1.3.6.1.4.1.2636.1.1.1.2.31', 'core-ex9208')
             + ifn({501: 'irb.49', 502: 'irb.50', 10: 'me0'})
             + inv({501: (53, 1, 1, 'GW office', 0), 502: (53, 1, 1, 'GW lab', 0), 10: (6, 1, 1, 'mgmt', 1000)},
                   [('127.0.0.11', '255.0.0.0', 10), ('165.246.49.1', '255.255.255.0', 501), ('165.246.50.1', '255.255.255.0', 502)],
                   vlans={49: 'office-49', 50: 'lab-50'}, model='EX9208', serial='JN12345') + arp([
    (501, '165.246.49.10', '00:11:22:00:00:10', 3), (501, '165.246.49.11', '00:11:22:00:00:11', 3),
    (501, '165.246.49.12', '00:11:22:00:00:99', 3), (501, '165.246.49.99', '00:11:22:00:00:ee', 3),
    (501, '165.246.49.13', '00:11:22:00:00:13', 3), (501, '165.246.49.77', '00:11:22:00:00:77', 2),
    (501, '165.246.49.78', '01:00:5e:00:00:01', 3), (502, '165.246.50.20', '00:11:22:00:05:20', 3)])}
# 2) Cisco 액세스 (VLAN context 별 BRIDGE-MIB)
cis = sysrec('Cisco IOS Software, C2960X Software (C2960X-UNIVERSALK9-M), Version 15.2(7)E8', '1.3.6.1.4.1.9.1.1208', 'sw-cisco') \
    + ifn({10105: 'Gi1/0/5', 10124: 'Gi1/0/24', 1: 'Vl1'}) + [f'1.3.6.1.4.1.9.9.46.1.3.1.1.2.1.{v}|2|1' for v in (1, 49, 50, 1002)] \
    + inv({10105: (6, 1, 1, '9-101 교수연구실', 1000), 10124: (6, 1, 1, 'uplink-core', 1000), 1: (53, 1, 1, '', 0)},
          [('127.0.0.12', '255.0.0.0', 1)], cvlans={1: 'default', 49: 'OFFICE', 50: 'LAB', 1002: 'fddi-default'},
          model='WS-C2960X-48FPD-L', serial='FOC1234X')
DEV['12'] = {'': cis,
             'vlan-1': dfdb([], {5: 10105, 24: 10124}),
             'vlan-49': dfdb([('00:11:22:00:00:30', 5, 3)] + [(f'00:11:22:00:0a:0{k}', 24, 3) for k in range(5)]
                             + [('00:11:22:00:00:ff', 0, 4)], {5: 10105, 24: 10124}),
             'vlan-50': dfdb([('00:11:22:00:05:31', 24, 3)], {5: 10105, 24: 10124})}
# 3) Juniper EX 액세스 (Q-BRIDGE, fdbId≠VLAN)
DEV['13'] = {'': sysrec('Juniper Networks, Inc. ex2300-48p', '1.3.6.1.4.1.2636.1.1.1.2.132', 'sw-juniper')
             + ifn({517: 'ge-0/0/9.0', 518: 'ge-0/0/10.0', 509: 'ge-0/0/9', 510: 'ge-0/0/10', 11: 'me0'})
             + inv({509: (6, 1, 1, '9-102', 1000), 510: (6, 1, 2, '', 1000), 517: (53, 1, 1, '', 0), 518: (53, 1, 2, '', 0),
                    11: (6, 1, 1, '', 1000)}, [('127.0.0.13', '255.0.0.0', 11)], vlans={49: 'office-49'}, model='EX2300-48P')
             + qfdb([
    (5, '00:11:22:00:00:10', 9, 3), (5, '00:11:22:00:00:11', 10, 3), (5, '00:11:22:00:00:fe', 0, 4)], {9: 517, 10: 518}, {49: 5})}
# 4) Aruba CX (Q-BRIDGE fdbId=VLAN, 브리지포트=ifIndex, ARP는 ipNetToPhysical 만)
DEV['14'] = {'': sysrec('Aruba JL658A 6300M Switch, FL.10.10.1020', '1.3.6.1.4.1.47196.4.1.1.1.302', 'sw-arubacx')
             + ifn({1009: '1/1/9', 1050: 'vlan50'}) + qfdb([(50, '00:11:22:00:05:20', 1009, 3)], {}, {})
             + arp2([(1050, '165.246.50.21', '00:11:22:00:05:21', 3), (1050, '165.246.50.1', '00:11:22:00:05:01', 5),
                     (1050, 'fe80::1', '00:11:22:00:05:22', 3)])}
# 5) HP ProCurve (BRIDGE-MIB 기본 context)
DEV['15'] = {'': sysrec('HP J9772A 2530-48G-PoEP Switch, revision YA.16.10', '1.3.6.1.4.1.11.2.3.7.11.153', 'sw-procurve')
             + ifn({7: '7'}) + dfdb([('00:11:22:00:00:13', 7, 3)], {7: 7})}
# 8) 두 번째 계정을 쓰는 Comware
DEV['18'] = {'': sysrec('HPE Comware Platform Software, 5130-EI', '1.3.6.1.4.1.25506.11.1.1', 'sw-comware')
             + ifn({3: 'GigabitEthernet1/0/3'}) + qfdb([(49, '00:11:22:00:00:33', 3, 3)], {3: 3}, {})}
# 7) 비밀번호 틀린 장비
DEV['17'] = {'': sysrec('Cisco', '1.3.6.1.4.1.9.1.1', 'sw-wrongpw')}
DEV['17']['public'] = DEV['17']['']  # v2c community 'public' (벤더 판별 테스트)

# 대량: 액세스 스위치 40대(각 48포트 × MAC) + 코어 ARP 4,000건
big = sysrec('Juniper EX4300', '1.3.6.1.4.1.2636.1.1.1.2.63', 'big') + ifn({500 + p: f'ge-0/0/{p}.0' for p in range(48)})
ent = []
for p in range(48):
    for k in range(3 if p < 47 else 60):  # 47번은 업링크(60 MAC)
        ent.append((7, f'00:22:{p:02x}:00:{k // 256:02x}:{k % 256:02x}', p + 1, 3))
big += qfdb(ent, {p + 1: 500 + p for p in range(48)}, {49: 7})
DEV['big'] = {'': big}
bigcore = sysrec('Juniper MX', '1.3.6.1.4.1.2636.1.1.1.2.25', 'bigcore') + ifn({700: 'irb.60'})
bigcore += arp([(700, f'165.246.{60 + i // 250}.{i % 250 + 2}', f'00:33:00:00:{i // 256:02x}:{i % 256:02x}', 3) for i in range(4000)])
DEV['bigcore'] = {'': bigcore}

USERS = {'main': ('ipam-ro', 'Auth#Pass2026', 'Priv#Pass2026'), 'second': ('ipam2', 'Second#Auth1', 'Second#Priv1'),
         'wrong': ('ipam-ro', 'Other#Auth999', 'Priv#Pass2026')}
EP = {'11': 'main', '12': 'main', '13': 'main', '14': 'main', '15': 'main', '18': 'second', '17': 'wrong'}

def write():
    for k, files in DEV.items():
        d = f'{D}/{k}'; os.makedirs(d, exist_ok=True)
        for ctx, lines in files.items():
            open(f'{d}/{ctx}.snmprec', 'w').write(rec(lines))
    subprocess.run(['chmod', '-R', 'a+rX', D])

def start():
    os.makedirs('/tmp/simpids', exist_ok=True); os.makedirs('/tmp/simcache', exist_ok=True)
    subprocess.run(['chmod', '777', '/tmp/simpids', '/tmp/simcache'])
    def run(tag, data, eps, user):
        u, a, p = USERS[user]
        args = ['/home/claude/simvenv/bin/snmpsim-command-responder', '--process-user=nobody', '--process-group=nogroup',
                f'--data-dir={D}/{data}', f'--cache-dir=/tmp/simcache/{tag}', f'--pid-file=/tmp/simpids/{tag}.pid',
                f'--v3-user={u}', f'--v3-auth-key={a}', '--v3-auth-proto=SHA', f'--v3-priv-key={p}', '--v3-priv-proto=AES']
        args += [f'--agent-udpv4-endpoint={e}:1161' for e in eps]
        os.makedirs(f'/tmp/simcache/{tag}', exist_ok=True); subprocess.run(['chmod', '777', f'/tmp/simcache/{tag}'])
        subprocess.Popen(args, stdout=open(f'/tmp/sim_{tag}.log', 'w'), stderr=subprocess.STDOUT, start_new_session=True)
    for k, user in EP.items():
        run(k, k, [f'127.0.0.{k}'], user)
    run('big', 'big', [f'127.0.1.{i}' for i in range(1, 41)], 'main')
    run('bigcore', 'bigcore', ['127.0.2.1'], 'main')

if __name__ == '__main__':
    write()
    if 'start' in sys.argv:
        start()
