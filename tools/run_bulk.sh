#!/bin/bash
# =====================================================================================
# snmp_bulk_config.py 실행 도우미 — 경로·파이썬·이전 결과 파일을 알아서 찾아 실행
#   bash ~/test-ipam/tools/run_bulk.sh [장비목록.xlsx] [snmp_bulk_config.py 옵션...]
# 예)
#   bash ~/test-ipam/tools/run_bulk.sh devices.xlsx --acl-only --force                       # 점검만
#   bash ~/test-ipam/tools/run_bulk.sh devices.xlsx --apply --acl-only --force --retry --limit 3
#   --retry : 가장 최근 result_*.xlsx 에서 실패한 장비만 (= --only-failed <최근 결과 파일>)
#   수집 서버(CentOS)에서 SNMP 응답만 확인 — SSH 계정 불필요, SNMP 계정은 env/ipam-snmp.env 에서 읽음:
#   sudo -E bash ~/test-ipam/tools/run_bulk.sh devices.xlsx --verify-only [--retry]
# 하는 일
#   1) 이 스크립트가 있는 tools 폴더에서 실행 (어디서 불러도 됨)
#   2) 장비 목록·이전 결과 파일을 tools 폴더, 현재 폴더, 예전 위치(~/netbox-docker/netbox-ip-request/tools)에서 찾음
#   3) 파이썬: ~/ipam-tools 가상환경 → 없으면 만들고 필요한 패키지 설치
#   4) 비어 있는 환경변수(SSH·SNMP 계정, 수집 서버 IP)는 물어봄 — 비밀번호는 화면에 안 보임, 파일에 안 남음
# =====================================================================================
set -uo pipefail
TOOLS=$(cd "$(dirname "$0")" && pwd)
START=$PWD
OLD=~/netbox-docker/netbox-ip-request/tools
VENV=${IPAM_VENV:-~/ipam-tools}
VENV=${VENV/#\~/$HOME}

find_file() {   # 이름 또는 패턴 → 첫 번째로 있는 경로
  for d in "$START" "$TOOLS" "$OLD"; do
    for f in $d/$1; do [ -f "$f" ] && { echo "$f"; return 0; }; done
  done
  return 1
}

# ---- 1) 파이썬
PY=${IPAM_PY:-$VENV/bin/python}
if ! "$PY" -c 'import netmiko, pandas, openpyxl, pysnmp' 2>/dev/null; then
  echo "[준비] 파이썬 가상환경 $VENV 에 필요한 패키지 설치 (처음 한 번, 1~2분)"
  [ -x "$VENV/bin/python" ] || python3 -m venv "$VENV" || { echo "python3 -m venv 실패 — 'sudo dnf install python3' 또는 'sudo apt install python3-venv' 후 다시"; exit 1; }
  PY=$VENV/bin/python
  "$PY" -m pip install -q --disable-pip-version-check --upgrade pip
  "$PY" -m pip install -q --disable-pip-version-check pandas openpyxl netmiko 'paramiko<4' 'pysnmp>=7' || { echo "패키지 설치 실패 — 인터넷(프록시) 확인"; exit 1; }
fi
"$PY" -c 'import paramiko,sys; sys.exit(int(paramiko.__version__.split(".")[0]) >= 4)' 2>/dev/null \
  || { echo "[준비] 구형 장비 SSH 를 위해 paramiko 3.x 로 맞춤"; "$PY" -m pip install -q --disable-pip-version-check 'paramiko<4'; }

# ---- 2) 인자: 첫 인자가 파일이면 장비 목록
ARGS=()
INV=''
if [ $# -ge 1 ] && [[ "$1" != --* ]]; then INV=$1; shift; fi
RETRY=0
for a in "$@"; do
  if [ "$a" = --retry ]; then RETRY=1; else ARGS+=("$a"); fi
done
if [[ " ${ARGS[*]-} " != *" --from-netbox "* ]]; then
  INV=${INV:-devices.xlsx}
  P=$(find_file "$INV") || { echo "장비 목록 '$INV' 를 찾지 못함 — 찾아본 곳: $START, $TOOLS, $OLD"; exit 1; }
  if [ "$(dirname "$P")" != "$TOOLS" ] && [ ! -e "$TOOLS/$(basename "$P")" ]; then
    cp "$P" "$TOOLS/" && echo "[준비] 장비 목록 복사: $P → $TOOLS/"
  fi
  ARGS=("$(basename "$P")" "${ARGS[@]+"${ARGS[@]}"}")
fi
if [ $RETRY = 1 ]; then
  LAST=$(ls -t "$TOOLS"/result_*.xlsx "$OLD"/result_*.xlsx "$START"/result_*.xlsx 2>/dev/null | head -1)
  [ -n "$LAST" ] || { echo "--retry: 이전 결과 파일(result_*.xlsx)이 없습니다. 처음 실행이면 --retry 빼고 실행"; exit 1; }
  [ "$(dirname "$LAST")" = "$TOOLS" ] || [ -e "$TOOLS/$(basename "$LAST")" ] || cp "$LAST" "$TOOLS/"
  echo "[준비] 실패분만 다시: $(basename "$LAST")"
  ARGS+=(--only-failed "$(basename "$LAST")")
fi

# ---- 3) 환경변수 (없으면 물어봄)
ask() {  # 이름 설명 숨김여부
  if [ -z "${!1:-}" ]; then
    if [ "$3" = 1 ]; then read -r -s -p "$2: " v; echo; else read -r -p "$2: " v; fi
    export "$1=$v"
  fi
}
VERIFY=0
[[ " ${ARGS[*]-} " == *" --verify-only "* ]] && VERIFY=1
# 수집 서버에서 실행하면 SNMP 계정은 서버 설정 파일에서 읽음 (화면·파일에 비밀번호를 다시 적지 않음)
ENVF=${IPAM_ENV_FILE:-/opt/netbox-docker/env/ipam-snmp.env}
if [ -z "${IPAM_SNMP_AUTH:-}" ] && [ -r "$ENVF" ]; then
  for k in IPAM_SNMP_USER IPAM_SNMP_AUTH IPAM_SNMP_PRIV; do
    v=$(grep -E "^$k=" "$ENVF" | tail -1 | cut -d= -f2-)
    [ -n "$v" ] && export "$k=$v"
  done
  echo "[준비] SNMP 계정은 $ENVF 에서 읽음 (계정 이름: ${IPAM_SNMP_USER:-?})"
elif [ -z "${IPAM_SNMP_AUTH:-}" ] && [ -e "$ENVF" ]; then
  echo "※ $ENVF 를 읽을 권한이 없음 — 'sudo -E bash $0 …' 로 실행하거나 아래에서 직접 입력"
fi
if [ $VERIFY = 0 ]; then
  ask NET_USER 'SSH 계정(NET_USER)' 0
  ask NET_PASS 'SSH 비밀번호(NET_PASS)' 1
  if [ -z "${NET_SECRET+x}" ]; then
    read -r -s -p 'Cisco enable 비밀번호(NET_SECRET, 없거나 로그인 비밀번호와 같으면 Enter): ' NET_SECRET; echo; export NET_SECRET
  fi
  ask COLLECTOR_IPS '수집 서버 IP(COLLECTOR_IPS, 쉼표로 여러 개)' 0
fi
if [ -z "${IPAM_SNMP_AUTH:-}" ]; then
  echo "SNMPv3 계정 — 장비에 계정이 없을 때 만들고, 적용 후 응답을 확인하는 데 씀 (모르면 Enter 로 건너뜀)"
  ask IPAM_SNMP_USER 'SNMP 계정 이름(IPAM_SNMP_USER, 기본 ipam-ro)' 0
  [ -n "$IPAM_SNMP_USER" ] || export IPAM_SNMP_USER=ipam-ro
  ask IPAM_SNMP_AUTH 'SNMP auth 비밀번호' 1
  ask IPAM_SNMP_PRIV 'SNMP priv 비밀번호' 1
fi

# ---- 4) 실행
cd "$TOOLS"
echo "[실행] $PY snmp_bulk_config.py ${ARGS[*]}"
exec "$PY" snmp_bulk_config.py "${ARGS[@]}"
