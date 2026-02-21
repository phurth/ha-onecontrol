#!/usr/bin/env bash
# Deploy ha-onecontrol to Home Assistant instance via SSH
#
# Usage: ./scripts/deploy.sh [host] [port]
#   host: SSH host (default: root@10.115.19.131)
#   port: SSH port (default: 22)

set -euo pipefail

HOST="${1:-root@10.115.19.131}"
PORT="${2:-22}"
COMPONENT_DIR="custom_components/onecontrol"
REMOTE_DIR="/config/custom_components/onecontrol"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

echo "==> Deploying onecontrol to ${HOST}:${PORT}"
echo "    Source: ${PROJECT_DIR}/${COMPONENT_DIR}"
echo "    Target: ${HOST}:${REMOTE_DIR}"

# Ensure remote directory exists
ssh -p "${PORT}" "${HOST}" "mkdir -p ${REMOTE_DIR}/protocol ${REMOTE_DIR}/translations"

# Deploy via scp (HAOS doesn't have rsync)
scp -p -P "${PORT}" "${PROJECT_DIR}/${COMPONENT_DIR}"/*.py "${PROJECT_DIR}/${COMPONENT_DIR}"/*.json \
  "${HOST}:${REMOTE_DIR}/"
scp -p -P "${PORT}" "${PROJECT_DIR}/${COMPONENT_DIR}"/protocol/*.py \
  "${HOST}:${REMOTE_DIR}/protocol/"
scp -p -P "${PORT}" "${PROJECT_DIR}/${COMPONENT_DIR}"/translations/*.json \
  "${HOST}:${REMOTE_DIR}/translations/"

echo "==> Files deployed. Restarting HA core..."
ssh -p "${PORT}" "${HOST}" "ha core restart"

echo "==> Done. Monitor logs with:"
echo "    ssh -p ${PORT} ${HOST} 'ha core logs -f' | grep -i onecontrol"
