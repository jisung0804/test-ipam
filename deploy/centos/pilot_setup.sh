#!/bin/bash
# =====================================================================================
# [새 서버] 시범 운영 설정 — NetBox 를 http://<서버IP>:18000 으로, 지정한 관리자 PC 에서만 열기
#   sudo bash /opt/test-ipam/deploy/centos/pilot_setup.sh <서버IP> <관리자IP 또는 대역> [...]
#   예) sudo bash pilot_setup.sh 165.246.12.104 165.246.1.21 165.246.1.22 165.246.30.0/24
#   관리자를 추가·삭제할 때도 '전체 목록'으로 다시 실행하면 된다(이전 허용 목록은 지우고 새로 씀).
#   전체 사용자에게 열 때(실제 운영): 관리자 자리에 학내 대역(예: 165.246.0.0/16) 또는 all
#   HTTPS 운영 설정(nginx-netbox.conf)으로 바꾼 뒤에 다시 실행해도 그 설정은 유지하고 허용 목록만 바꾼다
# 하는 일
#   1) SELinux: nginx 가 18000 포트를 쓰고 내부 8000 으로 넘길 수 있게 허용
#   2) firewalld: 18000 은 관리자 IP 에서만 허용 (서버 방화벽)
#   3) nginx: 시범 운영 설정(nginx-netbox-pilot.conf) + 허용 목록(/etc/nginx/netbox-allow.inc)
#   4) NetBox: ALLOWED_HOSTS·CSRF_TRUSTED_ORIGINS 에 http://<서버IP>:18000 등록 후 재기동
#   5) 확인: 서버 자신에서 http://127.0.0.1:18000/login/ 응답
# =====================================================================================
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "sudo 로 실행하세요"; exit 1; }
SERVER_IP=${1:?사용법: pilot_setup.sh <서버IP> <관리자IP...>}; shift
[ $# -ge 1 ] || { echo "관리자 IP(또는 대역)를 하나 이상 적으세요. 제한 없이 열려면 all"; exit 1; }
ADMINS=("$@")
PORT=${PORT:-18000}
NBD=${NBD:-/opt/netbox-docker}
HERE=$(cd "$(dirname "$0")" && pwd)
ipok() { [[ $1 =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}(/[0-9]{1,2})?$ ]]; }
ipok "$SERVER_IP" || { echo "서버 IP 형식 오류: $SERVER_IP"; exit 1; }
for a in "${ADMINS[@]}"; do [ "$a" = all ] || ipok "$a" || { echo "관리자 IP 형식 오류: $a"; exit 1; }; done

echo "[1/5] SELinux: nginx 가 $PORT 포트 사용·내부 연결 허용"
if command -v getenforce >/dev/null && [ "$(getenforce)" != Disabled ]; then
  semanage port -a -t http_port_t -p tcp "$PORT" 2>/dev/null || semanage port -m -t http_port_t -p tcp "$PORT"
  setsebool -P httpd_can_network_connect 1
else
  echo "      SELinux 꺼짐 — 건너뜀"
fi

echo "[2/5] firewalld: $PORT 포트 허용 대상 다시 설정"
systemctl enable --now firewalld >/dev/null
# 이전에 이 스크립트가 넣은 규칙(같은 포트)을 모두 지우고 새로 넣는다
while read -r r; do
  [ -n "$r" ] && firewall-cmd --permanent --remove-rich-rule="$r" >/dev/null
done < <(firewall-cmd --permanent --list-rich-rules | grep "port=\"$PORT\"" || true)
firewall-cmd --permanent --remove-port="$PORT/tcp" >/dev/null 2>&1 || true
for a in "${ADMINS[@]}"; do
  if [ "$a" = all ]; then
    firewall-cmd --permanent --add-port="$PORT/tcp" >/dev/null
  else
    firewall-cmd --permanent --add-rich-rule="rule family=\"ipv4\" source address=\"$a\" port port=\"$PORT\" protocol=\"tcp\" accept" >/dev/null
  fi
done
firewall-cmd --reload >/dev/null
echo "      허용: ${ADMINS[*]}"

echo "[3/5] nginx: 시범 운영 설정 설치"
{
  echo "# pilot_setup.sh 가 작성 ($(date '+%F %T')) — 관리자 PC 허용 목록"
  echo "allow 127.0.0.1;            # 서버 자신(점검 스크립트)"
  for a in "${ADMINS[@]}"; do
    if [ "$a" = all ]; then echo "allow all;"; else echo "allow $a;"; fi
  done
} > /etc/nginx/netbox-allow.inc
if grep -qs "listen $PORT ssl" /etc/nginx/conf.d/netbox.conf; then
  # 이미 실제 운영(HTTPS) 설정으로 바꾼 서버: 설정 파일은 그대로 두고 허용 목록만 바꾼다
  echo "      HTTPS 운영 설정 유지 — 허용 목록만 갱신"
else
  if [ -f /etc/nginx/conf.d/netbox.conf ] && ! cmp -s /etc/nginx/conf.d/netbox.conf "$HERE/nginx-netbox-pilot.conf"; then
    cp /etc/nginx/conf.d/netbox.conf "/etc/nginx/conf.d/netbox.conf.bak-$(date +%Y%m%d-%H%M)"   # .bak 은 nginx 가 읽지 않음
  fi
  cp "$HERE/nginx-netbox-pilot.conf" /etc/nginx/conf.d/netbox.conf
fi
command -v restorecon >/dev/null && restorecon -R /etc/nginx >/dev/null 2>&1 || true
nginx -t
systemctl enable nginx >/dev/null
systemctl restart nginx

echo "[4/5] NetBox: 접속 주소 등록 (env/netbox.env)"
ENVF="$NBD/env/netbox.env"
[ -f "$ENVF" ] || { echo "$ENVF 가 없습니다. NBD=<netbox-docker 경로> 로 지정하세요"; exit 1; }
OWNER=$(stat -c %U:%G "$ENVF")
cp -p "$ENVF" "$ENVF.bak-$(date +%Y%m%d-%H%M)"
setenv() {   # 같은 키가 있으면 바꾸고, 없으면 끝에 추가 (따옴표 없이 — docker env_file 규칙)
  if grep -q "^$1=" "$ENVF"; then sed -i "s|^$1=.*|$1=$2|" "$ENVF"; else echo "$1=$2" >> "$ENVF"; fi
}
HOSTS=$(grep -E '^ALLOWED_HOSTS=' "$ENVF" | cut -d= -f2- || true)
for h in "$SERVER_IP" localhost; do
  [[ " $HOSTS " == *" $h "* ]] || HOSTS="${HOSTS:+$HOSTS }$h"
done
[[ " $HOSTS " == *" * "* ]] && HOSTS="$SERVER_IP localhost"     # 기본값 * 은 쓰지 않음
setenv ALLOWED_HOSTS "$HOSTS"
ORIG=$(grep -E '^CSRF_TRUSTED_ORIGINS=' "$ENVF" | cut -d= -f2- || true)
O="http://$SERVER_IP:$PORT"
[[ " $ORIG " == *" $O "* ]] || ORIG="${ORIG:+$ORIG }$O"
setenv CSRF_TRUSTED_ORIGINS "$ORIG"
chown "$OWNER" "$ENVF"; chmod 600 "$ENVF"
echo "      ALLOWED_HOSTS=$HOSTS"
echo "      CSRF_TRUSTED_ORIGINS=$ORIG"
(cd "$NBD" && docker compose up -d >/dev/null)

echo "[5/5] 확인 (NetBox 가 다시 뜨는 데 1~2분)"
code=000
URL="http://127.0.0.1:$PORT/login/"
grep -qs "listen $PORT ssl" /etc/nginx/conf.d/netbox.conf && URL="https://127.0.0.1:$PORT/login/"
for _ in $(seq 1 40); do
  code=$(curl -sk -o /dev/null -w '%{http_code}' "$URL" || true)
  [ "$code" = 200 ] && break; sleep 5
done
if [ "$code" = 200 ]; then
  if grep -qs "listen $PORT ssl" /etc/nginx/conf.d/netbox.conf; then
    echo "완료 — 허용된 PC 브라우저에서 https://<서버 이름>:$PORT 으로 접속하세요"
  else
    echo "완료 — 관리자 PC 브라우저에서 http://$SERVER_IP:$PORT 으로 접속하세요"
  fi
else
  echo "※ $URL 응답 $code — 'sudo tail /var/log/nginx/error.log' 와 'docker compose ps' 를 확인하세요"
  exit 1
fi
