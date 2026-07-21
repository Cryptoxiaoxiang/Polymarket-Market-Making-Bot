#!/usr/bin/env bash
set -euo pipefail

INSTALL_DIR="${POLYMM_INSTALL_DIR:-/opt/polymarket-mm-bot}"
SERVICE_NAME="polymarket-mm-bot"
SERVICE_USER="polymm"
PYTHON_BIN="${POLYMM_PYTHON:-python3}"
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run this installer as root: sudo ./deploy/install-vps.sh" >&2
  exit 1
fi

"${PYTHON_BIN}" - <<'PY'
import sys

if sys.version_info < (3, 11):
    raise SystemExit("Python 3.11+ is required; set POLYMM_PYTHON to the correct binary")
PY

was_active=0
if systemctl is-active --quiet "${SERVICE_NAME}.service"; then
  was_active=1
  systemctl stop "${SERVICE_NAME}.service"
fi

if ! getent passwd "${SERVICE_USER}" >/dev/null; then
  useradd --system --home-dir "${INSTALL_DIR}" --shell /usr/sbin/nologin "${SERVICE_USER}"
fi

install -d -o root -g "${SERVICE_USER}" -m 0750 "${INSTALL_DIR}"
tar \
  --exclude='__pycache__' \
  --exclude='*.pyc' \
  --exclude='.pytest_cache' \
  --exclude='.ruff_cache' \
  -cf - \
  -C "${SOURCE_DIR}" \
  poly_mm pyproject.toml README.md config.example.toml .env.example \
  | tar -xf - -C "${INSTALL_DIR}"

if [[ ! -e "${INSTALL_DIR}/config.toml" ]]; then
  install -o root -g "${SERVICE_USER}" -m 0640 \
    "${SOURCE_DIR}/config.example.toml" "${INSTALL_DIR}/config.toml"
fi
if [[ ! -e "${INSTALL_DIR}/.env" ]]; then
  install -o root -g "${SERVICE_USER}" -m 0640 \
    "${SOURCE_DIR}/.env.example" "${INSTALL_DIR}/.env"
  sed -i \
    's|^POLYMARKET_ORDER_JOURNAL_PATH=.*|POLYMARKET_ORDER_JOURNAL_PATH=/var/lib/polymarket-mm-bot/orders.json|' \
    "${INSTALL_DIR}/.env"
fi

"${PYTHON_BIN}" -m venv "${INSTALL_DIR}/.venv"
"${INSTALL_DIR}/.venv/bin/python" -m pip install --upgrade pip
"${INSTALL_DIR}/.venv/bin/python" -m pip install "${INSTALL_DIR}"

chown -R root:root "${INSTALL_DIR}/poly_mm" "${INSTALL_DIR}/.venv"
chmod -R go-w "${INSTALL_DIR}/poly_mm" "${INSTALL_DIR}/.venv"
chown root:root \
  "${INSTALL_DIR}/pyproject.toml" \
  "${INSTALL_DIR}/README.md" \
  "${INSTALL_DIR}/config.example.toml" \
  "${INSTALL_DIR}/.env.example"
chown root:"${SERVICE_USER}" "${INSTALL_DIR}/config.toml" "${INSTALL_DIR}/.env"
chmod 0640 "${INSTALL_DIR}/config.toml" "${INSTALL_DIR}/.env"

install -o root -g root -m 0644 \
  "${SOURCE_DIR}/deploy/polymarket-mm-bot.service" \
  "/etc/systemd/system/${SERVICE_NAME}.service"
systemctl daemon-reload
systemctl enable "${SERVICE_NAME}.service"

if [[ "${was_active}" -eq 1 ]]; then
  systemctl start "${SERVICE_NAME}.service"
  echo "Updated and restarted ${SERVICE_NAME}.service"
else
  echo "Installed ${SERVICE_NAME}.service but did not start it."
  echo "Edit ${INSTALL_DIR}/.env and ${INSTALL_DIR}/config.toml, run preflight, then start it."
fi
