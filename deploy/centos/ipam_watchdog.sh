#!/bin/bash
# =====================================================================================
# IPAM 자동 복구(워치독) — cron 이 5분마다 실행 (설치: sudo bash install_watchdog.sh)
#   1) NetBox 컨테이너(netbox·netbox-worker·postgres·redis)가 꺼져 있으면 다시 켬
#   2) 플러그인 자동 작업 점검 (netbox_ip_request.jobs.watchdog_check)
#      - 예약이 끊김(대기열 비워짐 등)        → 정리 후 다시 예약
#      - 수집 작업이 한도 시간을 넘겨 멈춤    → netbox-worker 재시작 (시작할 때 끊긴 작업 정리·재예약)
#   3) 재시작은 1시간에 3번까지 — 넘으면 멈추고 로그에 '수동 확인 필요'
# 로그: /var/log/ipam-watchdog.log (정상일 때는 남기지 않음, VERBOSE=1 이면 남김)
# =====================================================================================
set -uo pipefail
NBD=${NBD:-/opt/netbox-docker}
STATE=${STATE:-/var/lib/ipam-watchdog}
MAX_RESTARTS=${MAX_RESTARTS:-3}
mkdir -p "$STATE"
exec 9>"$STATE/lock"
flock -n 9 || exit 0                      # 앞 실행이 아직 안 끝났으면 건너뜀
ts() { date '+%F %T'; }
cd "$NBD" || { echo "$(ts) $NBD 없음"; exit 1; }

# 1) 컨테이너
running=$(docker compose ps --status running --services 2>/dev/null)
for s in postgres redis netbox netbox-worker; do
  if ! grep -qx "$s" <<< "$running"; then
    echo "$(ts) [복구] $s 꺼져 있음 → docker compose up -d $s"
    docker compose up -d "$s" >/dev/null 2>&1 9>&-
  fi
done

# 2) 자동 작업 점검
out=$(timeout 180 docker compose exec -T netbox /opt/netbox/venv/bin/python /opt/netbox/netbox/manage.py shell -c \
  "from netbox_ip_request.jobs import watchdog_check as w; r = w(); print('WD|' + r['action'] + '|' + r['reason'])" \
  2>/dev/null | grep '^WD|' | tail -1)
if [ -z "$out" ]; then
  echo "$(ts) [경고] 점검 실패 — netbox 컨테이너가 응답하지 않음 (docker compose ps / logs netbox 확인)"
  exit 1
fi
action=$(cut -d'|' -f2 <<< "$out"); reason=$(cut -d'|' -f3- <<< "$out")
case "$action" in
  ok)
    [ -n "${VERBOSE:-}" ] && echo "$(ts) 정상"
    ;;
  requeued)
    echo "$(ts) [복구] $reason"
    ;;
  restart)
    now=$(date +%s)
    touch "$STATE/restarts"
    awk -v n="$now" '$1 > n - 3600' "$STATE/restarts" > "$STATE/restarts.tmp"; mv "$STATE/restarts.tmp" "$STATE/restarts"
    if [ "$(wc -l < "$STATE/restarts")" -ge "$MAX_RESTARTS" ]; then
      echo "$(ts) [중지] 1시간에 작업자 재시작 ${MAX_RESTARTS}번 초과 — 수동 확인 필요: $reason"
      echo "        확인: cd $NBD && docker compose logs --tail 100 netbox-worker"
      exit 1
    fi
    echo "$now" >> "$STATE/restarts"
    echo "$(ts) [복구] $reason → netbox-worker 재시작"
    docker compose restart netbox-worker >/dev/null 2>&1 9>&-
    ;;
  *)
    echo "$(ts) [경고] 알 수 없는 점검 결과: $out"
    ;;
esac
