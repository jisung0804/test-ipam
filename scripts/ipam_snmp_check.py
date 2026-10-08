"""SNMP 수집 점검 / 즉시 수집 (NetBox 웹 화면: 사용자 정의 › 스크립트)

- 장치를 고르지 않으면: 자동 수집 대상 전체(태그 ipam-arp / ipam-mac, 역할 access-switch)를 지금 수집
- 장치를 고르면: 그 장치만 수집 (새 장비 SNMP 설정이 맞는지 확인할 때)
- IP 직접 입력: NetBox에 아직 등록하지 않은 장비의 SNMP 응답만 확인 (대장 반영 안 함)
'변경 사항 커밋'을 체크하지 않으면 읽기만 하고 대장에는 반영하지 않는다.
"""
from dcim.models import Device
from django.conf import settings
from extras.scripts import BooleanVar, ChoiceVar, IPAddressVar, MultiObjectVar, Script
from utilities.exceptions import AbortScript

from netbox_ip_request import snmp


class SnmpCheck(Script):
    class Meta:
        name = 'SNMP 수집 점검 / 지금 수집'
        description = '장비에서 ARP·MAC 테이블을 SNMPv3로 읽어 결과를 보여주고, 커밋하면 대장과 대사합니다.'
        commit_default = False
        scheduling_enabled = False
        job_timeout = 1800
        fieldsets = (('대상', ('devices', 'host')), ('수집 항목', ('what', 'register')))

    devices = MultiObjectVar(model=Device, required=False, label='장치',
                             description='비워 두면 자동 수집 대상 전체')
    host = IPAddressVar(required=False, label='IP 직접 입력',
                        description='NetBox에 없는 장비의 응답만 확인 (예: 10.0.0.1/32)')
    what = ChoiceVar(choices=(('both', 'ARP + MAC'), ('arp', 'ARP만'), ('mac', 'MAC만')), default='both',
                     label='수집 항목')
    register = BooleanVar(label='대장에 없는 IP 자동 등록',
                          default=settings.PLUGINS_CONFIG.get('netbox_ip_request', {}).get('auto_register_discovered', True),
                          description="ARP에 보였지만 IP 주소 목록에 없는 단말을 '자동 발견' 태그로 등록 (대역이 없으면 /24 생성). "
                                      "기본값은 서버 설정 IPAM_AUTO_REGISTER 를 따름 — 관리자 판정 기간(0)에는 꺼 둘 것")

    def run(self, data, commit):
        snmp.ensure_tags()
        what = ('arp', 'mac') if data['what'] == 'both' else (data['what'],)
        creds = snmp.credentials()
        if not creds:
            raise AbortScript('SNMP 계정이 설정되지 않았습니다. env/ipam-snmp.env 에 IPAM_SNMP_USER 등을 넣고 '
                              'docker compose up -d 로 다시 시작하세요.')
        if data.get('host'):
            devs = [{'name': str(data['host'].ip), 'host': str(data['host'].ip), 'arp': True, 'mac': True}]
            commit_ok = False
        else:
            ids = [d.pk for d in data['devices']] if data.get('devices') else None
            devs, skipped = snmp.targets(ids)
            for n, why in skipped:
                self.log_warning(f'{n}: 건너뜀 — {why}')
            commit_ok = True
        if not devs:
            raise AbortScript('수집할 장치가 없습니다. 장치에 태그 "IPAM ARP 수집"/"IPAM MAC 수집"을 붙이거나 '
                              '역할이 access-switch 인 장치가 기본 IP를 가지고 있어야 합니다.')
        self.log_info(f'대상 {len(devs)}대 수집 시작 (계정 {len(creds)}개 순서대로 시도)')
        results = snmp.poll(devs, what, creds)

        for r in results:
            if r['error']:
                self.log_failure(f"**{r['name']}** ({r['host']}) 실패 — {r['error']}")
                continue
            sy = r.get('system', {})
            self.log_success(f"**{r['name']}** ({r['host']}) {sy.get('vendor')} · 응답이름 {sy.get('name') or '-'} · "
                             f"ARP {len(r['arp'])}건 · MAC {len(r['mac'])}건 {r['method']} · {r['secs']}초")
            if len(results) <= 5:
                for ip, mac, itf in r['arp'][:10]:
                    self.log_debug(f'ARP {ip}  {mac}  {itf}')
                for mac, vlan, port in r['mac'][:10]:
                    self.log_debug(f'MAC {mac}  VLAN {vlan}  {port}')

        ok = [r for r in results if not r['error']]
        self.log_info(f'성공 {len(ok)}대 / 실패 {len(results) - len(ok)}대')
        if not commit_ok:
            self.log_info('IP 직접 입력 점검은 대장에 반영하지 않습니다.')
            return
        from netbox_ip_request.room_import import ensure_custom_fields
        ensure_custom_fields()
        tot = snmp.ingest(results)
        if data.get('register'):
            from netbox_ip_request.logic import check_ports, register_discovered
            reg = register_discovered()
            if reg['created']:
                tot['ports'] = check_ports()
            self.log_info(f"자동 발견 등록 — IP {reg['created']}건, 새 대역(프리픽스) {reg['prefixes']}개, 건너뜀 {reg['skipped']}건")
        msg = (f"대장 대사 — 미등록 사용 {tot['unregistered_use']} · MAC 불일치 {tot['mac_mismatch']} · "
               f"IP 충돌 {tot['ip_conflict']} · 미사용→사용 복귀 {tot['revived']} · MAC 자동 채움 {tot['mac_filled']}")
        if tot.get('ports'):
            p = tot['ports']
            msg += f" · 포트 일치 {p['matched']} / 채움 {p['filled']} / 불일치 {p['mismatch']}"
        # 대사 결과·관리자 판정 화면(recon_state)은 reconcile 이 채운다 — 수집 직후 바로 계산
        from netbox.plugins import get_plugin_config
        from netbox_ip_request.recon import reconcile
        rc = reconcile(days=get_plugin_config('netbox_ip_request', 'recon_days') or 30)
        if isinstance(rc, dict):
            self.log_info('대사 결과 갱신 — ' + ' · '.join(f'{k} {v}' for k, v in rc.items()))
        if commit:
            self.log_success('반영 완료 — ' + msg)
        else:
            self.log_warning('점검만 함(반영 안 함) — 커밋하면: ' + msg)
