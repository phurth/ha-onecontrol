#!/usr/bin/env bash
# Deploy ha-onecontrol to Home Assistant instance via SSH
#
# Usage: ./scripts/deploy.sh [host] [port]
#   host: SSH host (default: root@10.115.19.131)
#   port: SSH port (default: 22222)

set -euo pipefail

HOST="${1:-root@10.115.19.131}"
PORT="${2:-22222}"
COMPONENT_DIR="custom_components/onecontrol"
REMOTE_DIR="/config/custom_components/onecontrol"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

echo "==> Deploying onecontrol to ${HOST}:${PORT}"
echo "    Source: ${PROJECT_DIR}/${COMPONENT_DIR}"
echo "    Target: ${HOST}:${REMOTE_DIR}"

# Ensure remote directory exists
ssh -p "${PORT}" "${HOST}" "mkdir -p ${REMOTE_DIR}/protocol ${REMOTE_DIR}/translations"

# Sync files
rsync -avz --delete \
  -e "ssh -p ${PORT}" \
  "${PROJECT_DIR}/${COMPONENT_DIR}/" \
  "${HOST}:${REMOTE_DIR}/"

echo "==> Files deployed. Restarting HA core..."
ssh -p "${PORT}" "${HOST}" "ha core restart"

echo "==> Done. Monitor logs with:"
echo "    ssh -p ${PORT} ${HOST} 'ha core logs -f' | grep -i onecontrol"
