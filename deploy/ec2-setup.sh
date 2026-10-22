#!/usr/bin/env bash
# One-time preparation of a fresh Ubuntu 24.04 EC2 instance: Docker Engine, the
# Compose plugin, a little swap, and automatic security updates.
#
#   curl -fsSL <raw url of this file> | sudo bash      # or: sudo bash ec2-setup.sh
#
# Safe to run twice.
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "Run as root: sudo bash $0" >&2
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y ca-certificates curl git unattended-upgrades

# Docker's own repository — Ubuntu's docker.io package lags and has no Compose v2.
install -m 0755 -d /etc/apt/keyrings
if [[ ! -f /etc/apt/keyrings/docker.asc ]]; then
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
fi
. /etc/os-release
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable" \
  > /etc/apt/sources.list.d/docker.list
apt-get update -y
apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
systemctl enable --now docker

# Let the default user run docker without sudo (takes effect at next login).
DEFAULT_USER="${SUDO_USER:-ubuntu}"
if id "$DEFAULT_USER" >/dev/null 2>&1; then
  usermod -aG docker "$DEFAULT_USER"
fi

# 2 GB of swap: building the image on a small instance can run out of memory.
if ! swapon --show | grep -q /swapfile; then
  fallocate -l 2G /swapfile
  chmod 600 /swapfile
  mkswap /swapfile
  swapon /swapfile
  echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

# The organisation's day is measured in its own timezone by the app; the host
# clock only has to be right, which chrony on Ubuntu already keeps it.
dpkg-reconfigure -f noninteractive unattended-upgrades

echo
echo "Docker $(docker --version | awk '{print $3}' | tr -d ,) and Compose $(docker compose version --short) are installed."
echo "Log out and back in, then follow DEPLOY.md from step 4."
