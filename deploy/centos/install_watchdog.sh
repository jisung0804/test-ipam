#!/bin/bash
# =====================================================================================
# IPAM 자동 복구(워치독) 설치 — 5분마다 cron 실행 + 로그 정리
#   sudo bash /opt/test-ipam/deploy/centos/install_watchdog.sh            # 설치(다시 실행해도 같은 결과)
#   sudo bash /opt/test-ipam/deploy/centos/install_watchdog.sh remove     # 제거
# =====================================================================================
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "sudo 로 실행하세요"; exit 1; }
HERE=$(cd "$(dirname "$0")" && pwd)
NBD=${NBD:-/opt/netbox-docker}
if [ "${1:-}" = remove ]; then
  rm -f /etc/cron.d/ipam-watchdog /etc/logrotate.d/ipam-watchdog /usr/local/sbin/ipam-watchdog
  echo "제거 완료 (로그 /var/log/ipam-watchdog.log 는 남겨 둠)"; exit 0
fi
[ -f "$NBD/docker-compose.yml" ] || { echo "$NBD 에 docker-compose.yml 이 없음 — NBD=<netbox-docker 경로> 로 지정"; exit 1; }
install -m 755 "$HERE/ipam_watchdog.sh" /usr/local/sbin/ipam-watchdog
cat > /etc/cron.d/ipam-watchdog <<CRON
# IPAM 자동 복구 — 5분마다 (install_watchdog.sh 가 작성)
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
*/5 * * * * root NBD=$NBD /usr/local/sbin/ipam-watchdog >> /var/log/ipam-watchdog.log 2>&1
CRON
chmod 644 /etc/cron.d/ipam-watchdog
cat > /etc/logrotate.d/ipam-watchdog <<ROT
/var/log/ipam-watchdog.log {
    weekly
    rotate 8
    compress
    missingok
    notifempty
}
ROT
command -v restorecon >/dev/null && restorecon /etc/cron.d/ipam-watchdog /usr/local/sbin/ipam-watchdog 2>/dev/null || true
systemctl is-active --quiet crond 2>/dev/null || systemctl enable --now crond 2>/dev/null || true
echo "설치 완료 — 지금 한 번 실행:"
NBD=$NBD VERBOSE=1 /usr/local/sbin/ipam-watchdog | tee -a /var/log/ipam-watchdog.log
echo "로그: tail -f /var/log/ipam-watchdog.log"
