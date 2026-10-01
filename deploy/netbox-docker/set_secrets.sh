#!/bin/bash
# netbox-docker 기본 비밀번호/키를 무작위 값으로 교체 (최초 기동 전에 1회 실행)
set -e
# netbox-docker 폴더 안에서 실행
rnd() { python3 -c "import secrets,string; a=string.ascii_letters+string.digits; print(''.join(secrets.choice(a) for _ in range($1)))"; }
DB=$(rnd 24); RD=$(rnd 24); RC=$(rnd 24); SK=$(rnd 60); PP=$(rnd 60)
sed -i "s|^DB_PASSWORD=.*|DB_PASSWORD=$DB|; s|^REDIS_PASSWORD=.*|REDIS_PASSWORD=$RD|; s|^REDIS_CACHE_PASSWORD=.*|REDIS_CACHE_PASSWORD=$RC|; s|^SECRET_KEY=.*|SECRET_KEY='$SK'|; s|^API_TOKEN_PEPPER_1=.*|API_TOKEN_PEPPER_1='$PP'|" env/netbox.env
sed -i "s|^POSTGRES_PASSWORD=.*|POSTGRES_PASSWORD=$DB|" env/postgres.env
sed -i "s|^REDIS_PASSWORD=.*|REDIS_PASSWORD=$RD|" env/redis.env
sed -i "s|^REDIS_PASSWORD=.*|REDIS_PASSWORD=$RC|" env/redis-cache.env
echo "비밀번호 교체 완료"
