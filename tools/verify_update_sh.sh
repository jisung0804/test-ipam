#!/bin/bash
# update_plugin.sh 검증 — 가짜 docker 로 운영 서버 흉내 (테스트 환경 전용). 결과: 화면 + /tmp/claude_update_sh_report.tsv
HERE=$(cd "$(dirname "$0")" && pwd)
UP="$HERE/../deploy/centos/update_plugin.sh"
export FAKE_DIR=$(mktemp -d) NBD=$(mktemp -d) VERIFY_WAIT=1
export PATH="$HERE/fakedocker:$PATH"
printf 'postgres\nredis\nnetbox\nnetbox-worker\n' > "$FAKE_DIR/running"; touch "$NBD/docker-compose.yml"
P=0; F=0; REP=/tmp/claude_update_sh_report.tsv; printf '항목\t결과\t근거\n' > $REP
ok() { if [ "$1" = 0 ]; then P=$((P+1)); echo "  PASS $2"; printf '%s\tPASS\t%s\n' "$2" "${3:0:200}" >> $REP
       else F=$((F+1)); echo "  FAIL $2  [${3:0:400}]"; printf '%s\tFAIL\t%s\n' "$2" "${3:0:200}" >> $REP; fi; }
has() { grep -q -- "$2" <<< "$1"; echo $?; }
old() { rm -rf "$NBD"/netbox-ip-request*; mkdir -p "$NBD/netbox-ip-request"; echo old > "$NBD/netbox-ip-request/marker"; : > "$FAKE_DIR/restarts"; }

echo "== 1. quick — 빌드 없이 컨테이너에 코드 복사"
old; out=$(timeout 300 bash "$UP" quick 2>&1); rc=$?
ok $rc 'quick 정상 종료' "$(tail -5 <<< "$out")"
ok $(has "$(cat $FAKE_DIR/restarts)" "cp netbox-worker") 'netbox·netbox-worker 둘 다 복사' "$(cat $FAKE_DIR/restarts)"
ok $([ -f "$FAKE_DIR/cp_netbox/jobs.py" ]; echo $?) '플러그인 파일이 컨테이너 위치로 복사됨' "$(ls $FAKE_DIR/cp_netbox | head -3)"
ok $(tr '\n' ' ' < $FAKE_DIR/restarts | grep -q "stop netbox-worker.*restart netbox.*start netbox-worker"; echo $?) \
   '작업자 정지 → netbox 재시작(켜질 때 migrate) → 작업자 시작 순서' "$(tr '\n' ' ' < $FAKE_DIR/restarts)"
ok $([ -z "$(grep -E '^exec' $FAKE_DIR/calls | grep -v 'shell -c')" ]; echo $?) 'quick 적용에 exec(컨테이너 안 실행)를 쓰지 않음' "$(grep -E '^compose exec' $FAKE_DIR/calls | head -3)"
ok $(has "$out" "실행 중인 플러그인: 0.6.5") '적용 후 버전 확인' "$(grep '플러그인' <<< "$out")"
ok $(has "$out" "판정:") '끝에 진단 보고' ""
ok $(has "$(cat $NBD/netbox-ip-request/marker)" old) 'quick 은 빌드용 코드 폴더를 건드리지 않음' ""
ok $([ -n "$(ls /var/log/ipam-update-*.log 2>/dev/null)" ]; echo $?) '실행 기록 파일 남김' ""

echo "== 1b. 운영 서버 증상 — exec 가 멈추고 종료 신호도 무시"
old; touch $FAKE_DIR/exec_hang; t=$(date +%s)
out=$(WD_EXEC_TIMEOUT=5 timeout 600 bash "$UP" quick 2>&1); rc=$?; el=$(( $(date +%s) - t ))
rm -f $FAKE_DIR/exec_hang
ok $([ $rc = 0 ] && [ $el -lt 120 ]; echo $?) "exec 가 멈춰도 업데이트는 끝까지 진행(${el}초, 무한 대기 없음)" "$(tail -4 <<< "$out")"
ok $(has "$out" "실행 중인 플러그인: 0.6.5") 'exec 없이 버전 확인' "$(grep '플러그인' <<< "$out" | head -3)"
ok $([ -z "$(pgrep -fx 'sleep 600')" ]; echo $?) '멈춘 exec 프로세스가 남지 않음(강제 종료됨)' "$(pgrep -afx 'sleep 600')"

echo "== 2. build 성공"
old; out=$(timeout 300 bash "$UP" build 2>&1); rc=$?
ok $rc 'build 정상 종료' "$(tail -5 <<< "$out")"
ok $(has "$out" "#5 \[2/5\]") '빌드 진행 단계가 실시간 표시됨(멈춘 것처럼 보이지 않음)' "$(grep '#5' <<< "$out")"
ok $([ -f "$NBD/netbox-ip-request.bak/marker" ] && [ -f "$NBD/netbox-ip-request/netbox_ip_request/jobs.py" ]; echo $?) '새 코드로 교체, 이전 코드 .bak 보관' "$(ls $NBD)"

echo "== 3. build 실패 → 코드 폴더 원상복구"
old; out=$(FAKE_BUILD_FAIL=1 timeout 300 bash "$UP" build 2>&1); rc=$?
ok $([ $rc != 0 ]; echo $?) '실패로 종료' "$rc"
ok $(has "$(cat $NBD/netbox-ip-request/marker 2>/dev/null)" old) '코드 폴더를 원래대로 되돌림' "$(ls $NBD)"
ok $(has "$out" "update_plugin.sh quick") '빠른 적용(quick) 안내' "$(tail -3 <<< "$out")"

echo "== 4. build 가 멈춤(다운로드 막힘) → 시간 제한으로 끝냄"
old; t=$(date +%s); out=$(FAKE_BUILD_HANG=1 BUILD_TIMEOUT=5 timeout 300 bash "$UP" build 2>&1); rc=$?
ok $([ $rc != 0 ] && [ $(( $(date +%s) - t )) -lt 25 ]; echo $?) "시간 제한(테스트 5초)에서 끝남 — 무한 대기 없음" "$(( $(date +%s) - t ))초"
ok $(has "$out" "안에 끝나지 않음") '시간 초과 안내' "$(tail -3 <<< "$out")"
ok $(has "$(cat $NBD/netbox-ip-request/marker 2>/dev/null)" old) '코드 폴더 원상복구' ""

echo "== 5. auto — 인터넷(PyPI) 안 되면 quick 으로"
old; out=$(PATH="$HERE/fakenet:$PATH" timeout 300 bash "$UP" 2>&1)
ok $(has "$out" "방식: quick") 'PyPI 접속 안 됨 → quick 자동 선택' "$(grep '방식' <<< "$out")"

echo "== 6. rollback"
old; mkdir -p "$NBD/netbox-ip-request.bak"; cp -r "$HERE/../netbox_ip_request" "$NBD/netbox-ip-request.bak/"; echo bak > "$NBD/netbox-ip-request.bak/netbox_ip_request/ROLLBACK_MARK"
rm -rf "$FAKE_DIR"/cp_*; out=$(timeout 300 bash "$UP" rollback 2>&1); rc=$?
ok $([ $rc = 0 ] && [ -f "$FAKE_DIR/cp_netbox/ROLLBACK_MARK" ]; echo $?) '직전 코드(.bak)를 컨테이너에 복사' "$(tail -3 <<< "$out")"

echo "== 7. 워치독을 sudo 없이 실행"
out=$(su nobody -s /bin/bash -c "bash $HERE/../deploy/centos/ipam_watchdog.sh" 2>&1)
ok $(has "$out" "root 권한이 필요합니다") '권한 오류 대신 sudo 안내' "$out"

bash "$HERE/../deploy/centos/install_watchdog.sh" remove >/dev/null; rm -f /var/log/ipam-update-*.log /var/log/ipam-watchdog.log
rm -rf "$FAKE_DIR" "$NBD"
echo; echo "결과: PASS $P / FAIL $F"
