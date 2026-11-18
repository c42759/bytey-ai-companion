#!/usr/bin/env python3
"""Entrypoint to launch the Bytey AI Companion GTK4 / Libadwaita Settings UI."""

from __future__ import annotations

import os
import sys
from pathlib import Path

SETTINGS_DIR = Path(__file__).resolve().parent
if str(SETTINGS_DIR) not in sys.path:
    sys.path.insert(0, str(SETTINGS_DIR))

from bytey_settings.window import SettingsApplication  # noqa: E402


def main() -> int:
    daemon_url = os.environ.get("BYTEY_DAEMON_URL") or os.environ.get("AI_PET_DAEMON_URL") or "http://127.0.0.1:8765"
    app = SettingsApplication(daemon_url=daemon_url)
    return app.run(sys.argv)


if __name__ == "__main__":
    raise SystemExit(main())
