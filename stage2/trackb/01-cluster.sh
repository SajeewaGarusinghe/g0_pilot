#!/usr/bin/env bash
# Create the k3d cluster for the Track B spike. Idempotent.
set -euo pipefail
CLUSTER=${CLUSTER:-trackb}

# docker group membership only takes effect in a new login shell, so re-exec under it
if ! docker ps >/dev/null 2>&1; then exec sg docker -c "$(printf '%q ' "$0" "$@")"; fi

if k3d cluster list | awk '{print $1}' | grep -qx "$CLUSTER"; then
  echo "cluster '$CLUSTER' already exists"
else
  # traefik is not needed: the spike reaches the frontend by port-forward
  k3d cluster create "$CLUSTER" --servers 1 --agents 2 \
    --k3s-arg "--disable=traefik@server:0" --wait
fi
kubectl cluster-info
kubectl get nodes -o wide
