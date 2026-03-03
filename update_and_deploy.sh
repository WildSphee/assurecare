#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_SOURCE="${SCRIPT_DIR}/deploy/systemd/assure_dynamic_bot.service"
SERVICE_NAME="assure_dynamic_bot.service"
SERVICE_TARGET="/etc/systemd/system/${SERVICE_NAME}"

if [[ ! -f "${SERVICE_SOURCE}" ]]; then
  echo "Service file not found: ${SERVICE_SOURCE}" >&2
  exit 1
fi

cd "${SCRIPT_DIR}"

git pull origin main
sudo cp "${SERVICE_SOURCE}" "${SERVICE_TARGET}"
sudo systemctl daemon-reload
sudo systemctl restart "${SERVICE_NAME}"

echo "Updated repo and restarted ${SERVICE_NAME}."
