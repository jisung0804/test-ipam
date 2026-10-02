#!/bin/bash
# =====================================================================================
# [기존 서버(WSL)에서 실행] NetBox 전체(DB·업로드 스크립트·첨부·설정·플러그인)를 파일 하나로 묶는다
#   cd ~/netbox-docker && bash export_old_server.sh            # 리허설: 묶은 뒤 기존 서버 다시 켬
#   cd ~/netbox-docker && bash export_old_server.sh --final    # 실제 전환: 묶은 뒤 기존 서버 꺼 둠
# 결과: ~/ipam-migrate-YYYYMMDD-HHMM.tar   (env 비밀번호가 들어 있으니 scp 로만 옮기고 다 쓰면 지울 것)
# =====================================================================================
set -euo pipefail
FINAL=0; [ "${1:-}" = "--final" ] && FINAL=1
[ -f docker-compose.yml ] && [ -d env ] || { echo "netbox-docker 폴더 안에서 실행하세요 (docker-compose.yml, env/ 가 있는 곳)"; exit 1; }
TS=$(date +%Y%m%d-%H%M); NAME=ipam-migrate-$TS; OUT=$HOME/$NAME
mkdir -p "$OUT"
echo "[1/6] 데이터가 바뀌지 않도록 NetBox 웹·작업자 정지 (DB 는 켜 둠)"
docker compose stop netbox-worker netbox
docker compose up -d postgres
for _ in $(seq 1 30); do docker compose exec -T postgres sh -c 'pg_isready -q -U "$POSTGRES_USER"' && break; sleep 2; done

echo "[2/6] DB 백업 (pg_dump custom 형식)"
docker compose exec -T postgres sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > "$OUT/netbox.dump"
N=$(docker compose exec -T postgres sh -c 'pg_restore -l' < "$OUT/netbox.dump" | grep -c "TABLE DATA" || true)
[ "$N" -gt 100 ] || { echo "DB 백업이 비정상입니다 (TABLE DATA $N 개). 중단"; exit 1; }
echo "      테이블 $N 개, $(du -h "$OUT/netbox.dump" | cut -f1)"

echo "[3/6] 업로드 파일(media·scripts·reports) 백업"
for d in media scripts reports; do
  docker compose run --rm --no-deps -T --entrypoint tar netbox -C /opt/netbox/netbox/$d -czf - . > "$OUT/$d.tgz"
  echo "      $d: $(tar -tzf "$OUT/$d.tgz" | wc -l) 항목"
done

echo "[4/6] 설정·플러그인 백업 (env 비밀번호 포함 — 외부 공유 금지)"
tar --exclude='.git' --exclude='__pycache__' --exclude='*.egg-info' -czf "$OUT/config.tgz" \
    env configuration docker-compose.override.yml Dockerfile-Plugins netbox-ip-request $(ls *.sh 2>/dev/null)

echo "[5/6] 버전·건수 기록 (새 서버와 비교용)"
{
  echo "exported_at=$TS"
  echo "netbox_image=$(docker compose config --images | grep -m1 ipreq || true)"
  docker compose exec -T postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At -F=' < "$(dirname "$0")/counts.sql"
} > "$OUT/counts.txt"
cat "$OUT/counts.txt"
(cd "$OUT" && sha256sum netbox.dump *.tgz > SHA256SUMS)

echo "[6/6] 하나의 파일로 묶기"
tar -C "$HOME" -cf "$HOME/$NAME.tar" "$NAME" && rm -rf "$OUT"
chmod 600 "$HOME/$NAME.tar"
if [ $FINAL = 1 ]; then
  echo "※ --final: 기존 서버 NetBox 는 꺼진 상태로 둡니다 (새 서버 확인 후에도 켜지 말 것 — 데이터가 갈라짐)"
else
  docker compose start netbox netbox-worker
  echo "※ 리허설: 기존 서버를 다시 켰습니다. 이후 변경분은 실제 전환(--final) 때 다시 옮겨집니다"
fi
echo "완료: $HOME/$NAME.tar  ($(du -h "$HOME/$NAME.tar" | cut -f1))"
echo "새 서버로 복사:  scp $HOME/$NAME.tar <계정>@<새서버IP>:~/"
