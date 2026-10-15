#!/bin/bash
# =====================================================================================
# IPAM 서버 상태 진단 (읽기 전용, 모든 단계 시간 제한 — 이 스크립트 자체는 멈추지 않음)
#   sudo bash /opt/test-ipam/deploy/centos/ipam_diag.sh
# 'docker compose exec 가 멈춤', 'update_plugin.sh 가 멈춤', '수집이 계속 실패' 일 때 어디가 막혔는지 단계별로 확인
# 결과: 화면 + /root/ipam_diag_<시각>.txt  (이 파일 내용을 그대로 붙여 주면 원인 분석 가능, 비밀번호는 출력 안 함)
# =====================================================================================
set -uo pipefail
[ "$(id -u)" = 0 ] || { echo "sudo 로 실행하세요"; exit 1; }
NBD=${NBD:-/opt/netbox-docker}
OUT=/root/ipam_diag_$(date +%Y%m%d-%H%M%S).txt
exec > >(tee "$OUT") 2>&1
cd "$NBD" || { echo "$NBD 없음"; exit 1; }
T() { timeout -k 5 "$@"; }
sec() { echo; echo "== $*"; }
probe() {  # probe <설명> <초> <명령...> — 걸린 시간과 결과
  local what=$1 lim=$2; shift 2
  local t0 out rc
  t0=$(date +%s%N)
  out=$(T "$lim" "$@" 2>&1); rc=$?
  local ms=$(( ($(date +%s%N) - t0) / 1000000 ))
  printf "  %-34s %3d.%d초  " "$what" $((ms / 1000)) $((ms % 1000 / 100))
  LAST=$rc
  if [ $rc = 124 ] || [ $rc = 137 ]; then echo "※ ${lim}초 안에 응답 없음(멈춤)"
  elif [ $rc != 0 ]; then echo "※ 오류(코드 $rc): $(tail -1 <<< "$out" | cut -c1-120)"
  else echo "정상 $(tail -1 <<< "$out" | cut -c1-80)"; fi
}

sec "서버"
uptime
free -h | sed 's/^/  /'
df -h / /var/lib/docker 2>/dev/null | sed 's/^/  /'
echo "  CPU 많이 쓰는 프로세스:"
ps -eo pid,pcpu,pmem,etime,comm --sort=-pcpu | head -6 | sed 's/^/    /'
echo "  남아 있는 빌드·업데이트 프로세스:"
ps -eo pid,etime,args | grep -E "[d]ocker( |-)compose (build|exec)|[b]uildkit|[u]pdate_plugin" | cut -c1-150 | sed 's/^/    /' || true

sec "docker 단계별 응답 (어디서 멈추는지)"
probe "docker 데몬(docker info)"            20 docker info --format '{{.ServerVersion}}'
probe "컨테이너 목록(compose ps)"           30 docker compose ps -q
probe "netbox 파일 읽기(cp, exec 아님)"     30 sh -c "docker compose cp netbox:/etc/hostname - | tar -xO"
probe "netbox exec — 단순 명령(echo)"       30 docker compose exec -T netbox echo ok
EXEC_OK=$LAST
if [ "$EXEC_OK" = 0 ]; then
probe "netbox exec — 파이썬 시작"           60 docker compose exec -T netbox /opt/netbox/venv/bin/python -c "print('ok')"
probe "netbox exec — NetBox(Django) 시작"   120 docker compose exec -T netbox /opt/netbox/venv/bin/python /opt/netbox/netbox/manage.py shell -c "print('ok')"
else echo "  (exec 자체가 안 되므로 파이썬·NetBox 시작 확인은 건너뜀 → 컨테이너 또는 docker 데몬 문제)"; fi
probe "postgres 응답"                       30 docker compose exec -T postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc "select 1"'
PG_OK=$LAST
probe "redis 응답"                          30 docker compose exec -T redis sh -c 'redis-cli --no-auth-warning -a "${REDIS_PASSWORD:-}" ping'

sec "컨테이너 상태"
T 30 docker compose ps --format 'table {{.Service}}\t{{.State}}\t{{.Status}}' 2>&1 | sed 's/^/  /'
T 30 docker stats --no-stream --format 'table {{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}' 2>&1 | sed 's/^/  /'

if [ "$PG_OK" = 0 ]; then
sec "DB — 30초 넘게 실행 중인 쿼리·잠금 대기 (수집 작업이 DB 를 붙잡고 있는지)"
T 30 docker compose exec -T postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -P pager=off -c "
select pid, state, wait_event_type as wait, extract(epoch from now()-query_start)::int as secs,
       left(replace(replace(query, chr(10), chr(32)), chr(13), chr(32)), 90) as query
  from pg_stat_activity
 where datname = current_database() and state <> '"'"'idle'"'"' and pid <> pg_backend_pid()
   and now()-query_start > interval '"'"'30 seconds'"'"'
 order by secs desc limit 15;"' 2>&1 | sed 's/^/  /'
T 30 docker compose exec -T postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -P pager=off -Atc "
select '"'"'관측 표 행 수: ARP '"'"' || (select count(*) from netbox_ip_request_arpentry) ||
       '"'"' · MAC '"'"' || (select count(*) from netbox_ip_request_macentry) ||
       '"'"' · IP '"'"' || (select count(*) from ipam_ipaddress);"' 2>&1 | sed 's/^/  /'

sec "최근 수집 작업 (DB 에서 직접)"
T 30 docker compose exec -T postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -P pager=off -c "
select id, status, to_char(started at time zone '"'"'Asia/Seoul'"'"','"'"'MM-DD HH24:MI'"'"') as started,
       extract(epoch from coalesce(completed, now())-started)::int as secs, left(coalesce(error,'"'"''"'"'), 70) as error
  from core_job where name = '"'"'SNMP ARP/MAC 자동 수집'"'"' order by created desc limit 8;"' 2>&1 | sed 's/^/  /'

else echo; echo "== DB 확인 건너뜀 (postgres 응답 없음)"; fi

sec "netbox-worker 로그 (최근 25줄)"
T 30 docker compose logs --no-color --tail 25 netbox-worker 2>&1 | cut -c1-220 | sed 's/^/  /'
sec "netbox 로그 중 오류 (최근 300줄에서)"
T 30 docker compose logs --no-color --tail 300 netbox 2>&1 | grep -iE "error|exception|timeout|killed" | tail -10 | cut -c1-220 | sed 's/^/  /'
echo; echo "결과 파일: $OUT"
