from netbox.plugins import PluginConfig


class IPRequestConfig(PluginConfig):
    name = 'netbox_ip_request'
    verbose_name = 'IP 발급 신청'
    description = 'IP 발급 신청·승인, ARP/MAC 대사, 장기 미사용 IP 판정'
    version = '0.5.3'
    base_url = 'ip-request'
    min_version = '4.5.0'
    default_settings = {
        'arp_guard_days': 30,      # 최근 N일 ARP 관측 IP는 할당 제외
        'dormant_days': 180,       # 장기 미사용 판정 기준
        'quarantine_days': 30,     # 회수 후 격리 기간
        # ---- IP 신청·발급
        'alloc_host_min': 21,      # 자동 발급 범위: /24 대역 마지막 자리 21~252 (그 밖은 발급 제외)
        'alloc_host_max': 252,
        'max_ip_count': 10,          # 신청 1건에 받을 수 있는 최대 IP 개수
        'default_period_days': 180,  # 신청 사용 기한(일) — 신청자는 변경 불가, 관리자만 수정
        'gateway_offset': 1,       # 게이트웨이 IP 를 못 찾을 때 대역 시작 + N
        'dns_servers': [],         # 안내 메일에 넣을 DNS 서버
        'admin_contact': '',       # 안내 메일 하단 문의처
        'mail_from': '',           # 보내는 사람 주소 (비우면 NetBox EMAIL FROM_EMAIL)
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
        'contains_search': True,   # IP 주소 빠른 검색: 앞뒤 상관없이 포함 검색 (끄면 NetBox 기본)
        'l2_overwrite': True,      # 대사 때 실제 L2(ARP·MAC 테이블)로 대장의 MAC·스위치·포트를 덮어씀 (엑셀은 최초 값)
        'recon_days': 30,          # 대사: 이 기간 안에 ARP 에 안 보이면 '미관측'
        'inventory_interval': 1440,  # 장비 인터페이스·VLAN 동기화 주기(분). 0 = 끔
    }

    def ready(self):
        super().ready()
        from . import jobs  # noqa: F401  (system job 등록)
        from .ui import apply_labels, patch_ip_search
        from netbox.plugins import get_plugin_config
        apply_labels(get_plugin_config('netbox_ip_request', 'field_labels'))
        if get_plugin_config('netbox_ip_request', 'contains_search'):
            patch_ip_search()
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
