#!/bin/bash
# 워치독 셸(ipam_watchdog.sh) 검증 — 가짜 docker 로 테스트 NetBox 를 운영 서버처럼 흉내 낸다 (테스트 환경 전용)
#   bash tools/verify_watchdog_sh.sh     결과: 화면 + /tmp/claude_watchdog_sh_report.tsv
HERE=$(cd "$(dirname "$0")" && pwd)
WD="$HERE/../deploy/centos/ipam_watchdog.sh"
export FAKE_DIR=$(mktemp -d) STATE=$(mktemp -d) NBD=$(mktemp -d) LOG=/dev/null VERIFY_WAIT=1
export PATH="$HERE/fakedocker:$PATH"
printf 'postgres\nredis\nnetbox\nnetbox-worker\n' > "$FAKE_DIR/running"
P=0; F=0; REP=/tmp/claude_watchdog_sh_report.tsv; printf '항목\t결과\t근거\n' > $REP
ok() { if [ "$1" = 0 ]; then P=$((P+1)); echo "  PASS $2"; printf '%s\tPASS\t%s\n' "$2" "${3:0:200}" >> $REP
       else F=$((F+1)); echo "  FAIL $2  [${3:0:300}]"; printf '%s\tFAIL\t%s\n' "$2" "${3:0:200}" >> $REP; fi; }
fx() { (cd /home/claude/netbox/netbox && PYTHONPATH="$HERE" /home/claude/nbvenv/bin/python manage.py shell -c "import wd_fixture as F; F.$1" 2>/dev/null) | grep -v "objects imported\|^$"; }
wd() { bash "$WD" "$@" 2>&1; }
has() { grep -q -- "$2" <<< "$1"; echo $?; }

# 실제 작업자는 멈춰 두고(가짜 재시작이 '켜질 때 정리'만 흉내) 검증
for p in $(pgrep -f "manage.py rq[w]orker"); do kill "$p"; done

echo "== 1. 정상"
fx 'clean()'; out=$(wd); ok $([ -z "$out" ]; echo $?) '정상이면 아무것도 기록하지 않음' "$out"

echo "== 2. 수집 작업이 93분째 실행 중 (화면의 #5704 상황)"
pk=$(fx 'stuck(93)' | awk '/^JOB/{print $2}')
out=$(wd); ok $(has "$out" "93분째 실행 중") '멈춘 작업 감지' "$out"
ok $(has "$(cat $FAKE_DIR/restarts 2>/dev/null)" "restart netbox-worker") 'netbox-worker 재시작' "$(cat $FAKE_DIR/restarts 2>/dev/null)"
ok $(has "$out" "\[확인\] 재시작 후 정상") '재시작 1분 뒤 복구됐는지 다시 확인해 기록' "$out"
ok $(has "$(fx "status($pk)")" "STATUS errored") '멈춰 있던 작업은 오류로 정리됨(다음 수집 예약 가능)' "$(fx "status($pk)")"

echo "== 3. 수집 3회 연속 오류 (화면의 #5697·5702 시간 초과 상황)"
rm -f $STATE/restarts $FAKE_DIR/restarts; fx 'clean()'; fx 'errors(3)'
out=$(wd); ok $(has "$out" "수집 3회 연속 오류") '연속 오류 감지 → 작업자 재시작' "$out"
ok $(has "$(cat $FAKE_DIR/restarts 2>/dev/null)" "restart netbox-worker") 'netbox-worker 재시작 실행' "$(cat $FAKE_DIR/restarts)"
out=$(wd); ok $(has "$out" "재시작 후에도 계속됨") '같은 오류면 다시 재시작하지 않고 경고만' "$out"
ok $([ "$(grep -c netbox-worker $FAKE_DIR/restarts)" = 1 ]; echo $?) '재시작은 한 번만' "$(cat $FAKE_DIR/restarts)"
out=$(wd); ok $([ -z "$(grep '재시작 후에도' <<< "$out")" ]; echo $?) '같은 경고는 1시간에 한 번만 기록' "$out"
fx 'tidy()'

echo "== 4. 정상 완료지만 RQ 한도(900초)의 80% 를 넘김 → 미리 경고"
fx 'slow(800)'; out=$(wd); ok $(has "$out" "800초 걸림") '느려지는 추세 경고 (단계별 시간 포함)' "$out"
ok $(has "$out" "ports") '경고에 단계별 시간이 있어 느린 단계를 바로 앎' "$out"
fx 'tidy()'

echo "== 5. netbox 컨테이너 응답 없음"
rm -f $STATE/restarts $FAKE_DIR/restarts; touch $FAKE_DIR/exec_fail
out=$(wd); ok $(has "$out" "점검 실패 1회째") '1회째는 경고만' "$out"
out=$(wd); ok $(has "$out" "2회 연속 응답 없음") '2회 연속이면 재시작' "$out"
ok $(has "$(cat $FAKE_DIR/restarts)" "restart netbox") 'netbox·netbox-worker 재시작' "$(cat $FAKE_DIR/restarts)"
rm -f $FAKE_DIR/exec_fail

echo "== 6. 컨테이너 꺼짐"
printf 'postgres\nredis\nnetbox\n' > "$FAKE_DIR/running"
out=$(wd); ok $(has "$out" "netbox-worker 꺼져 있음") '꺼진 netbox-worker 를 켬' "$out"

echo "== 7. 재시작 횟수 제한 (1시간 3번)"
now=$(date +%s); printf '%s\n%s\n%s\n' $now $now $now > $STATE/restarts
fx 'stuck(93)' >/dev/null; out=$(wd); ok $(has "$out" "\[중지\]") '1시간에 3번 넘으면 멈추고 수동 확인 요청' "$out"
rm -f $STATE/restarts

echo "== 8. 진단 보고(status)·즉시 복구(fix)"
out=$(wd status); ok $(has "$out" "== 수집 진단") 'status: 진단 보고 출력' "$out"
ok $(has "$out" "판정:") 'status: 판정 줄' "$out"
ok $(has "$out" "설치 안 됨") 'status: cron 미설치 알려 줌 (테스트 환경엔 설치 안 함)' "$out"
out=$(wd fix); ok $(has "$out" "\[수동 복구\]") 'fix: 정리·재시작 실행' "$out"
ok $(has "$out" "\[확인\]") 'fix: 재시작 후 확인' "$out"
ok $(has "$out" "판정:") 'fix: 끝에 진단 보고' "$out"

fx 'tidy()'
/home/claude/rq_start.sh
rm -rf "$FAKE_DIR" "$STATE" "$NBD"
echo; echo "결과: PASS $P / FAIL $F"
