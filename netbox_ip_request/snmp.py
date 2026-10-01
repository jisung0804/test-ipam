"""SNMPv3 ARP / MAC 테이블 수집 (Cisco · Juniper · HP/Aruba 혼재 환경용)

벤더 전용 MIB 대신 **표준 MIB**만 읽는다 → 장비 종류가 섞여 있어도 같은 코드로 수집.
  ARP  : IP-MIB ipNetToMediaTable (없으면 ipNetToPhysicalTable)
  MAC  : Q-BRIDGE-MIB dot1qTpFdbTable (없으면 BRIDGE-MIB dot1dTpFdbTable)
         Cisco IOS/IOS-XE 는 VLAN마다 SNMPv3 context 'vlan-<ID>' 로 BRIDGE-MIB 를 따로 읽어야 한다.
  포트 : BRIDGE-MIB dot1dBasePortIfIndex(브리지 포트→ifIndex) + IF-MIB ifName

수집 대상은 NetBox 장치(Device)에서 고른다 (별도 장비 목록 파일 없음).
  - ARP 수집 : 태그 'ipam-arp' 가 붙은 장치 (코어/분배 L3 스위치, 라우터, 방화벽 등)
  - MAC 수집 : 태그 'ipam-mac' 가 붙었거나, 역할이 snmp_mac_roles(기본 access-switch)인 장치
  - 접속 주소 : 장치의 기본 IPv4(primary IP). 예외는 장치의 로컬 설정 컨텍스트 {"snmp_host": .., "snmp_port": ..}
SNMP 계정은 NetBox DB가 아니라 설정(환경변수)에만 둔다 → plugins.py 참고.
"""
import asyncio
import ipaddress
import time

from django.utils import timezone

from netbox.plugins import get_plugin_config

PLUGIN = 'netbox_ip_request'

OID = {
    'sysDescr': '1.3.6.1.2.1.1.1.0',
    'sysObjectID': '1.3.6.1.2.1.1.2.0',
    'sysName': '1.3.6.1.2.1.1.5.0',
    'ifName': '1.3.6.1.2.1.31.1.1.1.1',
    'ifDescr': '1.3.6.1.2.1.2.2.1.2',
    'arpPhys': '1.3.6.1.2.1.4.22.1.2',       # ipNetToMediaPhysAddress   [ifIndex.a.b.c.d]
    'arpType': '1.3.6.1.2.1.4.22.1.4',       # 2=invalid 3=dynamic 4=static
    'arp2Phys': '1.3.6.1.2.1.4.35.1.4',      # ipNetToPhysicalPhysAddress [ifIndex.type.len.addr]
    'arp2Type': '1.3.6.1.2.1.4.35.1.6',      # 2=invalid 5=local
    'basePortIf': '1.3.6.1.2.1.17.1.4.1.2',  # dot1dBasePortIfIndex
    'dFdbPort': '1.3.6.1.2.1.17.4.3.1.2',    # dot1dTpFdbPort   [mac 6]
    'dFdbStatus': '1.3.6.1.2.1.17.4.3.1.3',  # 3=learned 4=self 5=mgmt
    'qFdbPort': '1.3.6.1.2.1.17.7.1.2.2.1.2',    # dot1qTpFdbPort   [fdbId.mac 6]
    'qFdbStatus': '1.3.6.1.2.1.17.7.1.2.2.1.3',
    'qVlanFdbId': '1.3.6.1.2.1.17.7.1.4.2.1.3',  # dot1qVlanFdbId   [timeMark.vlan] = fdbId
    'ciscoVlan': '1.3.6.1.4.1.9.9.46.1.3.1.1.2',  # CISCO-VTP-MIB vtpVlanState [domain.vlan]
    # ---- 장비 정보(인벤토리)
    'ifType': '1.3.6.1.2.1.2.2.1.3',
    'ifAdmin': '1.3.6.1.2.1.2.2.1.7',        # 1=up 2=down
    'ifOper': '1.3.6.1.2.1.2.2.1.8',         # 1=up 2=down
    'ifAlias': '1.3.6.1.2.1.31.1.1.1.18',    # 포트 설명(description)
    'ifHighSpeed': '1.3.6.1.2.1.31.1.1.1.15',  # Mbps
    'ipAdIf': '1.3.6.1.2.1.4.20.1.2',        # ipAdEntIfIndex [ip] = ifIndex
    'ipAdMask': '1.3.6.1.2.1.4.20.1.3',      # ipAdEntNetMask [ip]
    'qVlanName': '1.3.6.1.2.1.17.7.1.4.3.1.1',   # dot1qVlanStaticName [vid]
    'ciscoVlanName': '1.3.6.1.4.1.9.9.46.1.3.1.1.4',  # vtpVlanName [domain.vid]
    'entModel': '1.3.6.1.2.1.47.1.1.1.1.13',  # entPhysicalModelName
    'entSerial': '1.3.6.1.2.1.47.1.1.1.1.11',  # entPhysicalSerialNum
    'entClass': '1.3.6.1.2.1.47.1.1.1.1.5',    # 3=chassis
}

VENDORS = {9: 'Cisco', 2636: 'Juniper', 11: 'HP/Aruba(ProCurve)', 25506: 'HPE Comware(H3C)',
           47196: 'Aruba CX', 14823: 'Aruba', 12356: 'Fortinet', 6486: 'Alcatel', 1916: 'Extreme'}


def cfg(key):
    return get_plugin_config(PLUGIN, key)


class SnmpError(Exception):
    pass


# --------------------------------------------------------------------- SNMP 저수준
def _auth(cred):
    from pysnmp.hlapi.v3arch import asyncio as h
    auth = {'md5': h.usmHMACMD5AuthProtocol, 'sha': h.usmHMACSHAAuthProtocol, 'sha1': h.usmHMACSHAAuthProtocol,
            'sha224': h.usmHMAC128SHA224AuthProtocol, 'sha256': h.usmHMAC192SHA256AuthProtocol,
            'sha384': h.usmHMAC256SHA384AuthProtocol, 'sha512': h.usmHMAC384SHA512AuthProtocol}
    priv = {'des': h.usmDESPrivProtocol, '3des': h.usm3DESEDEPrivProtocol, 'aes': h.usmAesCfb128Protocol,
            'aes128': h.usmAesCfb128Protocol, 'aes192': h.usmAesCfb192Protocol, 'aes256': h.usmAesCfb256Protocol}
    a = (cred.get('auth_proto') or 'sha').lower()
    p = (cred.get('priv_proto') or 'aes').lower()
    if a not in auth or p not in priv:
        raise SnmpError(f'지원하지 않는 방식: auth={a} priv={p}')
    return h.UsmUserData(cred['user'], cred.get('auth_key') or None, cred.get('priv_key') or None,
                         authProtocol=auth[a] if cred.get('auth_key') else h.usmNoAuthProtocol,
                         privProtocol=priv[p] if cred.get('priv_key') else h.usmNoPrivProtocol)


class Session:
    """장비 1대에 대한 SNMPv3 세션 (asyncio)."""

    def __init__(self, engine, host, port, cred, timeout, retries):
        self.engine, self.host, self.port, self.cred = engine, host, port, cred
        self.timeout, self.retries = timeout, retries
        self.auth = _auth(cred)
        self.target = None

    async def _target(self):
        from pysnmp.hlapi.v3arch import asyncio as h
        if self.target is None:
            self.target = await h.UdpTransportTarget.create((self.host, self.port), timeout=self.timeout,
                                                            retries=self.retries)
        return self.target

    def _ctx(self, context):
        from pysnmp.hlapi.v3arch import asyncio as h
        return h.ContextData(contextName=context.encode()) if context else h.ContextData()

    async def get(self, *oids, context=''):
        from pysnmp.hlapi.v3arch import asyncio as h
        err, status, _, vbs = await h.get_cmd(self.engine, self.auth, await self._target(), self._ctx(context),
                                              *[h.ObjectType(h.ObjectIdentity(o)) for o in oids], lookupMib=False)
        if err:
            raise SnmpError(_explain(str(err)))
        if status:
            raise SnmpError(f'장비 오류: {status.prettyPrint()}')
        return [None if v.__class__.__name__ in ('NoSuchObject', 'NoSuchInstance', 'EndOfMibView') else v
                for _, v in vbs]

    async def walk(self, oid, context=''):
        """oid 하위 전체를 GETBULK로 읽어 {인덱스(tuple): 값} 반환. 없는 테이블이면 빈 dict."""
        from pysnmp.hlapi.v3arch import asyncio as h
        base = tuple(int(x) for x in oid.split('.'))
        out = {}
        async for err, status, _, vbs in h.bulk_walk_cmd(
                self.engine, self.auth, await self._target(), self._ctx(context), 0, 40,
                h.ObjectType(h.ObjectIdentity(oid)), lexicographicMode=False, lookupMib=False):
            if err:
                raise SnmpError(_explain(str(err)))
            if status:
                break
            for name, val in vbs:
                t = tuple(name)
                if t[:len(base)] != base or val.__class__.__name__ == 'EndOfMibView':
                    return out
                out[t[len(base):]] = val
        return out


def _explain(msg):
    """pysnmp 오류 문구 → 원인 안내(한글)"""
    m = msg.lower()
    if 'timeout' in m or 'no snmp response' in m:
        return '응답 없음(타임아웃) — IP·방화벽/ACL·장비 SNMP 설정 확인'
    if 'unknown usm user' in m or 'unknown user' in m or 'unknownusername' in m:
        return '장비에 없는 SNMP 사용자 이름'
    if 'digest' in m:
        return '인증 비밀번호(auth) 또는 인증 방식(SHA/MD5) 불일치'
    if 'ciphertext' in m or 'decrypt' in m or 'ciphering' in m:
        return '암호화 비밀번호(priv) 또는 암호화 방식(AES/DES) 불일치'
    if 'security level' in m or 'seclevel' in m:
        return '보안 수준 불일치(장비는 authPriv 인데 설정은 다르거나 반대)'
    if 'context' in m:
        return 'VLAN context 접근 불가 — Cisco 는 "context vlan- match prefix" 설정 필요'
    if 'time window' in m or 'notintimewindow' in m:
        return '장비 시간 동기 오류(재시도하면 대부분 해결)'
    return msg


def _mac(val):
    b = val.asOctets() if hasattr(val, 'asOctets') else bytes(val)
    if len(b) != 6:
        return None
    return ':'.join(f'{x:02x}' for x in b)


def _clean_mac(m):
    """관리 대상이 아닌 MAC(없음/브로드캐스트/멀티캐스트) 제외."""
    if not m or m in ('00:00:00:00:00:00', 'ff:ff:ff:ff:ff:ff') or int(m[1], 16) & 1:
        return None
    return m


# --------------------------------------------------------------------- 테이블 해석
async def read_system(s):
    descr, oid, name = await s.get(OID['sysDescr'], OID['sysObjectID'], OID['sysName'])
    ent = None
    if oid is not None:
        t = tuple(oid)
        if t[:6] == (1, 3, 6, 1, 4, 1) and len(t) > 6:
            ent = t[6]
    return {'descr': text(descr)[:200], 'name': text(name), 'enterprise': ent,
            'vendor': VENDORS.get(ent, f'기타({ent})' if ent else '알 수 없음')}


async def read_ifnames(s):
    names = await s.walk(OID['ifName'])
    if not names:
        names = await s.walk(OID['ifDescr'])
    return {idx[0]: text(v) for idx, v in names.items()}


async def read_arp(s, ifnames):
    """[(ip, mac, 인터페이스명)]"""
    rows = []
    phys, types = await s.walk(OID['arpPhys']), await s.walk(OID['arpType'])
    for idx, v in phys.items():
        if len(idx) != 5 or int(types.get(idx, 3)) == 2:
            continue
        rows.append((str(ipaddress.IPv4Address(bytes(idx[1:]))), _clean_mac(_mac(v)), ifnames.get(idx[0], str(idx[0]))))
    if not phys:  # 구형 테이블이 없는 장비 → RFC4293 테이블
        phys, types = await s.walk(OID['arp2Phys']), await s.walk(OID['arp2Type'])
        for idx, v in phys.items():
            if len(idx) != 7 or idx[1] != 1 or idx[2] != 4 or int(types.get(idx, 3)) in (2, 5):
                continue
            rows.append((str(ipaddress.IPv4Address(bytes(idx[3:]))), _clean_mac(_mac(v)),
                         ifnames.get(idx[0], str(idx[0]))))
    return [r for r in rows if r[1]]


async def _bridge_ports(s, context=''):
    return {idx[0]: int(v) for idx, v in (await s.walk(OID['basePortIf'], context)).items()}


async def _fdb_dot1d(s, ifnames, vlan=None, context=''):
    ports, bp = await s.walk(OID['dFdbPort'], context), None
    if not ports:
        return []
    status = await s.walk(OID['dFdbStatus'], context)
    bp = await _bridge_ports(s, context)
    out = []
    for idx, port in ports.items():
        if len(idx) != 6 or int(status.get(idx, 3)) != 3 or int(port) == 0:
            continue
        ifi = bp.get(int(port))
        out.append((_clean_mac(':'.join(f'{x:02x}' for x in idx)), vlan, ifnames.get(ifi, str(port))))
    return out


async def _fdb_qbridge(s, ifnames):
    ports = await s.walk(OID['qFdbPort'])
    if not ports:
        return []
    status = await s.walk(OID['qFdbStatus'])
    fdb2vlan = {int(v): idx[-1] for idx, v in (await s.walk(OID['qVlanFdbId'])).items()}
    bp = await _bridge_ports(s)
    out = []
    for idx, port in ports.items():
        if len(idx) != 7 or int(status.get(idx, 3)) != 3 or int(port) == 0:
            continue
        ifi = bp.get(int(port), int(port))  # 일부 장비는 브리지 포트 = ifIndex
        out.append((_clean_mac(':'.join(f'{x:02x}' for x in idx[1:])), fdb2vlan.get(idx[0], idx[0]),
                    ifnames.get(ifi, str(port))))
    return out


async def read_mac(s, ifnames, enterprise):
    """[(mac, vlan, 포트명)], 사용한 방식"""
    if enterprise == 9:  # Cisco: VLAN 별 context
        vlans = sorted({idx[-1] for idx, v in (await s.walk(OID['ciscoVlan'])).items()
                        if int(v) == 1 and not 1002 <= idx[-1] <= 1005})
        if vlans:
            out = []
            for vid in vlans:
                out += await _fdb_dot1d(s, ifnames, vid, context=f'vlan-{vid}')
            return [r for r in out if r[0]], f'BRIDGE-MIB(VLAN context {len(vlans)}개)'
    rows = await _fdb_qbridge(s, ifnames)
    if rows:
        return [r for r in rows if r[0]], 'Q-BRIDGE-MIB'
    rows = await _fdb_dot1d(s, ifnames)
    return [r for r in rows if r[0]], 'BRIDGE-MIB'


def text(v):
    """장비가 보낸 문자열 디코딩: UTF-8 → (한글 장비 설정) CP949/EUC-KR → latin-1"""
    if v is None:
        return ''
    b = v.asOctets() if hasattr(v, 'asOctets') else str(v).encode('latin-1', 'replace')
    for enc in ('utf-8', 'cp949'):
        try:
            return b.decode(enc).strip('\x00 ')
        except UnicodeDecodeError:
            pass
    return b.decode('latin-1').strip('\x00 ')


async def read_inventory(s, ifnames):
    """인터페이스(상태·설명·속도·종류), IP 주소(SVI), VLAN 이름, 모델·시리얼"""
    def col(d):
        return {idx[0]: v for idx, v in d.items() if len(idx) == 1}
    typ, adm, opr = col(await s.walk(OID['ifType'])), col(await s.walk(OID['ifAdmin'])), col(await s.walk(OID['ifOper']))
    ali, spd = col(await s.walk(OID['ifAlias'])), col(await s.walk(OID['ifHighSpeed']))
    ifs = {}
    for i, name in ifnames.items():
        ifs[i] = {'name': name, 'type': int(typ.get(i, 1)), 'admin': int(adm.get(i, 1)) == 1,
                  'oper': int(opr.get(i, 2)) == 1, 'alias': text(ali.get(i)), 'speed': int(spd.get(i, 0) or 0)}
    addrs = []
    ifx, msk = await s.walk(OID['ipAdIf']), await s.walk(OID['ipAdMask'])
    for idx, v in ifx.items():
        if len(idx) == 4:
            ip = '.'.join(str(x) for x in idx)
            m = msk.get(idx)
            mask = str(ipaddress.IPv4Address(bytes(m.asOctets()))) if m is not None and hasattr(m, 'asOctets') else str(m or '255.255.255.255')
            addrs.append((ip, mask, int(v)))
    vlans = {idx[-1]: text(v) for idx, v in (await s.walk(OID['qVlanName'])).items() if idx}
    if not vlans:
        vlans = {idx[-1]: text(v) for idx, v in (await s.walk(OID['ciscoVlanName'])).items()
                 if idx and not 1002 <= idx[-1] <= 1005}
    model = serial = ''
    try:
        cls = {idx[0]: int(v) for idx, v in (await s.walk(OID['entClass'])).items() if len(idx) == 1}
        mods = {idx[0]: text(v) for idx, v in (await s.walk(OID['entModel'])).items() if len(idx) == 1 and text(v)}
        sers = {idx[0]: text(v) for idx, v in (await s.walk(OID['entSerial'])).items() if len(idx) == 1 and text(v)}
        chassis = [i for i in sorted(cls) if cls[i] == 3] or sorted(mods)
        for i in chassis:
            if mods.get(i):
                model, serial = mods[i].strip(), sers.get(i, '').strip()
                break
    except SnmpError:
        pass
    return {'interfaces': ifs, 'addrs': addrs, 'vlans': vlans, 'model': model, 'serial': serial}


# --------------------------------------------------------------------- 장비 1대 / 전체
def credentials():
    creds = [c for c in (cfg('snmp_credentials') or []) if c and c.get('user')]
    return creds


async def poll_device(engine, dev, creds, what=('arp', 'mac')):
    """dev: {'name','host','port','arp','mac'} → 결과 dict (예외 대신 error 에 사유)"""
    res = {'name': dev['name'], 'host': dev['host'], 'arp': [], 'mac': [], 'error': None, 'method': '', 'secs': 0}
    t0 = time.monotonic()
    errs = []
    for n, cred in enumerate(creds, 1):  # 계정이 여러 개면 순서대로 시도
        s = Session(engine, dev['host'], dev.get('port') or cfg('snmp_port'), cred,
                    cfg('snmp_timeout'), cfg('snmp_retries'))
        try:
            res['system'] = await read_system(s)
            res['user'] = cred['user']
            break
        except SnmpError as e:
            errs.append(f"계정{n}({cred['user']}): {e}" if len(creds) > 1 else str(e))
            if '타임아웃' in str(e):
                break  # 응답이 없는 장비는 다른 계정도 소용없음 → 시간 절약
    else:
        errs = errs or ['SNMP 계정이 설정되지 않음(env/ipam-snmp.env 확인)']
    if errs and 'system' not in res:
        res['error'] = ' / '.join(errs)
        res['secs'] = round(time.monotonic() - t0, 1)
        return res
    try:
        ifn = await read_ifnames(s)
        if 'arp' in what and dev.get('arp'):
            res['arp'] = await read_arp(s, ifn)
        if 'mac' in what and dev.get('mac'):
            res['mac'], res['method'] = await read_mac(s, ifn, res['system']['enterprise'])
        if 'inv' in what:
            res['inv'] = await read_inventory(s, ifn)
    except SnmpError as e:
        res['error'] = f'수집 중 오류: {e}'
    res['secs'] = round(time.monotonic() - t0, 1)
    return res


async def _poll_all(devices, creds, what):
    from pysnmp.hlapi.v3arch import asyncio as h
    engine = h.SnmpEngine()
    sem = asyncio.Semaphore(cfg('snmp_concurrency'))

    async def one(d):
        async with sem:
            try:
                return await asyncio.wait_for(poll_device(engine, d, creds, what), cfg('snmp_device_timeout'))
            except asyncio.TimeoutError:
                return {'name': d['name'], 'host': d['host'], 'arp': [], 'mac': [], 'method': '',
                        'error': f"장비 1대 수집 시간 초과({cfg('snmp_device_timeout')}초)", 'secs': cfg('snmp_device_timeout')}
    try:
        return await asyncio.gather(*[one(d) for d in devices])
    finally:
        engine.close_dispatcher()


def poll(devices, what=('arp', 'mac'), creds=None):
    return asyncio.run(_poll_all(devices, credentials() if creds is None else creds, what))


def targets(device_ids=None):
    """NetBox 장치 → 수집 대상 목록. 기본 IPv4 가 없는 장치는 건너뛰고 사유를 돌려준다."""
    from dcim.models import Device
    from django.db.models import Q
    roles = cfg('snmp_mac_roles') or []
    qs = Device.objects.filter(status='active')
    if device_ids:
        qs = qs.filter(pk__in=device_ids)
    else:
        qs = qs.filter(Q(tags__slug__in=['ipam-arp', 'ipam-mac']) | Q(role__slug__in=roles)).distinct()
    out, skipped = [], []
    for d in qs.select_related('primary_ip4', 'role').prefetch_related('tags'):
        tags = {t.slug for t in d.tags.all()}
        arp = 'ipam-arp' in tags
        mac = 'ipam-mac' in tags or (d.role and d.role.slug in roles)
        if device_ids and not (arp or mac):
            arp = mac = True  # 직접 고른 장치는 둘 다 시도
        if not d.primary_ip4:
            skipped.append((d.name, '기본 IPv4(primary IP) 없음'))
            continue
        lc = d.local_context_data or {}  # 장치 '로컬 설정 컨텍스트'로 예외 지정 가능: {"snmp_port": 1161, "snmp_host": "10.0.0.5"}
        out.append({'name': d.name, 'host': lc.get('snmp_host') or str(d.primary_ip4.address.ip),
                    'port': lc.get('snmp_port'), 'arp': arp, 'mac': mac})
    return out, skipped


def ingest(results, now=None):
    """수집 결과를 대장에 반영(동기, ORM). 반환: 합계 요약."""
    from . import logic
    now = now or timezone.now()
    tot = {'devices': len(results), 'ok': 0, 'failed': 0, 'arp': 0, 'mac': 0, 'unregistered_use': 0,
           'mac_mismatch': 0, 'ip_conflict': 0, 'revived': 0, 'mac_filled': 0}
    for r in results:
        if r['error']:
            tot['failed'] += 1
            continue
        tot['ok'] += 1
        if r['arp']:
            f = logic.ingest_arp(r['name'], r['arp'], now=now)
            for k, v in f.items():
                tot[k] = tot.get(k, 0) + v
            tot['arp'] += len(r['arp'])
        if r['mac']:
            logic.ingest_mac(r['name'], r['mac'], now=now)
            tot['mac'] += len(r['mac'])
    tot['ports'] = logic.check_ports() if tot['mac'] else {}
    return tot


def ensure_tags():
    from extras.models import Tag
    for slug, name, color, desc in (('ipam-arp', 'IPAM ARP 수집', '2196f3', 'SNMP로 ARP 테이블 수집(L3 장비)'),
                                    ('ipam-mac', 'IPAM MAC 수집', '4caf50', 'SNMP로 MAC 테이블 수집(L2 스위치)'),
                                    ('ipam-discovered', 'IPAM 자동 발견', 'ff9800', '대장에 없던 IP를 ARP 수집으로 자동 등록')):
        Tag.objects.get_or_create(slug=slug, defaults={'name': name, 'color': color, 'description': desc})
