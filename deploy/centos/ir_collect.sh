#!/bin/bash
# =====================================================================================
# 웹쉘 탐지 대응 — 증거 수집 (읽기 전용: 서버·컨테이너·NetBox 데이터를 바꾸지 않음)
#   sudo bash /opt/test-ipam/deploy/centos/ir_collect.sh [탐지된 파일 경로...]
#   예) sudo bash ir_collect.sh /opt/netbox-docker/netbox-ip-request/tools/verify_phase3.py
# 결과: /root/ir_<호스트>_<시각>/ 폴더 + 같은 이름 .tar.gz (+ .sha256) — 보안팀에 이 묶음을 전달
#   ※ env 파일(비밀번호·키)은 수집하지 않는다. 결과물에는 계정명·IP·명령 이력이 들어 있으니 취급 주의
# 수집 항목
#   00 요약 · 01 탐지 파일(해시·메타·사본) · 02 계정·로그인 · 03 프로세스·네트워크 · 04 예약 작업·서비스
#   05 최근 바뀐 파일 · 06 패키지 무결성(rpm -Va) · 07 nginx 로그 분석 · 08 docker 상태·컨테이너 변경
#   09 NetBox 내부(업로드 스크립트·사용자·토큰·변경 이력·스크립트 실행 이력) · 10 배포 코드 무결성(git)
# =====================================================================================
set -uo pipefail
[ "$(id -u)" = 0 ] || { echo "sudo 로 실행하세요"; exit 1; }
NBD=${NBD:-/opt/netbox-docker}
REPO=${REPO:-/opt/test-ipam}
DAYS=${DAYS:-30}
TS=$(date +%Y%m%d_%H%M%S)
OUT=/root/ir_$(hostname -s)_$TS
mkdir -p "$OUT"; chmod 700 "$OUT"
log() { echo "[$(date '+%T')] $*" | tee -a "$OUT/00_collect.log"; }
run() {  # run <파일> <명령...>  — 명령과 결과를 함께 남김
  local f="$OUT/$1"; shift
  { echo "### $(date '+%F %T') \$ $*"; timeout 300 bash -c "$*" 2>&1; echo; } >> "$f"
}
log "수집 시작 → $OUT (최근 ${DAYS}일)"

# ---- 00 요약 정보
run 00_system.txt "hostname; date; uptime; uname -a; cat /etc/os-release; getenforce 2>/dev/null; df -h; free -m"

# ---- 01 탐지 파일
for f in "$@"; do
  log "탐지 파일: $f"
  run 01_detected.txt "ls -la --time-style=full-iso '$f'; stat '$f'; sha256sum '$f'; md5sum '$f'; file '$f'"
  run 01_detected.txt "cd \$(dirname '$f') 2>/dev/null && git log -1 --format='%H %ci %s' -- '$(basename "$f")' 2>/dev/null && git status --porcelain -- '$(basename "$f")' 2>/dev/null"
  [ -f "$f" ] && mkdir -p "$OUT/01_samples" && cp -p "$f" "$OUT/01_samples/$(echo "$f" | tr '/' '_').sample" \
    && chmod 400 "$OUT/01_samples/"* 2>/dev/null
done

# ---- 02 계정·로그인
log "계정·로그인"
run 02_accounts.txt "awk -F: '\$3==0 || \$7 !~ /(nologin|false)\$/' /etc/passwd"
run 02_accounts.txt "ls -la --time-style=full-iso /etc/passwd /etc/shadow /etc/group /etc/sudoers; ls -la --time-style=full-iso /etc/sudoers.d/; cat /etc/sudoers.d/* 2>/dev/null"
run 02_accounts.txt "for h in /root /home/*; do for k in \$h/.ssh/authorized_keys \$h/.ssh/authorized_keys2; do [ -f \$k ] && { echo == \$k; ls -la --time-style=full-iso \$k; cut -c1-60 \$k; }; done; done"
run 02_accounts.txt "for h in /root /home/*; do [ -f \$h/.bash_history ] && { echo == \$h; tail -n 300 \$h/.bash_history; }; done"
run 02_logins.txt "last -Faiw -n 200; echo; lastb -Faiw -n 100 2>/dev/null | head -100"
run 02_logins.txt "journalctl -u sshd --since '-${DAYS} days' --no-pager 2>/dev/null | grep -E 'Accepted|Failed|Invalid' | tail -500"
run 02_logins.txt "grep -hE 'sudo:.*COMMAND' /var/log/secure* 2>/dev/null | tail -300"

# ---- 03 프로세스·네트워크
log "프로세스·네트워크"
run 03_process.txt "ps auxfww"
run 03_process.txt "ls -la /proc/*/exe 2>/dev/null | grep -E '\\(deleted\\)|/tmp/|/dev/shm/|/var/tmp/'"
run 03_network.txt "ss -tulpn; echo; ss -tnp state established; echo; ip addr; ip route"
run 03_network.txt "firewall-cmd --list-all 2>/dev/null; firewall-cmd --list-rich-rules 2>/dev/null"

# ---- 04 예약 작업·서비스
log "예약 작업·서비스"
run 04_persistence.txt "for u in \$(cut -d: -f1 /etc/passwd); do c=\$(crontab -l -u \$u 2>/dev/null); [ -n \"\$c\" ] && echo \"== \$u\" && echo \"\$c\"; done"
run 04_persistence.txt "ls -la --time-style=full-iso /etc/cron.d /etc/cron.daily /etc/cron.hourly /var/spool/cron 2>/dev/null; cat /etc/cron.d/* 2>/dev/null"
run 04_persistence.txt "systemctl list-timers --all --no-pager; systemctl list-unit-files --state=enabled --no-pager"
run 04_persistence.txt "find /etc/systemd /usr/lib/systemd /run/systemd -type f -mtime -$DAYS -ls 2>/dev/null"
run 04_persistence.txt "cat /etc/rc.d/rc.local 2>/dev/null; ls -la /etc/profile.d; cat /etc/ld.so.preload 2>/dev/null"

# ---- 05 최근 바뀐 파일 (docker 저장소 제외)
log "최근 ${DAYS}일 바뀐 파일"
run 05_recent_files.txt "find /opt /etc /usr/local /var/www /root /home -xdev -type f -mtime -$DAYS -not -path '*/.git/*' -not -path '*/__pycache__/*' -printf '%TY-%Tm-%Td %TH:%TM  %u  %m  %10s  %p\\n' 2>/dev/null | sort -r | head -3000"
run 05_tmp_exec.txt "find /tmp /var/tmp /dev/shm -xdev \\( -type f -perm -u+x -o -name '.*' \\) -printf '%TY-%Tm-%Td %TH:%TM  %u  %m  %10s  %p\\n' 2>/dev/null"
run 05_web_like.txt "find / -xdev \\( -path /proc -o -path /sys -o -path /var/lib/docker \\) -prune -o -type f \\( -iname '*.php*' -o -iname '*.jsp*' -o -iname '*.asp*' -o -iname '*.phtml' \\) -mtime -$DAYS -print 2>/dev/null"

# ---- 06 패키지 무결성 (OS 파일 변조)
log "rpm -Va (수 분 걸릴 수 있음)"
run 06_rpm_verify.txt "rpm -Va 2>/dev/null | grep -vE '^.{8} c ' | grep -E '^..5|^.M' | head -500"

# ---- 07 nginx 로그
log "nginx 로그 분석"
LOGS=$(ls /var/log/nginx/access.log* 2>/dev/null | tr '\n' ' ')
if [ -n "$LOGS" ]; then
  Z="zcat -f $LOGS"
  run 07_nginx.txt "$Z | awk '{print \$1}' | sort | uniq -c | sort -rn | head -50"
  run 07_nginx.txt "echo '== POST 상위 경로'; $Z | awk '\$6 ~ /POST/ {print \$7}' | sed 's/?.*//' | sort | uniq -c | sort -rn | head -50"
  run 07_nginx.txt "echo '== 스크립트 업로드·실행·파일 관련 요청'; $Z | grep -E 'extras/scripts|/api/extras/scripts|/core/files|/media/|upload|data-files|/core/data-' | tail -500"
  run 07_nginx.txt "echo '== 웹쉘·공격 흔적 문자열'; $Z | grep -iE '\\.php|\\.jsp|\\.asp|cmd=|exec=|shell|wget%20|curl%20|base64|/etc/passwd|\\.\\./|%2e%2e|union.*select|<script' | tail -500"
  run 07_nginx.txt "echo '== 허용 관리자 IP 밖에서 온 요청(403 제외)'; cat /etc/nginx/netbox-allow.inc 2>/dev/null; $Z | awk '\$9 != 403' | awk '{print \$1}' | sort -u"
  run 07_nginx.txt "tail -300 /var/log/nginx/error.log 2>/dev/null"
  mkdir -p "$OUT/07_nginx_raw"; cp -p /var/log/nginx/access.log* /var/log/nginx/error.log* "$OUT/07_nginx_raw/" 2>/dev/null
fi

# ---- 08 docker
log "docker 상태"
if cd "$NBD" 2>/dev/null; then
  run 08_docker.txt "cd $NBD && docker compose ps -a; docker images --digests; docker ps -a --no-trunc"
  for c in $(docker compose ps -q 2>/dev/null); do
    n=$(docker inspect -f '{{.Name}}' "$c" | tr -d /)
    run 08_docker_diff.txt "echo == $n; docker diff $c | grep -vE ' /tmp|/root/.cache|__pycache__|\\.pyc\$|/run/|/var/run/' | head -300"
    run 08_docker_inspect.txt "docker inspect -f '{{.Name}} {{.Config.Image}} 시작 {{.State.StartedAt}} 재시작 {{.RestartCount}} {{range .Mounts}}{{.Source}}->{{.Destination}} {{end}}' $c"
  done
  run 08_docker.txt "cd $NBD && docker compose exec -T netbox sh -c 'ps aux; echo; ls -la --time-style=full-iso /opt/netbox/netbox/scripts /opt/netbox/netbox/media /opt/netbox/netbox/reports 2>/dev/null; find /opt/netbox/netbox/scripts /opt/netbox/netbox/media /tmp -type f -mtime -$DAYS -ls 2>/dev/null'"

  # ---- 09 NetBox 내부 (업로드 스크립트·사용자·토큰·이력)
  log "NetBox 내부 데이터"
  cat > "$OUT/.nb.py" <<PY
import datetime as dt, hashlib, os
from django.conf import settings
from django.utils import timezone
since = timezone.now() - dt.timedelta(days=$DAYS)
g = lambda o, a: getattr(o, a, '-')
print('== 업로드된 스크립트 파일 (이름·수정 시각·SHA256)')
from extras.models import ScriptModule
for m in ScriptModule.objects.all():
    p = os.path.join(settings.SCRIPTS_ROOT, m.file_path)
    h = hashlib.sha256(open(p, 'rb').read()).hexdigest() if os.path.exists(p) else '(파일 없음)'
    print(f"{m.pk}\t{m.file_path}\t생성 {g(m, 'created')}\t수정 {g(m, 'last_updated')}\t{h}")
print('\n== 사용자 (관리자·최근 로그인)')
from users.models import User, Token
for u in User.objects.all().order_by('-last_login'):
    print(f"{u.username}\tsuperuser={g(u, 'is_superuser')}\tactive={g(u, 'is_active')}\t가입 {g(u, 'date_joined')}\t최근 로그인 {g(u, 'last_login')}")
print('\n== API 토큰')
for t in Token.objects.select_related('user'):
    print(f"#{t.pk}\t{t.user.username}\t생성 {g(t, 'created')}\t최근 사용 {g(t, 'last_used')}\t쓰기 {g(t, 'write_enabled')}\t만료 {g(t, 'expires')}\t허용 IP {g(t, 'allowed_ips')}")
print('\n== 최근 변경 이력: 스크립트·사용자·토큰·권한·설정')
from core.models import ObjectChange
for c in ObjectChange.objects.filter(time__gte=since, changed_object_type__model__in=[
        'scriptmodule', 'user', 'token', 'objectpermission', 'group', 'configrevision', 'datasource', 'datafile',
        'eventrule', 'webhook', 'customlink', 'exporttemplate']).order_by('-time')[:500]:
    print(f"{c.time:%F %T}\t{c.user_name}\t{c.request_id}\t{c.action}\t{c.changed_object_type.model}\t{c.object_repr}")
print('\n== 최근 스크립트 실행 이력')
from core.models import Job
for j in Job.objects.filter(created__gte=since).exclude(user__isnull=True).order_by('-created')[:500]:
    print(f"{j.created:%F %T}\t{j.user}\t{j.name}\t{j.status}")
print('\n== 웹훅·이벤트 규칙(외부로 데이터를 보낼 수 있는 설정)')
from extras.models import EventRule, Webhook
for w in Webhook.objects.all():
    print(f"webhook {w.name}\t{g(w, 'payload_url')}")
for e in EventRule.objects.all():
    print(f"eventrule {e.name}\t{g(e, 'action_type')}\t{g(e, 'enabled')}")
PY
  run 09_netbox.txt "cd $NBD && docker compose exec -T netbox /opt/netbox/venv/bin/python /opt/netbox/netbox/manage.py shell < $OUT/.nb.py 2>&1 | grep -v 'objects imported'"
  rm -f "$OUT/.nb.py"
  run 09_netbox_scripts_copy.txt "cd $NBD && docker compose exec -T netbox sh -c 'cd /opt/netbox/netbox/scripts && for f in \$(find . -type f -name \"*.py\"); do echo \"=== \$f\"; sha256sum \$f; done'"
  mkdir -p "$OUT/09_scripts"
  docker compose exec -T netbox sh -c 'cd /opt/netbox/netbox/scripts && tar -cf - --exclude=__pycache__ .' 2>/dev/null | tar -xf - -C "$OUT/09_scripts" 2>/dev/null
fi

# ---- 10 배포 코드 무결성
log "배포 코드 무결성(git 과 비교)"
if [ -d "$REPO/.git" ]; then
  run 10_code.txt "cd $REPO && git log -5 --format='%h %ci %s' && echo && echo '== git 과 다른 파일(수정·추가)' && git status --porcelain --untracked-files=all"
  run 10_code.txt "diff -rq --exclude=.git --exclude=__pycache__ --exclude='*.pyc' --exclude='*.egg-info' $REPO $NBD/netbox-ip-request 2>&1 | head -200"
  run 10_code.txt "cd $REPO && echo '== 업로드 스크립트 vs 저장소 scripts/' && for f in scripts/*.py; do sha256sum \$f; done"
fi

# ---- 묶기
log "묶는 중"
tar -C /root -czf "$OUT.tar.gz" "$(basename "$OUT")" && sha256sum "$OUT.tar.gz" > "$OUT.tar.gz.sha256"
chmod 600 "$OUT.tar.gz" "$OUT.tar.gz.sha256"
log "완료: $OUT.tar.gz ($(du -h "$OUT.tar.gz" | cut -f1)) / SHA256: $(cut -d' ' -f1 "$OUT.tar.gz.sha256")"
echo
echo "먼저 볼 파일: 01_detected.txt · 10_code.txt(git 과 다른 파일) · 09_netbox.txt(스크립트·사용자·토큰) · 07_nginx.txt"
