#!/bin/bash
# =====================================================================================
# IPAM 자동 점검·복구(워치독) — cron 이 5분마다 실행 (설치: sudo bash install_watchdog.sh)
#   ipam-watchdog            점검 + 자동 복구 (cron 이 부름)
#   ipam-watchdog status     자체 진단 보고 — 컨테이너·cron·최근 수집(단계별 시간)·판정 (바꾸는 것 없음)
#   ipam-watchdog fix        지금 바로 복구 — 끊긴 작업 정리 → netbox-worker 재시작 → 다시 예약 → 진단 보고
# 점검·복구 내용 (5분마다)
#   1) 컨테이너(postgres·redis·netbox·netbox-worker)가 꺼져 있으면 켬
#   2) netbox 컨테이너가 2회 연속 응답 없음        → netbox·netbox-worker 재시작
#   3) 수집 작업이 한도 시간을 넘겨 멈춤            → netbox-worker 재시작 (시작할 때 끊긴 작업 정리·재예약)
#   4) 수집 3회 연속 오류(시간 초과 등)             → netbox-worker 재시작 (같은 오류로 반복하지 않음)
#   5) 예약이 끊김(대기열 비워짐 등)                → 정리 후 다시 예약
#   6) 재시작 후 1분 뒤 복구됐는지 다시 확인해 로그에 남김
#   재시작은 1시간에 3번까지 — 넘으면 멈추고 로그에 '수동 확인 필요'
# 로그: /var/log/ipam-watchdog.log (정상일 때는 남기지 않음, VERBOSE=1 이면 남김)
# =====================================================================================
set -uo pipefail
NBD=${NBD:-/opt/netbox-docker}
STATE=${STATE:-/var/lib/ipam-watchdog}
MAX_RESTARTS=${MAX_RESTARTS:-3}
LOG=${LOG:-/var/log/ipam-watchdog.log}
MODE=${1:-check}
mkdir -p "$STATE"
ts() { date '+%F %T'; }
cd "$NBD" || { echo "$(ts) $NBD 없음"; exit 1; }
PY="/opt/netbox/venv/bin/python /opt/netbox/netbox/manage.py shell -c"
nbpy() {  # netbox 컨테이너 안에서 파이썬 한 줄 실행 (최대 $2초)
  timeout "${2:-180}" docker compose exec -T netbox $PY "$1" 2>/dev/null 9>&-
}

health() {
  nbpy "from netbox_ip_request.jobs import health_report as h; print('\n'.join(h()))" 240 \
    | grep -v 'objects imported automatically' | sed '/./,$!d'
}

if [ "$MODE" = status ]; then
  echo "== 컨테이너"
  docker compose ps --format 'table {{.Service}}\t{{.State}}\t{{.Status}}' 2>/dev/null | sed 's/^/  /'
  echo "== 자동 점검(cron)"
  if [ -f /etc/cron.d/ipam-watchdog ]; then
    echo "  설치됨: $(grep -v '^#' /etc/cron.d/ipam-watchdog | grep ipam-watchdog)"
    systemctl is-active --quiet crond 2>/dev/null && echo "  crond 실행 중" || echo "  ※ crond 꺼져 있음 → sudo systemctl enable --now crond"
  else
    echo "  ※ 설치 안 됨 → sudo bash /opt/test-ipam/deploy/centos/install_watchdog.sh"
  fi
  echo "  최근 기록 ($LOG):"
  tail -n 8 "$LOG" 2>/dev/null | sed 's/^/    /' || echo "    (없음)"
  echo "== 수집 진단"
  out=$(health)
  [ -n "$out" ] && sed 's/^/  /' <<< "$out" || echo "  ※ netbox 컨테이너가 응답하지 않음 — docker compose logs --tail 50 netbox"
  exit 0
fi

exec 9>"$STATE/lock"
flock -n 9 || exit 0                      # 앞 실행이 아직 안 끝났으면 건너뜀

restart() {  # restart <이유> <서비스...>  — 1시간 MAX_RESTARTS 번 제한
  local why=$1; shift
  local now; now=$(date +%s)
  touch "$STATE/restarts"
  awk -v n="$now" '$1 > n - 3600' "$STATE/restarts" > "$STATE/restarts.tmp"; mv "$STATE/restarts.tmp" "$STATE/restarts"
  if [ "$MODE" != fix ] && [ "$(wc -l < "$STATE/restarts")" -ge "$MAX_RESTARTS" ]; then
    echo "$(ts) [중지] 1시간에 재시작 ${MAX_RESTARTS}번 초과 — 수동 확인 필요: $why"
    echo "        확인: sudo ipam-watchdog status   /   cd $NBD && docker compose logs --tail 100 netbox-worker"
    return 1
  fi
  echo "$now" >> "$STATE/restarts"
  echo "$(ts) [복구] $why → $* 재시작"
  docker compose restart "$@" >/dev/null 2>&1 9>&-
  touch "$STATE/verify"                   # 다음 단계에서 복구 확인
  return 0
}

verify_after_restart() {  # 재시작 뒤 실제로 풀렸는지 확인 (작업자 기동 대기 후)
  sleep "${VERIFY_WAIT:-60}"
  local r
  r=$(nbpy "from netbox_ip_request.jobs import watchdog_check as w; r = w(); print('WD|' + r['action'] + '|' + r['reason'])" \
      | grep '^WD|' | tail -1)
  case "$(cut -d'|' -f2 <<< "$r")" in
    ok|requeued) echo "$(ts) [확인] 재시작 후 정상 — $(cut -d'|' -f3- <<< "$r")" ;;
    restart)     echo "$(ts) [확인] 재시작 후에도 이상 — $(cut -d'|' -f3- <<< "$r") (다음 점검에서 다시 처리)" ;;
    *)           echo "$(ts) [확인] 재시작 후 netbox 응답 없음 — 다음 점검에서 다시 확인" ;;
  esac
  rm -f "$STATE/verify"
}

# 1) 컨테이너
running=$(docker compose ps --status running --services 2>/dev/null)
for s in postgres redis netbox netbox-worker; do
  if ! grep -qx "$s" <<< "$running"; then
    echo "$(ts) [복구] $s 꺼져 있음 → docker compose up -d $s"
    docker compose up -d "$s" >/dev/null 2>&1 9>&-
  fi
done

if [ "$MODE" = fix ]; then
  echo "$(ts) [수동 복구] 끊긴 작업 정리 → netbox-worker 재시작"
  nbpy "from netbox_ip_request.jobs import clear_stale_jobs as c; print('정리', c(on_start=True), '건')" | tail -1
  restart "수동 복구(ipam-watchdog fix)" netbox-worker
  verify_after_restart
  echo "== 진단"; health | sed 's/^/  /'
  exit 0
fi

# 2) 자동 작업 점검
raw=$(nbpy "from netbox_ip_request.jobs import watchdog_check as w
r = w()
print('WD|' + r['action'] + '|' + r['reason'])
[print('WARN|' + x) for x in r.get('warn', [])]")
out=$(grep '^WD|' <<< "$raw" | tail -1)
if [ -z "$out" ]; then
  n=$(( $(cat "$STATE/exec_fail" 2>/dev/null || echo 0) + 1 )); echo "$n" > "$STATE/exec_fail"
  echo "$(ts) [경고] 점검 실패 ${n}회째 — netbox 컨테이너가 응답하지 않음"
  if [ "$n" -ge 2 ]; then
    restart "netbox 컨테이너 ${n}회 연속 응답 없음" netbox netbox-worker && echo 0 > "$STATE/exec_fail"
  fi
  exit 1
fi
echo 0 > "$STATE/exec_fail"
grep '^WARN|' <<< "$raw" | cut -d'|' -f2- | while read -r w; do
  # 같은 경고는 1시간에 한 번만 기록
  key=$(md5sum <<< "$w" | cut -c1-12); f="$STATE/warn_$key"
  if [ ! -f "$f" ] || [ $(( $(date +%s) - $(stat -c %Y "$f") )) -ge 3600 ]; then
    echo "$(ts) [경고] $w"; touch "$f"
  fi
done
action=$(cut -d'|' -f2 <<< "$out"); reason=$(cut -d'|' -f3- <<< "$out")
case "$action" in
  ok)
    [ -n "${VERBOSE:-}" ] && echo "$(ts) 정상"
    ;;
  requeued)
    echo "$(ts) [복구] $reason"
    ;;
  restart)
    restart "$reason" netbox-worker && verify_after_restart
    ;;
  *)
    echo "$(ts) [경고] 알 수 없는 점검 결과: $out"
    ;;
esac
exit 0
