#!/bin/bash
# =====================================================================================
# nginx 기본 웹 서버(80 포트) 닫기 — IPAM 은 18000 포트만 쓴다
#   sudo bash /opt/test-ipam/deploy/centos/nginx_close_80.sh          # 닫기
#   sudo bash /opt/test-ipam/deploy/centos/nginx_close_80.sh --check  # 바꾸지 않고 상태만
# 하는 일
#   1) /etc/nginx/nginx.conf 백업 (nginx.conf.bak-날짜)
#   2) nginx.conf 안의 '기본 서버 블록'(listen 80, 주석 아닌 것)만 주석 처리 — 나머지 설정은 그대로
#   3) conf.d 의 다른 파일에 listen 80 이 있으면 알려 줌(직접 판단)
#   4) nginx -t 통과하면 reload, 실패하면 백업으로 되돌림
#   5) 확인: 80 포트 LISTEN 이 사라졌는지, 18000 은 그대로인지
# =====================================================================================
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "sudo 로 실행하세요"; exit 1; }
CONF=${CONF:-/etc/nginx/nginx.conf}
CHECK=0; [ "${1:-}" = --check ] && CHECK=1
listening() { ss -tlnH 2>/dev/null | awk '{print $4}' | grep -E "[:.]$1\$" >/dev/null; }

echo "== 현재"
listening 80 && echo "  80 포트: 열림" || echo "  80 포트: 닫힘"
listening 18000 && echo "  18000 포트: 열림" || echo "  18000 포트: 닫힘"
grep -rnE '^\s*listen\s+(\[::\]:)?80(\s|;)' /etc/nginx/conf.d/ 2>/dev/null | sed 's/^/  conf.d 에서 80 사용: /' || true
[ $CHECK = 1 ] && exit 0

BAK="$CONF.bak-$(date +%Y%m%d-%H%M%S)"
cp -p "$CONF" "$BAK"
python3 - "$CONF" <<'PY'
import re, sys
p = sys.argv[1]
lines = open(p, encoding='utf-8').read().split('\n')
out, i, n = [], 0, 0
while i < len(lines):
    ln = lines[i]
    if re.match(r'^\s*server\s*\{', ln):
        depth, j = 0, i
        while j < len(lines):                       # 이 server 블록의 끝 찾기(주석 줄 제외)
            code = lines[j].split('#', 1)[0]
            depth += code.count('{') - code.count('}')
            if depth == 0:
                break
            j += 1
        block = lines[i:j + 1]
        if any(re.match(r'^\s*listen\s+(\[::\]:)?80(\s|;)', b.split('#', 1)[0]) for b in block):
            out.append('    # [IPAM] 80 포트 기본 서버 비활성화 — nginx_close_80.sh')
            out += ['#' + b for b in block]
            n += 1
            i = j + 1
            continue
    out.append(ln)
    i += 1
open(p, 'w', encoding='utf-8').write('\n'.join(out))
print(f'  nginx.conf: 80 포트 서버 블록 {n}개 주석 처리')
PY
if nginx -t 2>/dev/null; then
  systemctl reload nginx
  echo "  nginx -t 통과 → 다시 읽음 (백업: $BAK)"
else
  cp -p "$BAK" "$CONF"
  echo "  ※ nginx -t 실패 → 원래 설정으로 되돌림. 'nginx -t' 출력 확인"; nginx -t || true; exit 1
fi
sleep 1
echo "== 결과"
listening 80 && echo "  80 포트: 아직 열림 — 위 'conf.d 에서 80 사용' 파일 확인" || echo "  80 포트: 닫힘"
listening 18000 && echo "  18000 포트: 열림 (IPAM 정상)" || echo "  ※ 18000 포트가 닫힘 — 'systemctl status nginx' 확인"
command -v firewall-cmd >/dev/null && firewall-cmd --list-services 2>/dev/null | grep -qwE 'http|https' \
  && echo "  참고: firewalld 에 http/https 서비스가 열려 있음 → 필요 없으면: firewall-cmd --permanent --remove-service=http --remove-service=https && firewall-cmd --reload"
exit 0
