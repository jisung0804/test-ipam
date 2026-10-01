import logging

from django.conf import settings

from netbox.jobs import JobRunner, system_job

logger = logging.getLogger('netbox_ip_request')


@system_job(interval=1440)  # 매일 1회 (분 단위)
class DormantIPJob(JobRunner):
    class Meta:
        name = 'IP 장기 미사용 판정 및 격리 해제'

    def run(self, *args, **kwargs):
        from .logic import classify_dormant, release_quarantine
        dormant = classify_dormant()
        released = release_quarantine()
        self.job.data = {'dormant': dormant, 'released': released}


# 주기는 설정(snmp_interval, 분)을 따른다. 0 이면 자동 수집을 등록하지 않음.
_INTERVAL = int(settings.PLUGINS_CONFIG.get('netbox_ip_request', {}).get('snmp_interval', 5) or 0)


class SnmpCollectJob(JobRunner):
    """태그(ipam-arp / ipam-mac) 또는 역할(access-switch) 장치에서 ARP·MAC 테이블을 SNMPv3로 읽어 대장과 대사."""

    class Meta:
        name = 'SNMP ARP/MAC 자동 수집'

    def run(self, *args, **kwargs):
        from . import snmp
        if not snmp.credentials():
            self.job.data = {'skipped': 'SNMP 계정 미설정 (env/ipam-snmp.env)'}
            return
        from .room_import import ensure_custom_fields
        from netbox.plugins import get_plugin_config
        ensure_custom_fields()
        devices, skipped = snmp.targets()
        results = snmp.poll(devices)
        tot = snmp.ingest(results)
        if get_plugin_config('netbox_ip_request', 'auto_register_discovered'):
            from .logic import check_ports, register_discovered
            tot['discovered'] = register_discovered()
            if tot['discovered']['created']:
                tot['ports'] = check_ports()   # 새로 등록된 IP의 스위치·포트를 MAC 테이블로 채움
        tot['skipped'] = [f'{n}: {why}' for n, why in skipped]
        tot['errors'] = [f"{r['name']}({r['host']}): {r['error']}" for r in results if r['error']][:200]
        self.job.data = tot
        logger.info('SNMP 수집: 장비 %s (성공 %s, 실패 %s) ARP %s MAC %s',
                    tot['devices'], tot['ok'], tot['failed'], tot['arp'], tot['mac'])


if _INTERVAL > 0:
    SnmpCollectJob = system_job(interval=_INTERVAL)(SnmpCollectJob)
