"""IPAM 운영 도구 (NetBox 화면: 사용자 정의 › 스크립트 › 이 파일 한 번 추가)

1. 장비 등록 (관리 IP만 입력)       — IP만 넣으면 SNMP 로 장치·인터페이스·VLAN·대역·관리 IP 까지 등록하고 바로 수집
2. VLAN 정보 엑셀 업로드           — VLAN 이름·설명·대역·소속·게이트웨이 일괄 반영
3. 대사 판정 반영                  — '관리자 판정' 칸에 정한 대로 대장 수정 / 회수 / 삭제
4. IP 일괄 삭제(조건)              — 대역·태그·상태·판정 조건으로 많은 IP 를 백그라운드에서 삭제
모든 도구는 '변경 사항 커밋'을 끄고 먼저 실행하면 점검만 하고 아무것도 바꾸지 않는다.
"""
import ipaddress
import re

from dcim.models import Site
from extras.models import Tag
from extras.scripts import BooleanVar, ChoiceVar, FileVar, MultiObjectVar, ObjectVar, Script, StringVar, TextVar
from ipam.models import Prefix
from utilities.exceptions import AbortScript

import netbox_ip_request

NEED = '0.4.0'


def _check_version(script):
    ver = netbox_ip_request.config.version
    if tuple(int(x) for x in ver.split('.')) < tuple(int(x) for x in NEED.split('.')):
        raise AbortScript(f'NetBox 이미지의 플러그인이 옛 버전({ver})입니다 — 새 zip 적용 후 '
                          f'"docker compose build --no-cache" → "docker compose up -d"')
    script.log_info(f'플러그인 버전 {ver}')


def parse_ips(text):
    """한 줄에 하나, 쉼표/공백 구분, 범위(165.246.49.2-9 또는 165.246.49.2-165.246.49.9) 지원"""
    out, bad = [], []
    for tok in re.split(r'[\s,;]+', text or ''):
        tok = tok.strip()
        if not tok:
            continue
        m = re.fullmatch(r'(\d+\.\d+\.\d+\.)(\d+)-(\d+)', tok)
        m2 = re.fullmatch(r'(\d+\.\d+\.\d+\.\d+)-(\d+\.\d+\.\d+\.\d+)', tok)
        try:
            if m:
                a, b = int(m.group(2)), int(m.group(3))
                out += [str(ipaddress.ip_address(f'{m.group(1)}{i}')) for i in range(a, b + 1)]
            elif m2:
                a, b = ipaddress.ip_address(m2.group(1)), ipaddress.ip_address(m2.group(2))
                if int(b) - int(a) > 1024:
                    raise ValueError
                out += [str(ipaddress.ip_address(i)) for i in range(int(a), int(b) + 1)]
            else:
                out.append(str(ipaddress.ip_address(tok.split('/')[0])))
        except ValueError:
            bad.append(tok)
    return list(dict.fromkeys(out)), bad


class DeviceOnboard(Script):
    class Meta:
        name = '1. 장비 등록 (관리 IP만 입력)'
        description = '관리 IP만 넣으면 SNMP로 장치·인터페이스·VLAN·대역·관리 IP를 등록하고 ARP/MAC 수집·대사까지 실행'
        commit_default = True
        scheduling_enabled = False
        job_timeout = 3600
        fieldsets = (('장비', ('ips', 'site')), ('옵션', ('role', 'rename', 'collect')))

    ips = TextVar(label='관리 IP', required=False,
                  description='한 줄에 하나 (쉼표 가능). 범위: 165.246.49.2-9 · 비워 두면 이미 등록된 장비 전체를 다시 동기화')
    site = ObjectVar(model=Site, label='사이트')
    role = ChoiceVar(label='역할', choices=(('auto', '자동 판별 (ARP·여러 대역이 있으면 L3)'), ('l3', 'L3 스위치(코어·분배)'),
                                           ('l2', 'L2 액세스 스위치')), default='auto')
    rename = BooleanVar(label='이미 있는 장치 이름을 장비 이름(sysName)으로 바꾸기', default=False)
    collect = BooleanVar(label='등록 후 바로 ARP/MAC 수집·대사', default=True)

    def run(self, data, commit):
        _check_version(self)
        from netbox_ip_request.inventory import onboard
        from netbox_ip_request import snmp
        if not snmp.credentials():
            raise AbortScript('SNMP 계정이 설정되지 않았습니다 (env/ipam-snmp.env)')
        ips, bad = parse_ips(data.get('ips'))
        for b in bad:
            self.log_warning(f'IP 형식 오류로 건너뜀: {b}')
        ports = {}
        if not ips:
            targets, _ = snmp.targets()
            ips = [t['host'] for t in targets]
            ports = {t['host']: t['port'] for t in targets if t.get('port')}
            self.log_info(f'IP를 비워 두어 등록된 장비 {len(ips)}대 전체를 다시 동기화합니다')
        else:
            from dcim.models import Device
            for d in Device.objects.filter(primary_ip4__isnull=False).select_related('primary_ip4'):
                h = str(d.primary_ip4.address.ip)
                if h in ips and (d.local_context_data or {}).get('snmp_port'):
                    ports[h] = d.local_context_data['snmp_port']
        if not ips:
            raise AbortScript('등록할 IP가 없습니다')
        self.log_info(f'{len(ips)}대 SNMP 조회 시작')
        rows, summary = onboard(ips, data['site'], data['role'], data['rename'], data['collect'],
                                log=self.log_info, ports=ports)
        for ip, st, msg in rows:
            (self.log_failure if st == '실패' else self.log_success)(f'**{ip}** {st} — {msg}')
        ok = sum(1 for r in rows if r[1] != '실패')
        self.log_info(f'성공 {ok}대 / 실패 {len(rows) - ok}대')
        if summary:
            d = summary.get('discovered', {})
            r = summary.get('recon', {})
            self.log_info(f"수집: ARP {summary.get('arp', 0)} · MAC {summary.get('mac', 0)} · 자동 발견 IP {d.get('created', 0)} · "
                          f"대사 — 일치 {r.get('ok', 0)}, MAC 불일치 {r.get('mac_diff', 0)}, 위치 불일치 {r.get('port_diff', 0)}, "
                          f"미관측 {r.get('unseen', 0)}, 자동 발견 {r.get('discovered', 0)}, 충돌 {r.get('conflict', 0)}")
        if not commit:
            self.log_warning('점검만 함(반영 안 함) — 커밋하면 위 내용이 저장됩니다')


class VlanImport(Script):
    class Meta:
        name = '2. VLAN 정보 엑셀 업로드'
        description = "열: VLAN ID | VLAN 이름 | 설명 | 대역 | 소속 | 게이트웨이 — 'IP 자원 현황'의 엑셀 내보내기 파일을 고쳐 올려도 됨"
        commit_default = False
        scheduling_enabled = False
        job_timeout = 1800

    excel_file = FileVar(label='엑셀 파일 (.xlsx)')
    site = ObjectVar(model=Site, label='사이트')
    base = StringVar(label='IP 앞 두 자리', default='165.246', regex=r'^\d{1,3}\.\d{1,3}$',
                     description="대역 칸에 '49'처럼 세 번째 자리만 적었을 때 붙일 앞자리")

    def run(self, data, commit):
        _check_version(self)
        from netbox_ip_request.resources import import_vlans
        try:
            c = import_vlans(data['excel_file'], data['site'], data['base'], log=self.log_warning)
        except ValueError as e:
            raise AbortScript(str(e))
        msg = (f"VLAN 신규 {c['vlan_new']} · 수정 {c['vlan_upd']} · 대역 연결 {c['prefix_link']}(신규 {c['prefix_new']}) · "
               f"게이트웨이 신규 {c['gateway_new']} · 건너뜀 {c['skipped'] + c['prefix_bad']}")
        (self.log_success if commit else self.log_warning)(('반영 완료 — ' if commit else '점검만 함(반영 안 함) — ') + msg)


class ReviewApply(Script):
    class Meta:
        name = '3. 대사 판정 반영'
        description = "IP 주소의 '관리자 판정'대로: 대장 수정 필요 → 관측값 반영 · 회수 대상 → 격리 · 삭제 대상 → 삭제"
        commit_default = False
        scheduling_enabled = False
        job_timeout = 3600

    fix = BooleanVar(label="'대장 수정 필요' → 관측 MAC·위치를 대장에 반영", default=True)
    reclaim = BooleanVar(label="'회수 대상' → 회수(격리)", default=True)
    delete = BooleanVar(label="'삭제 대상' → 삭제", default=False)

    def run(self, data, commit):
        _check_version(self)
        from netbox_ip_request.recon import apply_review
        kinds = {k for k, f in (('fix_ledger', 'fix'), ('reclaim', 'reclaim'), ('delete', 'delete')) if data[f]}
        if not kinds:
            raise AbortScript('처리할 판정을 하나 이상 고르세요')
        res = apply_review(kinds, log=self.log_info)
        msg = f"대장 수정 {res['fix_ledger']} · 회수 {res['reclaim']} · 삭제 {res['delete']} · 건너뜀 {res['skipped']}"
        (self.log_success if commit else self.log_warning)(('반영 완료 — ' if commit else '점검만 함(반영 안 함) — ') + msg)


class BulkDelete(Script):
    class Meta:
        name = '4. IP 일괄 삭제(조건)'
        description = '대역·태그·상태·대사 결과·판정 조건에 맞는 IP를 한꺼번에 삭제 (수천 건도 화면 멈춤 없이)'
        commit_default = False
        scheduling_enabled = False
        job_timeout = 3600
        fieldsets = (('조건 (모두 만족하는 IP)', ('prefixes', 'tags', 'status', 'recon_state', 'review', 'q')),
                     ('보호', ('keep_assigned',)))

    prefixes = MultiObjectVar(model=Prefix, label='대역', required=False)
    tags = MultiObjectVar(model=Tag, label='태그', required=False)
    status = ChoiceVar(label='상태', required=False, choices=(('', '상관없음'), ('active', '활성'), ('reserved', '예약됨'),
                                                             ('deprecated', '사용 중단'), ('dormant', '미사용(Dormant)'),
                                                             ('quarantine', '격리')))
    recon_state = ChoiceVar(label='대사 결과', required=False, choices=(('', '상관없음'), ('unseen', '미관측'),
                                                                     ('discovered', '대장 없음(자동 발견)'), ('mac_diff', 'MAC 불일치'),
                                                                     ('port_diff', '위치 불일치'), ('conflict', 'IP 충돌'), ('ok', '일치')))
    review = ChoiceVar(label='관리자 판정', required=False, choices=(('', '상관없음'), ('delete', '삭제 대상'),
                                                                  ('reclaim', '회수 대상'), ('ignore', '예외(무시)')))
    q = StringVar(label='포함 검색어', required=False, description='IP 목록 빠른 검색과 같은 규칙')
    keep_assigned = BooleanVar(label='장비 인터페이스에 붙은 IP(관리 IP·게이트웨이)는 지우지 않음', default=True)

    def run(self, data, commit):
        _check_version(self)
        from ipam.filtersets import IPAddressFilterSet
        from ipam.models import IPAddress
        from django.db.models import Q
        qs = IPAddress.objects.all()
        if data.get('prefixes'):
            cond = Q()
            for p in data['prefixes']:
                cond |= Q(address__net_host_contained=str(p.prefix))
            qs = qs.filter(cond)
        for t in data.get('tags') or []:
            qs = qs.filter(tags=t)
        if data.get('status'):
            qs = qs.filter(status=data['status'])
        if data.get('recon_state'):
            qs = qs.filter(custom_field_data__recon_state=data['recon_state'])
        if data.get('review'):
            qs = qs.filter(custom_field_data__review=data['review'])
        if data.get('q'):
            qs = IPAddressFilterSet().search(qs, 'q', data['q'])
        if data.get('keep_assigned'):
            qs = qs.filter(assigned_object_id__isnull=True)
        if not any(data.get(k) for k in ('prefixes', 'tags', 'status', 'recon_state', 'review', 'q')):
            raise AbortScript('조건을 하나 이상 지정하세요 (전체 삭제 방지)')
        n = qs.count()
        for o in qs[:20]:
            self.log_info(f'삭제 대상 예: {o} {o.description}')
        done = 0
        for o in qs.iterator(chunk_size=500):
            o.snapshot(); o.delete(); done += 1
            if done % 1000 == 0:
                self.log_info(f'{done}/{n}건 처리')
        (self.log_success if commit else self.log_warning)(
            (f'삭제 완료 — {done}건' if commit else f'점검만 함 — 커밋하면 {done}건 삭제'))
