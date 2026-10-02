#!/bin/bash
# =====================================================================================
# [새 서버] CentOS Stream 9/10 · Rocky/AlmaLinux 9 에 Docker CE + 기본 도구 설치
#   sudo bash install_docker.sh
# =====================================================================================
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "sudo 로 실행하세요"; exit 1; }
. /etc/os-release; echo "OS: $PRETTY_NAME"
case "${VERSION_ID%%.*}" in 7|8) echo "CentOS ${VERSION_ID} 은 지원 종료(EOL)된 OS 입니다. Stream 9/10 또는 Rocky/Alma 9 로 설치하세요"; exit 1;; esac

echo "[1/5] 시스템 업데이트·기본 도구"
dnf -y update
dnf -y install dnf-plugins-core git tar curl chrony cronie nginx policycoreutils-python-utils python3 python3-pip net-snmp-utils
systemctl enable --now chronyd crond
timedatectl set-timezone Asia/Seoul

echo "[2/5] 충돌 패키지(podman 등) 제거"
dnf -y remove docker docker-client docker-client-latest docker-common docker-latest docker-latest-logrotate \
  docker-logrotate docker-engine podman runc podman-docker buildah 2>/dev/null || true

echo "[3/5] Docker 저장소 추가"
REPO=https://download.docker.com/linux/centos/docker-ce.repo
dnf config-manager --add-repo "$REPO" 2>/dev/null || dnf config-manager addrepo --from-repofile="$REPO"

echo "[4/5] Docker CE 설치"
dnf -y install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
systemctl enable --now docker
U=${SUDO_USER:-$(logname 2>/dev/null || true)}
if [ -n "$U" ] && [ "$U" != root ]; then
  usermod -aG docker "$U"
  echo "  $U 를 docker 그룹에 추가 — SSH 를 완전히 끊고 다시 접속해야 적용 (확인: id 명령에 docker 가 보여야 함)"
else
  echo "  ※ 일반 계정을 찾지 못해 docker 그룹 추가를 건너뜀 → sudo usermod -aG docker <계정> 을 직접 실행"
fi

echo "[5/5] 확인"
docker --version; docker compose version
docker run --rm hello-world >/dev/null && echo "  Docker 정상 동작"
getenforce 2>/dev/null | sed 's/^/  SELinux: /'
echo "완료. 다음 단계: 방화벽(firewalld)·nginx 설정 → netbox-docker 설치"
