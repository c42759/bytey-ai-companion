#!/usr/bin/env bash
set -euo pipefail

UNIT_NAME="bytey-ai-companion-daemon.service"
USER_SYSTEMD_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
TARGET_UNIT="${USER_SYSTEMD_DIR}/${UNIT_NAME}"
EXT_UUID="bytey-ai-companion@c42759.gitlab.com"
EXT_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/gnome-shell/extensions/${EXT_UUID}"

WITH_EXTENSION=false

usage() {
    cat <<'EOF'
Usage: uninstall-service.sh [OPTIONS]

Stop, disable, and remove the Bytey AI Companion systemd --user service.
User configuration (~/.config/bytey-ai-companion/) and history (~/.local/share/bytey-ai-companion/) are preserved.

Options:
  --with-extension  Also disable and remove the GNOME Shell extension
  -h, --help        Show this help message
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --with-extension)
            WITH_EXTENSION=true
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "Unknown option: $1" >&2
            usage >&2
            exit 2
            ;;
    esac
done

systemctl --user disable --now "${UNIT_NAME}" >/dev/null 2>&1 || true
systemctl --user stop "${UNIT_NAME}" >/dev/null 2>&1 || true

if [[ -e "${TARGET_UNIT}" || -L "${TARGET_UNIT}" ]]; then
    rm -f "${TARGET_UNIT}"
    echo "Removed ${TARGET_UNIT}"
else
    echo "No unit file found at ${TARGET_UNIT}"
fi

systemctl --user daemon-reload
systemctl --user reset-failed "${UNIT_NAME}" >/dev/null 2>&1 || true
echo "Uninstalled ${UNIT_NAME}"

if [[ "${WITH_EXTENSION}" == true ]]; then
    if command -v gnome-extensions >/dev/null 2>&1; then
        gnome-extensions disable "${EXT_UUID}" >/dev/null 2>&1 || true
        gnome-extensions uninstall "${EXT_UUID}" >/dev/null 2>&1 || true
    fi
    if [[ -e "${EXT_DIR}" || -L "${EXT_DIR}" ]]; then
        rm -rf "${EXT_DIR}"
    fi
    echo "Removed GNOME Shell extension ${EXT_UUID}"
fi
