#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UUID="bytey-ai-companion@c42759.github.com"
EXT_DIR="${SCRIPT_DIR}/${UUID}"
OUT_DIR="${SCRIPT_DIR}"

Help() {
  echo "Usage: $(basename "$0") [-bilh] [-o OUT_DIR] [OUT_DIR]"
  echo "  -b  build the extension (default if no action flag is given)"
  echo "  -i  install the built extension"
  echo "  -l  log out GNOME session afterwards"
  echo "  -o  output directory for the .shell-extension.zip (default: ${SCRIPT_DIR})"
  echo "  -h  show this help message"
}

build=""
install=""
logout=""

while getopts ":bilo:h" option; do
  case "${option}" in
    b) build=1 ;;
    i) install=1 ;;
    l) logout=1 ;;
    o) OUT_DIR="${OPTARG}" ;;
    h)
      Help
      exit 0
      ;;
    *)
      Help >&2
      exit 1
      ;;
  esac
done
shift $((OPTIND - 1))

if [[ $# -gt 0 ]]; then
  OUT_DIR="$1"
fi

if [[ -z "${build}" && -z "${install}" && -z "${logout}" ]]; then
  build=1
fi

OUT_ZIP="${OUT_DIR}/${UUID}.shell-extension.zip"

if [[ -n "${build}" ]]; then
  mkdir -p "${OUT_DIR}"

  python3 - "${EXT_DIR}" "${UUID}" <<'PY'
import json, pathlib, sys
import xml.etree.ElementTree as ET
ext_dir = pathlib.Path(sys.argv[1])
expected_uuid = sys.argv[2]
meta = json.loads((ext_dir / "metadata.json").read_text(encoding="utf-8"))
assert meta.get("uuid") == expected_uuid, f"Unexpected uuid: {meta.get('uuid')}"
assert "version" not in meta, "Deprecated integer 'version' must not be in metadata.json"
assert meta.get("url"), "metadata.json missing url"
assert meta.get("settings-schema"), "metadata.json missing settings-schema"
assert meta.get("shell-version"), "metadata.json missing shell-version"
for icon_rel in (
    "icons/bytey-ai-companion.svg",
    "icons/bytey-ai-companion-symbolic.svg",
    "icons/bytey-double-check-symbolic.svg",
    "icons/bytey-god-mode-symbolic.svg",
):
    icon_path = ext_dir / icon_rel
    assert icon_path.is_file(), f"Missing icon file: {icon_path}"
    root = ET.parse(icon_path).getroot()
    assert root.tag.endswith("svg"), f"Invalid SVG root tag in {icon_rel}: {root.tag}"
PY

  glib-compile-schemas --strict --dry-run "${EXT_DIR}/schemas"

  if command -v node >/dev/null 2>&1; then
    for js in "${EXT_DIR}"/*.js; do
      node --check "${js}"
    done
  fi

  python3 - "${EXT_DIR}" "${OUT_ZIP}" <<'PY'
import pathlib, sys, zipfile
ext_dir = pathlib.Path(sys.argv[1])
out_zip = pathlib.Path(sys.argv[2])
files = [
    "LICENSE",
    "contextMenu.js",
    "extension.js",
    "eyeRenderer.js",
    "icons/bytey-ai-companion-symbolic.svg",
    "icons/bytey-ai-companion.svg",
    "icons/bytey-double-check-symbolic.svg",
    "icons/bytey-god-mode-symbolic.svg",
    "islandWidget.js",
    "metadata.json",
    "positionManager.js",
    "prefs.js",
    "schemas/org.gnome.shell.extensions.bytey-ai-companion.gschema.xml",
    "stylesheet.css",
    "transcriptDrawer.js",
]
with zipfile.ZipFile(out_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
    for rel in sorted(files):
        src = ext_dir / rel
        if not src.is_file():
            raise SystemExit(f"Missing required extension file: {src}")
        zf.write(src, arcname=rel)
print(f"Packed {out_zip} ({len(files)} files)")
PY
fi

if [[ -n "${install}" ]]; then
  if [[ ! -f "${OUT_ZIP}" ]]; then
    echo "Error: missing ${OUT_ZIP}. Run with -b first." >&2
    exit 1
  fi
  echo "Installing extension..."
  gnome-extensions install --force "${OUT_ZIP}"
fi

if [[ -n "${logout}" ]]; then
  echo "Quitting GNOME session..."
  gnome-session-quit --logout --no-prompt
fi
