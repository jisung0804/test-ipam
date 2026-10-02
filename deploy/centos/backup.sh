#!/bin/bash
# =====================================================================================
# [새 서버] 매일 자동 백업 — DB·업로드 파일·설정을 /backup/netbox/날짜 폴더에 저장, 14일 지난 것은 삭제
# 설치:  sudo cp backup.sh /opt/netbox-docker/ && sudo chmod 700 /opt/netbox-docker/backup.sh
#        echo '30 2 * * * root /opt/netbox-docker/backup.sh >> /var/log/netbox-backup.log 2>&1' | sudo tee /etc/cron.d/netbox-backup
# 복원:  cd /opt/netbox-docker && bash /opt/test-ipam/deploy/centos/restore_new_server.sh /backup/netbox/<날짜폴더>
# =====================================================================================
set -euo pipefail
NBD=${NBD:-/opt/netbox-docker}; DEST=${DEST:-/backup/netbox}; KEEP_DAYS=${KEEP_DAYS:-14}
cd "$NBD"
TS=$(date +%Y%m%d-%H%M); OUT="$DEST/$TS"; mkdir -p "$OUT"; chmod 700 "$DEST"
echo "== $(date '+%F %T') 백업 시작 → $OUT"
docker compose exec -T postgres sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > "$OUT/netbox.dump"
N=$(docker compose exec -T postgres sh -c 'pg_restore -l' < "$OUT/netbox.dump" | grep -c "TABLE DATA" || true)
if [ "$N" -lt 100 ]; then echo "!! DB 백업 이상 (TABLE DATA $N)"; exit 1; fi
for d in media scripts reports; do
  docker compose exec -T netbox tar -C /opt/netbox/netbox/$d -czf - . > "$OUT/$d.tgz"
done
tar --exclude='.git' --exclude='__pycache__' -czf "$OUT/config.tgz" env configuration docker-compose.override.yml Dockerfile-Plugins netbox-ip-request $(ls *.sh 2>/dev/null)
(cd "$OUT" && sha256sum netbox.dump *.tgz > SHA256SUMS)
chmod -R go-rwx "$OUT"
find "$DEST" -mindepth 1 -maxdepth 1 -type d -mtime +"$KEEP_DAYS" -exec rm -rf {} +
echo "== 완료: $(du -sh "$OUT" | cut -f1), DB 테이블 $N 개 (보관 $KEEP_DAYS 일)"
