#!/bin/bash
# =====================================================================================
# 플러그인 업데이트 (운영 서버) — /opt/test-ipam 의 새 코드를 NetBox 에 적용 → 자동 점검 설치 → 진단
#   sudo bash /opt/test-ipam/deploy/centos/update_plugin.sh            # 자동: 인터넷(PyPI) 되면 build, 안 되면 quick
#   sudo bash /opt/test-ipam/deploy/centos/update_plugin.sh quick      # 빠른 적용(1~2분, 인터넷 불필요)
#   sudo bash /opt/test-ipam/deploy/centos/update_plugin.sh build      # 이미지 다시 빌드(수 분~수십 분, PyPI 필요)
#   sudo bash /opt/test-ipam/deploy/centos/update_plugin.sh rollback   # 직전 코드로 되돌림(quick 방식)
# quick : 실행 중인 netbox·netbox-worker 컨테이너에 플러그인 파이썬 코드만 복사 → DB 변경(migrate) → 재시작
#         빌드·다운로드가 없어 멈출 일이 없음. 단, 'docker compose up' 으로 컨테이너를 새로 만들면 예전 이미지
#         코드로 돌아가므로 여유 있을 때 build 로 이미지에도 반영(진단 보고의 '플러그인 x.y.z' 로 확인)
# build : 이미지 다시 빌드(파이썬 패키지 다운로드 포함) → 컨테이너 다시 만듦. 단계마다 시간 제한, 진행 표시,
#         실패·시간 초과면 코드 폴더를 원래대로 되돌리고 멈춤 (실행 중인 서비스는 그대로)
# 모든 출력은 /var/log/ipam-update-<시각>.log 에도 남음. 데이터는 건드리지 않음
# =====================================================================================
set -uo pipefail
[ "$(id -u)" = 0 ] || { echo "sudo 로 실행하세요"; exit 1; }
HERE=$(cd "$(dirname "$0")" && pwd)
SRC=${SRC:-$(cd "$HERE/../.." && pwd)}            # /opt/test-ipam
NBD=${NBD:-/opt/netbox-docker}
DST=$NBD/netbox-ip-request
MODE=${1:-auto}
LOG=/var/log/ipam-update-$(date +%Y%m%d-%H%M%S).log
exec > >(tee -a "$LOG") 2>&1
cd "$NBD" || { echo "$NBD 없음 — NBD=<경로> 지정"; exit 1; }
[ -f docker-compose.yml ] || { echo "$NBD 에 docker-compose.yml 없음"; exit 1; }
step() { echo; echo "== [$(date +%T)] $*"; }
die() { echo "  ※ $*"; echo "  기록: $LOG"; exit 1; }
PYC="/opt/netbox/venv/bin/python /opt/netbox/netbox/manage.py shell -c"
nbver() {
  timeout 60 docker compose exec -T netbox $PYC "import netbox_ip_request as p; print('VER', p.config.version)" \
    2>/dev/null | awk '/^VER/{print $2}'
}
wait_healthy() {
  printf "  netbox 기동 대기"
  local st=''
  for i in $(seq 1 60); do
    st=$(timeout 20 docker compose ps netbox --format '{{.Health}}' 2>/dev/null || true)
    [ "$st" = healthy ] && break
    printf "."; sleep 5
  done
  echo " ${st:-?}"
  [ "$st" = healthy ]
}

new=$(grep -oP "^\s*version = '\K[^']+" "$SRC/netbox_ip_request/__init__.py")
step "점검 — 새 코드 $new ($SRC)"
echo "  실행 중인 플러그인: $(nbver || true)"
free -m | awk '/^Mem/{printf "  메모리 여유 %s MB / 전체 %s MB\n", $7, $2}'
df -Pm /var/lib/docker 2>/dev/null | awk 'NR==2{printf "  디스크 여유(/var/lib/docker) %s MB\n", $4}'
if [ "$MODE" = auto ]; then
  if timeout 10 curl -s -o /dev/null https://pypi.org/simple/ 2>/dev/null; then MODE=build
  else MODE=quick; echo "  인터넷(PyPI) 접속 안 됨 → 빌드 없이 빠른 적용(quick)으로 진행"; fi
fi
echo "  방식: $MODE"

# ---------------------------------------------------------------- quick / rollback
if [ "$MODE" = quick ] || [ "$MODE" = rollback ]; then
  CODE="$SRC/netbox_ip_request"
  if [ "$MODE" = rollback ]; then
    [ -d "$DST.bak/netbox_ip_request" ] || die "직전 코드($DST.bak) 없음"
    CODE="$DST.bak/netbox_ip_request"
  fi
  step "컨테이너 안 플러그인 위치 확인"
  PKG=$(timeout 60 docker compose exec -T netbox /opt/netbox/venv/bin/python -c \
        "import importlib.util as u, os; print(os.path.dirname(u.find_spec('netbox_ip_request').origin))" 2>/dev/null | tail -1)
  [ -n "$PKG" ] || die "netbox 컨테이너 응답 없음 또는 플러그인 위치를 찾지 못함 — docker compose ps / logs netbox 확인"
  echo "  $PKG"
  step "코드 복사 (netbox, netbox-worker)"
  for s in netbox netbox-worker; do
    timeout 120 docker compose cp "$CODE/." "$s:$PKG/" || die "$s 복사 실패"
    timeout 60 docker compose exec -T "$s" find "$PKG" -name '__pycache__' -prune -exec rm -rf {} + 2>/dev/null || true
    echo "  $s 완료"
  done
  step "DB 변경 적용 (migrate — 인덱스 추가, 큰 표면 수 분)"
  timeout 1200 docker compose exec -T netbox /opt/netbox/venv/bin/python /opt/netbox/netbox/manage.py migrate \
    netbox_ip_request --no-input || die "migrate 실패 — 위 오류 확인 (되돌리기: $0 rollback)"
  step "재시작 (netbox·netbox-worker — 웹 화면 1분 정도 끊김)"
  timeout 300 docker compose restart netbox netbox-worker || die "재시작 실패"
  wait_healthy || die "netbox 가 정상 상태가 아님 — docker compose logs --tail 80 netbox (되돌리기: $0 rollback)"
  echo "  실행 중인 플러그인: $(nbver)"
  echo "  ※ 빠른 적용 — 컨테이너를 새로 만들면(up -d·재부팅 시 재생성) 예전 이미지 코드로 돌아감."
  echo "    여유 있을 때 이미지에도 반영: sudo bash $0 build   (진단 보고 첫 줄 '플러그인 $new' 로 확인)"
fi

# ---------------------------------------------------------------- build
if [ "$MODE" = build ]; then
  step "코드 폴더 교체 ($DST, 이전 코드는 $DST.bak)"
  tmp=$(mktemp -d "$NBD/.ipreq.XXXX")
  tar -C "$SRC" --exclude='__pycache__' -cf - pyproject.toml netbox_ip_request scripts \
    $( [ -f "$SRC/README.md" ] && echo README.md ) | tar -C "$tmp" -xf - || die "코드 복사 실패"
  rm -rf "$DST.bak"; [ -d "$DST" ] && mv "$DST" "$DST.bak"
  mv "$tmp" "$DST"; chmod 755 "$DST"
  restore() { rm -rf "$DST"; [ -d "$DST.bak" ] && mv "$DST.bak" "$DST"; echo "  코드 폴더를 원래대로 되돌림 (실행 중인 서비스는 그대로)"; }
  BT=${BUILD_TIMEOUT:-1800}
  step "이미지 빌드 (최대 $((BT / 60))분, 진행 단계 표시 — 서버 부하를 줄이려 낮은 우선순위로 실행)"
  timeout "$BT" nice -n 15 docker compose build --progress=plain netbox 2>&1 \
    | grep --line-buffered -E '^#[0-9]+ \[|ERROR|error:|DONE [0-9.]+s$' | grep --line-buffered -v 'CACHED'
  rc=${PIPESTATUS[0]}
  if [ "$rc" != 0 ]; then
    restore
    [ "$rc" = 124 ] && die "빌드가 $((BT / 60))분 안에 끝나지 않음(대개 패키지 다운로드 막힘) → 빠른 적용: sudo bash $0 quick"
    die "빌드 실패(코드 $rc) — 위 ERROR 줄 확인. 빠른 적용: sudo bash $0 quick"
  fi
  step "적용 (netbox·netbox-worker 다시 만듦 — DB 변경은 netbox 가 켜질 때 자동, 웹 화면 1~3분 끊김)"
  timeout 600 docker compose up -d netbox netbox-worker || die "docker compose up 실패"
  wait_healthy || die "netbox 가 정상 상태가 아님 — docker compose logs --tail 80 netbox (되돌리기: $0 rollback 후 build)"
  echo "  실행 중인 플러그인: $(nbver)"
fi

case "$MODE" in quick|build|rollback) ;; *) die "알 수 없는 방식: $MODE (quick|build|rollback)";; esac
step "자동 점검(ipam-watchdog) 설치·갱신"
NBD=$NBD timeout 300 bash "$HERE/install_watchdog.sh" | tail -3
step "진단 (sudo ipam-watchdog status 와 같음)"
NBD=$NBD timeout 300 /usr/local/sbin/ipam-watchdog status
echo; echo "기록: $LOG"
