#!/usr/bin/env bash
# G0 stage 2, step 10 (Track B spike): install the container and cluster tooling.
#
# Oracle Linux 9 / UEK. Idempotent: every step checks before acting, so re-running is safe.
# Versions are pinned and printed, because the spike's result is only meaningful with them.
set -euo pipefail

K3D_VERSION=v5.7.5
KUBECTL_VERSION=v1.31.3

log() { printf '\n=== %s\n' "$*"; }

log "docker engine"
if ! command -v docker >/dev/null; then
  sudo dnf -y install dnf-plugins-core
  sudo dnf config-manager --add-repo https://download.docker.com/linux/centos/docker-ce.repo
  sudo dnf -y install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
else
  echo "already installed: $(docker --version)"
fi
sudo systemctl enable --now docker
sudo usermod -aG docker "$USER" || true     # takes effect in a new login shell; this script uses sudo

log "kubectl ${KUBECTL_VERSION}"
if ! command -v kubectl >/dev/null; then
  curl -fsSLo /tmp/kubectl "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/amd64/kubectl"
  sudo install -o root -g root -m 0755 /tmp/kubectl /usr/local/bin/kubectl
  rm -f /tmp/kubectl
else
  echo "already installed: $(kubectl version --client --output=yaml | grep gitVersion | head -1)"
fi

log "k3d ${K3D_VERSION}"
# the upstream installer lives on raw.githubusercontent.com, which this VM cannot reach
# (verified 2026-11-14: connection times out, while github.com release assets return 200),
# so the binary is fetched directly from the release instead of piping a remote script
if ! command -v k3d >/dev/null; then
  curl -fsSLo /tmp/k3d "https://github.com/k3d-io/k3d/releases/download/${K3D_VERSION}/k3d-linux-amd64"
  sudo install -o root -g root -m 0755 /tmp/k3d /usr/local/bin/k3d
  rm -f /tmp/k3d
else
  echo "already installed: $(k3d version | head -1)"
fi

log "versions"
sudo docker --version
kubectl version --client 2>/dev/null | head -2
k3d version | head -2
