from netbox.plugins import PluginConfig


class IPRequestConfig(PluginConfig):
    name = 'netbox_ip_request'
    verbose_name = 'IP 발급 신청'
    description = 'IP 발급 신청·승인, ARP/MAC 대사, 장기 미사용 IP 판정'
    version = '0.3.1'
    base_url = 'ip-request'
    min_version = '4.5.0'
    default_settings = {
        'arp_guard_days': 30,      # 최근 N일 ARP 관측 IP는 할당 제외
        'dormant_days': 180,       # 장기 미사용 판정 기준
        'quarantine_days': 30,     # 회수 후 격리 기간
        # ---- Phase 2: SNMPv3 자동 수집
        'snmp_credentials': [],    # [{'user','auth_key','priv_key','auth_proto':'sha','priv_proto':'aes'}] 순서대로 시도
        'snmp_interval': 5,        # 자동 수집 주기(분). 0 이면 자동 수집 끔
        'snmp_port': 161,
        'snmp_timeout': 2,         # 요청 1회 대기(초)
        'snmp_retries': 1,
        'snmp_concurrency': 20,    # 동시에 수집할 장비 수
        'snmp_device_timeout': 120,  # 장비 1대 최대 수집 시간(초)
        'snmp_mac_roles': ['access-switch'],  # 이 역할의 장치는 태그 없이도 MAC 수집
        'auto_register_discovered': False,  # 수집할 때마다 대장에 없는 IP를 '자동 발견'으로 등록
        'discovered_prefix_len': 24,       # 자동 발견 IP가 들어갈 대역이 없을 때 만들 프리픽스 길이
        # ---- 화면
        'field_labels': {},        # IP 주소 기본 칸의 화면 이름 바꾸기 {'tenant': '소속', ...}
        'draggable_tables': ['ipam.ipaddress'],  # 열 제목 끌어서 순서 바꾸기를 켤 목록
    }

    def ready(self):
        super().ready()
        from . import jobs  # noqa: F401  (system job 등록)
        from .ui import apply_labels
        from netbox.plugins import get_plugin_config
        apply_labels(get_plugin_config('netbox_ip_request', 'field_labels'))
        # DB 준비(migrate) 직후 필요한 사용자 정의 필드·태그를 자동 생성 (netbox-docker는 시작할 때마다 migrate 실행)
        from django.db.models.signals import post_migrate
        post_migrate.connect(_ensure_objects, sender=self)


def _ensure_objects(sender, **kwargs):
    try:
        from .room_import import ensure_custom_fields
        from .snmp import ensure_tags
        ensure_custom_fields()
        ensure_tags()
    except Exception as e:  # 필드 생성 실패가 NetBox 기동을 막지 않게
        import logging
        logging.getLogger('netbox_ip_request').warning('사용자 정의 필드/태그 자동 생성 실패: %s', e)


config = IPRequestConfig
