#!/usr/bin/env bash
# Installs Docker Engine, the compose plugin and the NVIDIA Container Toolkit inside a WSL 2
# Ubuntu distro, so containers with GPU access can run without Docker Desktop.
#
# Run from Windows (no password needed; WSL lets you run as root in your own distro):
#   wsl -d Ubuntu -u root -- bash -c "tr -d '\r' < /mnt/c/path/to/docker/install-docker-wsl.sh | bash -s -- YOUR_WSL_USERNAME"
#
# If you later install Docker Desktop, remove this engine first (apt-get remove docker-ce) to
# avoid two daemons fighting over the docker socket.
set -euo pipefail
TARGET_USER="${1:-${SUDO_USER:-}}"
export DEBIAN_FRONTEND=noninteractive

echo "== apt prerequisites"
apt-get update -qq
apt-get install -y -qq ca-certificates curl gnupg >/dev/null
install -m 0755 -d /etc/apt/keyrings
. /etc/os-release

echo "== Docker apt repository"
curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable" \
  > /etc/apt/sources.list.d/docker.list

echo "== NVIDIA Container Toolkit apt repository"
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | gpg --dearmor --yes -o /etc/apt/keyrings/nvidia-container-toolkit.gpg
curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
  | sed 's#deb https://#deb [signed-by=/etc/apt/keyrings/nvidia-container-toolkit.gpg] https://#g' \
  > /etc/apt/sources.list.d/nvidia-container-toolkit.list

echo "== installing packages"
apt-get update -qq
apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin nvidia-container-toolkit >/dev/null

echo "== configuring the NVIDIA runtime for Docker"
nvidia-ctk runtime configure --runtime=docker >/dev/null
systemctl enable docker >/dev/null 2>&1 || true
systemctl restart docker

if [ -n "${TARGET_USER}" ] && id "${TARGET_USER}" >/dev/null 2>&1; then
  usermod -aG docker "${TARGET_USER}"
  echo "== added ${TARGET_USER} to the docker group"
fi

echo "== versions"
docker --version
docker compose version
nvidia-ctk --version | head -1
echo "== INSTALL DONE"
