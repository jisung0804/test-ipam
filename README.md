# netbox-ip-request (NetBox 4.7 플러그인)

IP 발급 신청·승인, ARP/MAC 대사, 장기 미사용 IP 판정을 NetBox에 추가한다.
검증 환경: NetBox 4.7.1, PostgreSQL 16, Redis 7, Python 3.12.

## 설치
1. NetBox 서버에서 `pip install /path/to/netbox-ip-request` (운영: `local_requirements.txt`에 경로 추가 후 `upgrade.sh`)
2. `configuration.py`
   ```python
   PLUGINS = ['netbox_ip_request']
   PLUGINS_CONFIG = {'netbox_ip_request': {'arp_guard_days': 30, 'dormant_days': 180, 'quarantine_days': 30}}
   FIELD_CHOICES = {'ipam.IPAddress.status+': (
       ('dormant', 'Dormant (180d unseen)', 'orange'),
       ('quarantine', 'Quarantine', 'red'))}
   ```
3. `python manage.py migrate && python manage.py collectstatic --no-input` 후 netbox, netbox-rq 재시작
4. `tools/setup_netbox.py` 실행 → IP 사용자 정의 필드 7개 생성

## 도구 (tools/)
| 파일 | 용도 |
|---|---|
| `import_excel.py` | 엑셀 대장 검증(드라이런) → `--commit` 시 NetBox로 이관 |
| `collector.py`, `netparse.py` | 장비 ARP/MAC 수집 → 플러그인 API 전송 (5분 cron) |
| `import_room_excel.py` | 현행 호실 기준 대장(19개 컬럼) → NetBox 이관. 호실·스위치·포트까지 연결 (`--base 165.246` 등 앞 두 옥텟 지정) |
| `verify_netbox.py` | 검증 스위트 62항목 (`python verify_netbox.py 5`) — **테스트 전용 NetBox에서만 실행: 전체 데이터를 지움** |
| `mutate_netbox.py` | 방어 로직 제거 시 스위트가 잡는지 확인 |

환경변수: `NETBOX_URL`, `NETBOX_TOKEN`, (수집기) `NET_USER`, `NET_PASS`

## 규칙
플러그인·스크립트에서 IP를 쓸 때는 반드시 `logic.allocate()` 또는
`advisory_lock(ADVISORY_LOCK_KEYS['available-ips'])` → `transaction.atomic()` 순서(락이 바깥)로 감쌀 것.

## netbox-docker로 배포 (deploy/netbox-docker/)
1. `git clone -b release https://github.com/netbox-community/netbox-docker.git && cd netbox-docker`
2. 이 폴더의 파일을 그대로 복사: `Dockerfile-Plugins`, `docker-compose.override.yml`, `configuration/plugins.py`, `set_secrets.sh`
3. `netbox-ip-request/` 폴더를 netbox-docker 안에 복사
4. `bash set_secrets.sh` (최초 기동 전 1회) → `docker compose build --no-cache` → `docker compose up -d`
5. `docker compose exec netbox /opt/netbox/netbox/manage.py createsuperuser` → http://<서버>:8000
자세한 설명은 구축 가이드 문서의 "netbox-docker 설치 상세" 참고.

## 웹 화면에서 엑셀 올리기 (scripts/ipam_excel_import.py)
1. NetBox 화면 > 사용자 정의 > 스크립트 > 추가(+) > `scripts/ipam_excel_import.py` 업로드 (최초 1회)
2. 왼쪽 메뉴 IP 신청 > 엑셀 가져오기 (또는 사용자 정의 > 스크립트) > '호실 IP 대장 엑셀 가져오기'
3. 파일 선택 → '변경 사항 커밋' 해제 상태로 실행(점검) → 결과 확인 → 체크 후 다시 실행(반영)
- 2.5MB 넘는 엑셀은 `FILE_UPLOAD_MAX_MEMORY_SIZE`(deploy/netbox-docker/configuration/plugins.py에 20MB로 설정) 필요

## Phase 2 — SNMPv3 ARP/MAC 자동 수집 (netbox_ip_request/snmp.py)
- 표준 MIB만 사용: ARP = IP-MIB(ipNetToMedia → 없으면 ipNetToPhysical), MAC = Q-BRIDGE → 없으면 BRIDGE-MIB,
  Cisco IOS/IOS-XE 는 VLAN별 context `vlan-<ID>`. 포트명 = dot1dBasePortIfIndex + IF-MIB ifName.
- 대상: NetBox 장치 중 태그 `ipam-arp`(ARP), `ipam-mac` 또는 역할 `access-switch`(MAC), 접속 주소 = 기본 IPv4.
  예외 주소/포트는 장치의 로컬 설정 컨텍스트 `{"snmp_host": "...", "snmp_port": 161}`.
- 계정: `env/ipam-snmp.env` (예시 `deploy/netbox-docker/env-ipam-snmp.env.example`), 최대 3개 순서대로 시도.
- 자동 수집: 백그라운드 작업 "SNMP ARP/MAC 자동 수집" (기본 5분, IPAM_SNMP_INTERVAL). 결과는 운영 › 작업.
- 수동 점검/즉시 수집: 스크립트 `scripts/ipam_snmp_check.py` 를 사용자 정의 › 스크립트에 추가.
- 검증: `tools/snmp_simlab.py start` (시뮬레이터) → `manage.py shell -c "exec(open('tools/verify_snmp.py').read())"` — 39항목.

## 화면 이름·열 순서 / 엑셀 관대 모드
- 기본 칸 화면 이름: `configuration/plugins.py` → `field_labels` (적용: `docker compose restart netbox netbox-worker`) — `netbox_ip_request/ui.py`
- 열 제목 끌어 놓기로 순서 변경: `netbox_ip_request/template_content.py`, 켤 목록 = `draggable_tables`
- 사용자 정의 필드 이름 = 엑셀 컬럼명(마이그레이션 0003). 이후 변경은 화면 > 사용자 정의 필드 > 라벨
- 엑셀 가져오기: 기본은 형식 오류 무시 + 같은 IP 행 통합, 기존 IP = 건너뜀/합치기/덮어쓰기, 엄격 모드 선택 — `room_import.py`
- 검증: `tools/verify_excel_lenient.py` (22항목)

## 단말 IP 자동 등록 / 스위치 SNMP 일괄 설정
- 수집 때마다 대장에 없는 ARP IP를 '자동 발견' 태그로 등록(대역 없으면 /24 생성) — `logic.register_discovered`, 끄기 `IPAM_AUTO_REGISTER=0`
- 사용자 정의 필드·태그는 migrate 직후 자동 생성(post_migrate)
- `tools/snmp_bulk_config.py`: 수백 대 SNMPv3 계정 일괄 설정 (점검 → --limit 시범 → --apply, 백업·확인·결과 엑셀) — 검증 `tools/verify_bulk_config.py`(17항목)

## 0.4.0 — 장비 연동 통합 운영
- 관리 IP만으로 장비 등록·동기화: `inventory.py` (도구 1), 매일 재동기화 작업
- 대사(엑셀↔ARP·MAC·인터페이스) 자동 결과 + 관리자 판정: `recon.py` (화면 'IP 신청 › 장비 대사 결과·판정', 도구 3)
- IP 자원 현황(VLAN별)·엑셀 내보내기/VLAN 엑셀 업로드: `resources.py` (도구 2)
- IP 빠른 검색 포함 검색(`contains_search`), IP 목록 위 '선택 삭제', 조건 일괄 삭제(도구 4)
- 도구 스크립트: `scripts/ipam_tools.py` / 검증: `tools/verify_phase3.py`(46항목)

## 0.5.0 — IP 신청·발급 개선 (Phase 4)
- 신청서: 이름·소속·이메일·연락처 필수. 사용 기한 180일 고정(신청자는 변경 불가, 관리자만 수정)
- 사용 위치: 사용자는 대역을 고르지 않고 건물(드롭다운)·호실번호만 적음(모르면 비움) → `match.py` 가 IP 대장에서
  같은 호실 → 같은 층 → 같은 건물 → 대역/VLAN 설명 순으로 VLAN 대역을 찾아 넣음. 못 찾으면 '미정', 관리자가 선택
- 자동 발급: /24 대역 마지막 자리 21~252 만 사용 (`alloc_host_min`/`alloc_host_max`), 최근 ARP 에 보인 IP 제외
- 관리자: 신청 상세 화면에서 대역(VLAN) 변경 → 미리보기 → 자동 발급 / 수동 지정(범위 밖도 가능) 선택
- 발급 완료 시 신청자 이메일로 안내 메일 자동 발송(실패해도 발급은 유지, 화면에서 재발송)
  - 메일 서버: env/netbox.env 의 `EMAIL_SERVER`·`EMAIL_PORT`·`EMAIL_FROM` …
  - 메일 내용: env/ipam-snmp.env 의 `IPAM_DNS`·`IPAM_ADMIN_CONTACT`(·`IPAM_MAIL_FROM`)
- 엑셀은 최초 값: 수집(5분)마다 실제 L2(ARP·MAC 테이블)로 IP 의 MAC·스위치·포트를 덮어씀 (`l2_overwrite`, 기본 True)
  - 사용자 정의 필드 '데이터 기준'(엑셀/L2 확인/L2로 덮어씀/L2 신규), 'L2 덮어쓴 내용', '엑셀 원본값' 으로 구별
  - 엑셀을 다시 올려도 L2 로 확인된 MAC·스위치·포트는 되돌리지 않음
- 검증: `tools/verify_phase4.py` (30항목), `verify_phase3.py` P3b (L2 덮어쓰기)

## 운영 서버(CentOS) 이관 — deploy/centos/
| 파일 | 실행 위치 | 용도 |
|---|---|---|
| `install_docker.sh` | 새 서버 | Docker CE·nginx·cron 등 설치 (CentOS Stream 9/10, Rocky/Alma 9) |
| `export_old_server.sh` + `counts.sql` | 기존 서버(WSL) netbox-docker 폴더 | DB·업로드 파일·설정을 tar 하나로 (`--final` = 기존 서버 정지 유지) |
| `restore_new_server.sh` | 새 서버 /opt/netbox-docker | 묶음(또는 백업 폴더) 복원 → 빌드 → 기동 → 건수 비교 |
| `docker-compose.override.yml` | 새 서버 | 운영용: 8000 포트 127.0.0.1 전용, 자동 재시작 |
| `nginx-netbox.conf` | 새 서버 /etc/nginx/conf.d/ | HTTPS 프록시 |
| `backup.sh` | 새 서버 cron | 매일 DB·파일·설정 백업, 14일 보관 |
| `post_check.sh` | 새 서버 | 이관 후 점검 (컨테이너·마이그레이션·포트·메일·백업) |
