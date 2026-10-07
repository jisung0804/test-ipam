#!/bin/bash
# =====================================================================================
# 수집 서버 SNMP ACL 일괄 보정 — "서버에서 확인 → 막힌 장비만 ACL 추가 → 서버에서 다시 확인" 을 한 번에
#   (장비에 SSH 로 접속할 수 있는 PC 에서 실행. 수집 서버에는 SSH 로 들어감)
#
#   bash ~/test-ipam/tools/fix_collector_acl.sh <서버계정@서버IP> [장비목록.xlsx] [옵션...]
#   예) bash ~/test-ipam/tools/fix_collector_acl.sh ipam@165.246.12.104 devices.xlsx
#       bash ~/test-ipam/tools/fix_collector_acl.sh ipam@165.246.12.104:2022 devices.xlsx      # SSH 포트가 22 가 아닐 때
#       bash ~/test-ipam/tools/fix_collector_acl.sh ipam@165.246.12.104 devices.xlsx --limit 3     # 시범 3대
#       bash ~/test-ipam/tools/fix_collector_acl.sh ipam@165.246.12.104 devices.xlsx --check-only  # 확인만
#
# 순서
#   1) 서버의 NetBox 컨테이너에서 전체 장비 SNMPv3 응답 확인 (NetBox 장비 등록과 같은 계정·코드·경로)
#   2) 서버가 장비로 보낼 때 쓰는 '출발 IP' 를 알아냄 (ip route get) → ACL 에 넣을 IP
#   3) 응답 없는 장비만 골라 이 PC 에서 SSH 로 ACL 추가 (run_bulk.sh --apply --acl-only --force)
#      - 장비가 실제로 쓰는 ACL(Cisco 계정/그룹 ACL, Juniper lo0 필터, Comware 계정 ACL, ProCurve 허용 관리자)에 추가
#      - 장비에 SNMPv3 계정이 없으면 계정도 추가 (계정 값은 서버 env/ipam-snmp.env 에서 읽음 — 파일로 남기지 않음)
#   4) 서버에서 그 장비들만 다시 확인 → acl_fix_<시각>.xlsx 에 장비별 전/후 결과
# 환경변수(선택): NBD=/opt/netbox-docker  SSH_PORT=22  COLLECTOR_IPS=추가로 허용할 IP(쉼표)
# =====================================================================================
set -uo pipefail
SRV=${1:?사용법: fix_collector_acl.sh <서버계정@서버IP[:SSH포트]> [장비목록.xlsx] [옵션...]}; shift
if [[ "$SRV" =~ ^(.+):([0-9]+)$ ]]; then SRV=${BASH_REMATCH[1]}; SSH_PORT=${BASH_REMATCH[2]}; fi   # 계정@IP:포트
INV=devices.xlsx
if [ $# -ge 1 ] && [[ "$1" != --* ]]; then INV=$1; shift; fi
CHECK_ONLY=0; PASS=()
for a in "$@"; do
  if [ "$a" = --check-only ]; then CHECK_ONLY=1; else PASS+=("$a"); fi
done
TOOLS=$(cd "$(dirname "$0")" && pwd)
REPO=$(dirname "$TOOLS")
NBD=${NBD:-/opt/netbox-docker}
STAMP=$(date +%Y%m%d_%H%M%S)
WORK=$TOOLS/acl_fix_$STAMP
mkdir -p "$WORK"
SOCK=~/.ssh/ipam-acl-%r@%h:%p
mkdir -p ~/.ssh && chmod 700 ~/.ssh
SSH=(ssh -p "${SSH_PORT:-22}" -o ControlMaster=auto -o "ControlPath=$SOCK" -o ControlPersist=15m "$SRV")
trap '"${SSH[@]}" -O exit >/dev/null 2>&1 || true' EXIT

# ---- 준비: 파이썬(run_bulk.sh 와 같은 가상환경), 장비 목록
PY=${IPAM_PY:-$HOME/ipam-tools/bin/python}
if ! "$PY" -c 'import pandas, openpyxl' 2>/dev/null; then
  echo "[준비] 파이썬 환경이 없어 run_bulk.sh 로 먼저 만듭니다"
  bash "$TOOLS/run_bulk.sh" --help >/dev/null 2>&1 || true
  "$PY" -c 'import pandas, openpyxl' 2>/dev/null || { echo "파이썬 환경 준비 실패 — 'bash $TOOLS/run_bulk.sh devices.xlsx' 를 한 번 실행해 보세요"; exit 1; }
fi
for d in "$PWD" "$TOOLS" ~/netbox-docker/netbox-ip-request/tools; do
  [ -f "$d/$INV" ] && { INVP="$d/$INV"; break; }
done
[ -n "${INVP:-}" ] || { echo "장비 목록 '$INV' 를 찾지 못함"; exit 1; }
"$PY" - "$INVP" > "$WORK/ips.txt" <<'EOF' || exit 1
import sys, ipaddress, pandas as pd
p = sys.argv[1]
df = pd.read_csv(p, dtype=str) if p.lower().endswith('.csv') else pd.read_excel(p, dtype=str)
df.columns = [str(c).strip().lower() for c in df.columns]
for x in df['ip'].fillna(''):
    x = str(x).strip().split('/')[0]
    try:
        ipaddress.ip_address(x); print(x)
    except ValueError:
        pass
EOF
N=$(wc -l < "$WORK/ips.txt")
echo "장비 목록: $INVP ($N 대)"
[ "$N" -gt 0 ] || exit 1

# ---- 서버 접속 (비밀번호는 한 번만 물어봄)
echo "[1/4] 수집 서버 $SRV 접속 (SSH 포트 ${SSH_PORT:-22})"
"${SSH[@]}" true || { echo "서버 SSH 접속 실패"; exit 1; }
"${SSH[@]}" "test -d $NBD" || { echo "서버에 $NBD 가 없음 — NBD=<netbox-docker 경로> 로 지정"; exit 1; }

reach() {   # $1 = IP 목록 파일, $2 = 결과 파일(REACH 줄)
  "${SSH[@]}" "cd $NBD && docker compose exec -T -e IPS='$(tr '\n' ' ' < "$1")' netbox \
      /opt/netbox/venv/bin/python /opt/netbox/netbox/manage.py shell" \
      < "$REPO/deploy/centos/snmp_reach.py" 2>/dev/null | grep '^REACH|' > "$2"
  [ -s "$2" ] || { echo "서버 확인 실패 — 서버에서 'docker compose ps' 와 docker 권한(docker 그룹) 확인"; exit 1; }
  if grep -q '^REACH|-|error|' "$2"; then cut -d'|' -f4 "$2"; exit 1; fi
}
summary() { echo "      $(cut -d'|' -f3 "$1" | sort | uniq -c | awk '{printf "%s %s대  ", $2, $1}' \
  | sed 's/ok/응답/;s/timeout/응답없음(ACL·방화벽)/;s/usm/계정없음/;s/auth/비밀번호불일치/;s/error/기타오류/')"; }

echo "[2/4] 서버에서 SNMPv3 응답 확인 ($N 대, 응답 없는 장비가 많으면 몇 분 걸림)"
reach "$WORK/ips.txt" "$WORK/before.txt"
summary "$WORK/before.txt"
grep -v '|ok|' "$WORK/before.txt" | cut -d'|' -f2 > "$WORK/fail_ips.txt"
F=$(wc -l < "$WORK/fail_ips.txt")

# 서버 출발 IP (장비로 갈 때 쓰는 IP) — 실패 장비 기준, 없으면 전체 기준
SRCS=$( (cat "$WORK/fail_ips.txt"; [ "$F" -eq 0 ] && head -20 "$WORK/ips.txt") | head -200 | \
  "${SSH[@]}" 'while read ip; do ip -o route get "$ip" 2>/dev/null | grep -o "src [0-9.]*" | cut -d" " -f2; done' | sort -u | paste -sd, -)
echo "      서버 출발 IP: ${SRCS:-알 수 없음}  (장비 ACL 에 있어야 하는 IP)"
ALL_IPS=$(printf "%s\n" $(echo "${COLLECTOR_IPS:-},${SRCS:-}" | tr "," " ") | grep -E "^[0-9.]+$" | sort -u | paste -sd, -)

if [ "$F" -eq 0 ]; then echo "모든 장비가 수집 서버에 응답합니다. 할 일 없음"; exit 0; fi
if [ $CHECK_ONLY = 1 ]; then
  grep -v '|ok|' "$WORK/before.txt" | cut -d'|' -f2- | tr '|' '\t' > "$WORK/fail.tsv"
  echo "확인만 함 — 응답 없는 장비 $F 대: $WORK/fail.tsv"; exit 0
fi
[ -n "$ALL_IPS" ] || { echo "서버 출발 IP 를 알아내지 못함 — COLLECTOR_IPS=<서버IP> 로 지정 후 다시"; exit 1; }

# ---- 실패 장비만 목록 만들기 (vendor 칸 유지)
"$PY" - "$INVP" "$WORK/fail_ips.txt" "$WORK/retry.xlsx" <<'EOF' || exit 1
import sys, pandas as pd
p, fl, out = sys.argv[1:4]
df = pd.read_csv(p, dtype=str) if p.lower().endswith('.csv') else pd.read_excel(p, dtype=str)
df.columns = [str(c).strip().lower() for c in df.columns]
df['ip'] = [str(x).strip().split('/')[0] for x in df['ip'].fillna('')]
fail = set(open(fl).read().split())
df[df['ip'].isin(fail)].fillna('').to_excel(out, index=False)
EOF

# ---- SNMP 계정은 서버 설정에서 (계정이 없는 장비에 만들 때 필요) — 메모리로만 전달
if [ -z "${IPAM_SNMP_AUTH:-}" ]; then
  KV=$("${SSH[@]}" "grep -hE '^IPAM_SNMP_(USER|AUTH|PRIV)=' $NBD/env/ipam-snmp.env 2>/dev/null || sudo -n grep -hE '^IPAM_SNMP_(USER|AUTH|PRIV)=' $NBD/env/ipam-snmp.env 2>/dev/null")
  while IFS= read -r line; do
    [ -n "$line" ] && export "${line%%=*}=${line#*=}"
  done <<< "$KV"
  [ -n "${IPAM_SNMP_AUTH:-}" ] && echo "      SNMP 계정: 서버 설정에서 읽음 (${IPAM_SNMP_USER:-ipam-ro})" \
                               || echo "      (서버 env 파일을 읽지 못함 — 다음 단계에서 SNMP 계정을 물어봄)"
fi

echo "[3/4] 응답 없는 $F 대에 ACL 추가 (허용 IP: $ALL_IPS) — 이 PC 에서 SSH"
export COLLECTOR_IPS=$ALL_IPS
( cd "$WORK" && bash "$TOOLS/run_bulk.sh" "$WORK/retry.xlsx" --apply --acl-only --force \
    --out "$WORK/apply.xlsx" "${PASS[@]+"${PASS[@]}"}" )

echo "[4/4] 서버에서 다시 확인 ($F 대)"
sleep 5
reach "$WORK/fail_ips.txt" "$WORK/after.txt"
summary "$WORK/after.txt"

"$PY" - "$WORK" "$TOOLS/acl_fix_$STAMP.xlsx" "$ALL_IPS" <<'EOF'
import sys, os, pandas as pd
w, out, srcs = sys.argv[1:4]
def load(f):
    d = {}
    if os.path.exists(f):
        for ln in open(f, encoding='utf-8'):
            p = ln.rstrip('\n').split('|')
            if len(p) >= 4:
                d[p[1]] = (p[2], p[3])
    return d
lab = {'ok': '응답', 'timeout': '응답 없음(ACL·방화벽·SNMP 꺼짐)', 'usm': '계정 없음', 'auth': '비밀번호 불일치', 'error': '오류'}
b, a = load(f'{w}/before.txt'), load(f'{w}/after.txt')
ap = pd.read_excel(f'{w}/apply.xlsx', dtype=str).fillna('') if os.path.exists(f'{w}/apply.xlsx') else pd.DataFrame(columns=['ip'])
ap = {r['ip']: r for _, r in ap.iterrows()}
rows = []
for ip, (st, msg) in b.items():
    if st == 'ok':
        continue
    r = ap.get(ip, {})
    st2, msg2 = a.get(ip, ('', ''))
    rows.append({'ip': ip, 'name': r.get('name', ''), 'vendor': r.get('vendor', ''),
                 '처음(서버 확인)': lab.get(st, st), 'ACL 적용': r.get('status', '(적용 안 함)'),
                 '넣은 명령': r.get('commands', ''), '적용 메모': r.get('detail', ''),
                 '다시 확인(서버)': lab.get(st2, st2) or '-', '사유': msg2 if st2 != 'ok' else ''})
df = pd.DataFrame(rows)
df.to_excel(out, index=False)
fixed = (df['다시 확인(서버)'] == '응답').sum() if len(df) else 0
print(f'\n결과: 응답 없던 {len(df)}대 중 {fixed}대 해결, {len(df) - fixed}대 남음 → {out}')
print(f'허용한 수집 서버 IP: {srcs}')
if len(df) - fixed:
    left = df[df['다시 확인(서버)'] != '응답']
    print('남은 장비 사유(ACL 적용 결과 기준):')
    for k, v in left['ACL 적용'].value_counts().items():
        print(f'  {k}: {v}대')
EOF
echo "중간 파일: $WORK/"
