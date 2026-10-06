import os

# 신청 플러그인 활성화 + 설정
PLUGINS = ['netbox_ip_request']


def _snmp_credentials():
    """SNMPv3 계정은 파일(Git)에 적지 않고 환경변수로만 받는다 → env/ipam-snmp.env
    장비마다 계정이 다르면 _2, _3 을 붙여 최대 3개까지 (앞에서부터 순서대로 시도)."""
    out = []
    for sfx in ('', '_2', '_3'):
        user = os.environ.get('IPAM_SNMP_USER' + sfx, '').strip()
        if user:
            out.append({
                'user': user,
                'auth_key': os.environ.get('IPAM_SNMP_AUTH' + sfx, ''),
                'priv_key': os.environ.get('IPAM_SNMP_PRIV' + sfx, ''),
                'auth_proto': os.environ.get('IPAM_SNMP_AUTH_PROTO' + sfx, 'sha'),
                'priv_proto': os.environ.get('IPAM_SNMP_PRIV_PROTO' + sfx, 'aes'),
            })
    return out


PLUGINS_CONFIG = {
    'netbox_ip_request': {
        'arp_guard_days': 30,    # 최근 30일 ARP에 보인 IP는 할당 제외
        'dormant_days': 180,     # 장기 미사용 판정 기준
        'quarantine_days': 30,   # 회수 후 격리 기간

        # ---- IP 신청·발급 ------------------------------------------------------------------------------
        'alloc_host_min': 21,    # 자동 발급 범위: /24 대역 마지막 자리 21~252 (그 밖은 자동 발급 제외, 관리자 수동 지정은 가능)
        'alloc_host_max': 252,
        'default_period_days': 180,   # 신청 사용 기한(일). 신청자는 변경 불가, 관리자만 수정
        'gateway_offset': 1,     # 대역에 '게이트웨이' IP 가 없을 때 안내 메일 gateway = 대역 시작 + 1
        # 안내 메일의 DNS·문의처 — env/ipam-snmp.env 에 IPAM_DNS=1.1.1.1, 2.2.2.2 / IPAM_ADMIN_CONTACT=... 로 지정
        'dns_servers': [x.strip() for x in os.environ.get('IPAM_DNS', '').split(',') if x.strip()],
        'admin_contact': os.environ.get('IPAM_ADMIN_CONTACT', ''),
        'mail_from': os.environ.get('IPAM_MAIL_FROM', ''),   # 비우면 env/netbox.env 의 EMAIL_FROM
        # ---- Phase 2: SNMPv3 자동 수집
        'snmp_credentials': _snmp_credentials(),
        'snmp_interval': int(os.environ.get('IPAM_SNMP_INTERVAL', '5')),   # 분. 0 = 자동 수집 끔
        'snmp_concurrency': int(os.environ.get('IPAM_SNMP_CONCURRENCY', '20')),
        'snmp_port': int(os.environ.get('IPAM_SNMP_PORT', '161')),
        'snmp_timeout': 2,
        'snmp_retries': 1,
        'snmp_mac_roles': ['access-switch'],   # 엑셀로 만든 스위치(역할 access-switch)는 태그 없이도 MAC 수집
        # 자동 수집 때마다 대장에 없는 단말 IP를 '자동 발견' 태그로 IP 주소 목록에 등록 (끄려면 IPAM_AUTO_REGISTER=0)
        'auto_register_discovered': os.environ.get('IPAM_AUTO_REGISTER', '1') == '1',
        'discovered_prefix_len': 24,           # 그 IP가 들어갈 대역이 없으면 만들 프리픽스 길이(C클래스 = 24)

        # ---- 화면 이름 (IP 주소 목록·편집·필터 화면의 기본 칸 이름) ----------------------------------------
        # 왼쪽 = NetBox 내부 이름(바꾸지 말 것), 오른쪽 = 화면에 보일 이름(자유롭게 수정).
        # 고친 뒤:  docker compose restart netbox netbox-worker   (빌드 다시 할 필요 없음)
        # 호관호실·MAC ADDRESS 같은 사용자 정의 필드 이름은 여기가 아니라
        #   NetBox 화면 > 사용자 정의 > 사용자 정의 필드 > 해당 필드 > 편집 > '라벨' 에서 바꾼다.
        'field_labels': {
            'tenant': '소속',              # 엑셀 '학부(과)'
            'description': '호실명',       # 엑셀 '호관호실명칭'
            'dns_name': '호스트 이름',     # 엑셀 '호스트 이름'
            'comments': '비고',            # 엑셀 '비고' (+ 형식 오류 원본값, 통합된 다른 값)
            'address': 'IP ADDRESS',       # 엑셀 'SUBNET ADDRESS' + 'IP ADDRESS'
        },
        # 열 제목을 끌어서 순서를 바꿀 수 있는 목록 (다른 목록도 원하면 추가: 'ipam.prefix', 'dcim.device' 등)
        'draggable_tables': ['ipam.ipaddress'],
        # IP 주소 빠른 검색을 '앞뒤 상관없이 포함' 검색으로 (False 면 NetBox 기본: 앞부분 일치)
        'contains_search': True,
        # 대사: 이 기간(일) 안에 ARP 에 안 보이면 '미관측', IP 자원 화면 '실사용' 기준
        # 엑셀은 최초 값 — 실제 L2(SNMP)로 본 MAC·스위치·포트로 대장을 덮어씀. 0 이면 대사 결과만 표시(관리자 판정 기간)
        'l2_overwrite': os.environ.get('IPAM_L2_OVERWRITE', '1') == '1',
        'recon_days': 30,
        # 장비 인터페이스·VLAN·대역 SNMP 동기화 주기(분). 0 = 끔
        'inventory_interval': 1440,
    }
}

# IP 상태에 dormant / quarantine 추가 (NetBox 코어 수정 없이 설정으로 확장)
FIELD_CHOICES = {
    'ipam.IPAddress.status+': (
        ('dormant', 'Dormant (180d unseen)', 'orange'),
        ('quarantine', 'Quarantine', 'red'),
    )
}

# 웹 화면 엑셀 업로드(사용자 정의 스크립트) 허용 크기.
# 기본 2.5MB를 넘는 파일은 NetBox가 백그라운드 작업으로 넘기지 못해 오류(500)가 나므로 20MB로 올림
FILE_UPLOAD_MAX_MEMORY_SIZE = 20 * 1024 * 1024

# 백그라운드 작업 최대 실행 시간(초). 장비가 많으면 SNMP 수집 1회가 5분(기본값)을 넘을 수 있어 15분으로 늘림
RQ_DEFAULT_TIMEOUT = 900
