#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
CORE_DIR="${REPO_ROOT}/core-daemon"
UNIT_NAME="bytey-ai-companion-daemon.service"
USER_SYSTEMD_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
TARGET_UNIT="${USER_SYSTEMD_DIR}/${UNIT_NAME}"

mkdir -p "${USER_SYSTEMD_DIR}"

# Prefer an installed CLI entrypoint (active venv or ~/.local/bin);
# otherwise run directly from the cloned core-daemon directory with active python3.
DAEMON_BIN="$(command -v bytey-ai-companion-daemon || true)"
if [[ -z "${DAEMON_BIN}" && -x "${HOME}/.local/bin/bytey-ai-companion-daemon" ]]; then
    DAEMON_BIN="${HOME}/.local/bin/bytey-ai-companion-daemon"
fi

if [[ -n "${DAEMON_BIN}" ]]; then
    WORKDIR="%h"
    ENV_LINE=""
    EXEC_START="${DAEMON_BIN}"
else
    PYTHON_BIN="$(command -v python3)"
    WORKDIR="${CORE_DIR}"
    ENV_LINE="Environment=PYTHONPATH=${CORE_DIR}"
    EXEC_START="${PYTHON_BIN} -m bytey_ai_companion.main"
fi

# Remove any stale symlink before writing the resolved unit file
rm -f "${TARGET_UNIT}"

{
    echo "[Unit]"
    echo "Description=Bytey AI Companion Core Daemon"
    echo "After=graphical-session.target pipewire.service"
    echo "PartOf=graphical-session.target"
    echo ""
    echo "[Service]"
    echo "Type=simple"
    echo "WorkingDirectory=${WORKDIR}"
    if [[ -n "${ENV_LINE}" ]]; then
        echo "${ENV_LINE}"
    fi
    echo "ExecStart=${EXEC_START}"
    echo "Restart=on-failure"
    echo "RestartSec=3"
    echo ""
    echo "[Install]"
    echo "WantedBy=graphical-session.target"
} > "${TARGET_UNIT}"

systemctl --user daemon-reload
systemctl --user enable --now "${UNIT_NAME}"

echo "Installed and started ${TARGET_UNIT}"
echo "ExecStart=${EXEC_START}"
