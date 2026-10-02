#!/bin/bash
# =====================================================================================
# [새 CentOS 서버에서 실행] export_old_server.sh 로 만든 묶음을 netbox-docker 에 복원
#   cd /opt/netbox-docker && bash /opt/test-ipam/deploy/centos/restore_new_server.sh ~/ipam-migrate-YYYYMMDD-HHMM.tar
# 하는 일: 설정·플러그인 복원 → 이미지 빌드 → DB 복원 → 업로드 파일 복원 → 기동 → 건수 비교
# 주의: 이 폴더의 기존 DB·첨부 파일은 지워지고 묶음의 내용으로 바뀜. set_secrets.sh 는 절대 다시 실행하지 말 것
#       (기존 SECRET_KEY·API_TOKEN_PEPPER 가 바뀌면 API 토큰과 로그인 세션이 모두 무효가 됨)
# =====================================================================================
set -euo pipefail
TAR=${1:?사용법: restore_new_server.sh <ipam-migrate-....tar 또는 /backup/netbox/날짜 폴더>}
HERE=$(cd "$(dirname "$0")" && pwd)
[ -f docker-compose.yml ] || { echo "netbox-docker 폴더(/opt/netbox-docker) 안에서 실행하세요"; exit 1; }
WORK=$(mktemp -d "$PWD/.ipam-restore.XXXX"); trap 'rm -rf "$WORK"' EXIT
if [ -d "$TAR" ]; then B=$(cd "$TAR" && pwd)          # backup.sh 가 만든 백업 폴더에서 복원
else tar -C "$WORK" -xf "$TAR"; B=$(ls -d "$WORK"/ipam-migrate-*); fi

echo "[1/7] 파일 무결성 확인"
(cd "$B" && sha256sum -c SHA256SUMS)

echo "[2/7] 설정·플러그인 복원 (기존 파일은 backup-before-restore-*.tgz 로 보관)"
tar -czf "backup-before-restore-$(date +%Y%m%d-%H%M).tgz" env configuration $(ls docker-compose.override.yml Dockerfile-Plugins 2>/dev/null) || true
tar -xzf "$B/config.tgz"
# 운영용 override(포트 127.0.0.1 고정·자동 재시작)로 교체
cp "$HERE/docker-compose.override.yml" docker-compose.override.yml
grep -q '^TIME_ZONE=' env/netbox.env || echo 'TIME_ZONE=Asia/Seoul' >> env/netbox.env
chmod 600 env/*.env
# 기존 비밀값을 그대로 써야 하므로 set_secrets.sh 를 실수로 다시 돌리지 않게 이름을 바꿔 둠
[ -f set_secrets.sh ] && mv set_secrets.sh set_secrets.sh.done-DO-NOT-RUN

echo "[3/7] 이미지 빌드 (몇 분 걸림)"
docker compose build --pull netbox

echo "[4/7] DB 복원"
# 볼륨(DB·Redis·첨부)을 비우고 새로 만든다 → DB 비밀번호가 복원한 env 와 항상 일치, 몇 번 다시 실행해도 같은 결과
docker compose down -v
docker compose up -d postgres redis redis-cache
for _ in $(seq 1 60); do docker compose exec -T postgres sh -c 'pg_isready -q -U "$POSTGRES_USER"' && break; sleep 2; done
docker compose exec -T postgres sh -c 'dropdb -U "$POSTGRES_USER" --if-exists "$POSTGRES_DB" && createdb -U "$POSTGRES_USER" -O "$POSTGRES_USER" "$POSTGRES_DB"'
docker compose exec -T postgres sh -c 'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --no-owner --role="$POSTGRES_USER" --exit-on-error' < "$B/netbox.dump"
echo "      DB 복원 완료"

echo "[5/7] 업로드 파일 복원"
for d in media scripts reports; do
  docker compose run --rm --no-deps -T --entrypoint tar netbox --no-same-owner --no-overwrite-dir -C /opt/netbox/netbox/$d -xzf - < "$B/$d.tgz"
done

echo "[6/7] 기동 (첫 기동은 DB 마이그레이션·검색 인덱스 때문에 2~5분)"
docker compose up -d
for _ in $(seq 1 60); do
  s=$(docker inspect -f '{{.State.Health.Status}}' "$(docker compose ps -q netbox)" 2>/dev/null || echo starting)
  [ "$s" = healthy ] && break; sleep 5
done
echo "      netbox 상태: $s"

[ -f "$B/counts.txt" ] || { echo "[7/7] 복원 완료 (백업 폴더에는 건수 기록이 없어 비교 생략)"; exit 0; }
echo "[7/7] 건수 비교 (왼쪽: 기존 서버, 오른쪽: 새 서버)"
docker compose exec -T postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At -F=' < "$HERE/counts.sql" > "$WORK/new.txt"
DIFF=0
while IFS='=' read -r k v; do
  case $k in exported_at|netbox_image) continue;; esac
  n=$(grep "^$k=" "$WORK/new.txt" | cut -d= -f2); mark=OK; [ "$v" = "$n" ] || { mark='다름!'; DIFF=1; }
  printf '  %-15s %8s  %8s  %s\n' "$k" "$v" "$n" "$mark"
done < "$B/counts.txt"
[ $DIFF = 0 ] && echo "복원 완료 — 건수 모두 일치" || echo "※ 건수가 다른 항목이 있습니다. 위 표를 확인하세요"
