#!/bin/bash
# =====================================================================================
# [새 서버] 이관·업그레이드 후 점검 — 문제 있는 줄에 [FAIL] 표시
#   cd /opt/netbox-docker && bash /opt/test-ipam/deploy/centos/post_check.sh [https://ipam.inha.ac.kr] [메일 받을 주소]
# =====================================================================================
cd "${NBD:-/opt/netbox-docker}" || exit 1
URL=${1:-http://127.0.0.1:8000}; MAILTO=${2:-}
P=0; F=0
ok()  { echo "  [ OK ] $1"; P=$((P+1)); }
bad() { echo "  [FAIL] $1"; F=$((F+1)); }
chk() { if eval "$2" >/dev/null 2>&1; then ok "$1"; else bad "$1"; fi; }
MP='/opt/netbox/venv/bin/python /opt/netbox/netbox/manage.py'
echo "== 컨테이너"
for s in netbox netbox-worker postgres redis redis-cache; do
  st=$(docker inspect -f '{{.State.Status}}/{{if .State.Health}}{{.State.Health.Status}}{{end}}' "$(docker compose ps -q $s)" 2>/dev/null)
  case $st in running/healthy|running/) ok "$s $st";; *) bad "$s ${st:-없음}";; esac
  pol=$(docker inspect -f '{{.HostConfig.RestartPolicy.Name}}' "$(docker compose ps -q $s)" 2>/dev/null)
  [ "$pol" = unless-stopped ] || bad "$s 자동 재시작 정책 없음 ($pol) — 운영용 override 확인"
done
echo "== NetBox·플러그인"
chk "DB 마이그레이션 최신" "docker compose exec -T netbox $MP migrate --check"
V=$(docker compose exec -T netbox $MP shell -c "import netbox_ip_request as p; print(p.config.version)" 2>/dev/null | tail -1)
[ -n "$V" ] && ok "플러그인 netbox_ip_request $V" || bad "플러그인 로드 실패"
chk "웹 응답 ($URL/login/)" "curl -skf -o /dev/null $URL/login/"
chk "8000 포트가 외부에 열려 있지 않음(127.0.0.1 전용)" "! ss -ltn | grep -E '(0\.0\.0\.0|\*|\[::\]):8000 '"
echo "== 설정 값"
docker compose exec -T netbox $MP shell -c "
from django.conf import settings as s
c=s.PLUGINS_CONFIG.get('netbox_ip_request',{})
print('  TIME_ZONE', s.TIME_ZONE); print('  ALLOWED_HOSTS', s.ALLOWED_HOSTS); print('  CSRF_TRUSTED_ORIGINS', s.CSRF_TRUSTED_ORIGINS)
print('  EMAIL', s.EMAIL_HOST if hasattr(s,'EMAIL_HOST') else '', '보내는 주소', s.SERVER_EMAIL)
print('  발급 범위', c.get('alloc_host_min'), '~', c.get('alloc_host_max'), '· 기한', c.get('default_period_days'), '일 · DNS', c.get('dns_servers'))
print('  SNMP 계정', len(c.get('snmp_credentials') or []), '개')
" 2>/dev/null
if [ -n "$MAILTO" ]; then
  echo "== 메일 발송 시험 → $MAILTO"
  chk "SMTP 발송" "docker compose exec -T netbox $MP shell -c \"from django.core.mail import send_mail; from django.conf import settings as s; send_mail('[IPAM] 메일 시험', '새 서버에서 보낸 시험 메일입니다.', s.SERVER_EMAIL, ['$MAILTO'], fail_silently=False)\""
fi
echo "== 백업"
chk "백업 cron 등록 (/etc/cron.d/netbox-backup)" "test -f /etc/cron.d/netbox-backup"
L=$(ls -1d /backup/netbox/*/ 2>/dev/null | tail -1); [ -n "$L" ] && ok "최근 백업 $L" || bad "백업 폴더 없음 (/backup/netbox)"
echo "결과: OK $P / FAIL $F"
[ $F = 0 ]
