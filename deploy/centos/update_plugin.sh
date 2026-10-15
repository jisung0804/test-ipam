#!/bin/bash
# =====================================================================================
# 플러그인 업데이트 (운영 서버) — /opt/test-ipam 의 새 코드로 이미지 다시 빌드 → 적용 → 자동 점검 설치 → 진단
#   sudo bash /opt/test-ipam/deploy/centos/update_plugin.sh            # 업데이트
#   sudo bash /opt/test-ipam/deploy/centos/update_plugin.sh rollback   # 직전 버전으로 되돌림
# 하는 일
#   1) $NBD/netbox-ip-request 를 백업(netbox-ip-request.bak) 하고 /opt/test-ipam 의 플러그인 코드로 바꿈
#   2) docker compose build netbox → up -d netbox netbox-worker (DB 변경(migration)은 netbox 가 켜질 때 자동 적용)
#   3) netbox 가 정상(healthy)이 될 때까지 기다린 뒤 플러그인 버전 확인
#   4) 자동 점검(ipam-watchdog, 5분마다) 설치·갱신 → 진단 보고 출력
# 데이터는 건드리지 않음. 실패하면 'rollback' 으로 직전 코드로 되돌릴 수 있음
# =====================================================================================
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "sudo 로 실행하세요"; exit 1; }
HERE=$(cd "$(dirname "$0")" && pwd)
SRC=${SRC:-$(cd "$HERE/../.." && pwd)}            # /opt/test-ipam
NBD=${NBD:-/opt/netbox-docker}
DST=$NBD/netbox-ip-request
cd "$NBD"
[ -f docker-compose.yml ] || { echo "$NBD 에 docker-compose.yml 없음 — NBD=<경로> 지정"; exit 1; }

nbver() {
  timeout 120 docker compose exec -T netbox /opt/netbox/venv/bin/python /opt/netbox/netbox/manage.py shell -c \
    "import netbox_ip_request as p; print('VER', p.config.version)" 2>/dev/null | awk '/^VER/{print $2}'
}

if [ "${1:-}" = rollback ]; then
  [ -d "$DST.bak" ] || { echo "백업($DST.bak) 없음"; exit 1; }
  rm -rf "$DST.failed"; mv "$DST" "$DST.failed"; mv "$DST.bak" "$DST"
  echo "== 직전 코드로 되돌림 → 다시 빌드"
else
  new=$(grep -oP "version = '\K[^']+" "$SRC/netbox_ip_request/__init__.py")
  echo "== 현재 실행 중: $(nbver || echo '?')  →  새 코드: $new  ($SRC)"
  tmp=$(mktemp -d "$NBD/.ipreq.XXXX")
  tar -C "$SRC" --exclude='__pycache__' -cf - pyproject.toml netbox_ip_request scripts \
    $( [ -f "$SRC/README.md" ] && echo README.md ) | tar -C "$tmp" -xf -
  rm -rf "$DST.bak"; [ -d "$DST" ] && mv "$DST" "$DST.bak"
  mv "$tmp" "$DST"; chmod 755 "$DST"
  echo "  코드 교체 완료 (이전 코드: $DST.bak)"
fi

echo "== 이미지 빌드 (수 분)"
docker compose build netbox 2>&1 | tail -3
echo "== 적용 (netbox·netbox-worker 다시 만듦 — 웹 화면 1~2분 끊김)"
docker compose up -d netbox netbox-worker 2>&1 | tail -3
printf "  netbox 기동 대기"
for i in $(seq 1 60); do
  st=$(docker compose ps netbox --format '{{.Health}}' 2>/dev/null || true)
  [ "$st" = healthy ] && break
  printf "."; sleep 5
done
echo " $st"
[ "$st" = healthy ] || { echo "  ※ netbox 가 정상 상태가 아님 — docker compose logs --tail 80 netbox 확인 (되돌리기: $0 rollback)"; exit 1; }
echo "  실행 중인 플러그인 버전: $(nbver)"

echo "== 자동 점검(ipam-watchdog) 설치·갱신"
NBD=$NBD bash "$HERE/install_watchdog.sh" | tail -3
echo
echo "== 진단 (sudo ipam-watchdog status 와 같음)"
NBD=$NBD /usr/local/sbin/ipam-watchdog status
