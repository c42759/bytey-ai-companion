# Contributing to Bytey AI Companion

Contributions, bug reports, and feature suggestions are welcome!

## Ways to Contribute

- **Report Bugs**: Open an issue at [https://gitlab.com/c42759/bytey/-/issues](https://gitlab.com/c42759/bytey/-/issues) and include:
  - Output of `gnome-shell --version`
  - Session type (`echo $XDG_SESSION_TYPE`, e.g., `wayland`)
  - Relevant logs from `journalctl --user -u bytey-ai-companion-daemon.service -n 100` and `journalctl --user -b /usr/bin/gnome-shell`
  - Steps to reproduce
- **Suggest Enhancements**: Open an issue describing the use case, expected behavior, and any relevant provider/audio/GNOME Shell constraints.
- **Submit Merge Requests**: Fork the repository, create a feature branch, and open a Merge Request targeting `main`.

---

## Development Setup

### Prerequisites

- **Python**: `3.11+` (tested on `3.14`)
- **System Libraries**: PyGObject (`python3-gobject`), GTK4, Libadwaita (`libadwaita`)
- **Audio**: PipeWire tools (`pipewire-utils` for `pw-record` / `pw-play`)
- **GNOME Shell**: `48`, `49`, or `50` (`gjs`, `libsoup3`, `glib-compile-schemas`)
- **Optional**: `node` (for JS syntax validation in `build.sh`), `Rsvg` / `pycairo` (for icon rendering/verification), `faster-whisper`, `silero-vad`, `rapidfuzz`, `sounddevice`, `numpy`

---

## Project Structure

| Path | Purpose |
| :--- | :--- |
| `core-daemon/bytey_ai_companion/` | Python `asyncio` + HTTP/WebSocket daemon, audio/VAD/STT/TTS, LLM router, MCP host, SQLite storage |
| `gnome-extension/bytey-ai-companion@c42759.gitlab.com/` | GNOME Shell 48–50 ESM extension, Cairo eyes, Dynamic Island widget, transcript drawer, `prefs.js`, GSettings schema |
| `settings-ui/bytey_settings/` | Native GTK4 / Libadwaita (`Adw.PreferencesWindow`) standalone Settings UI |
| `systemd/` | `systemd --user` service unit (`bytey-ai-companion-daemon.service`) |
| `tests/` | Python unit and integration test suite |
| `gnome-extension/build.sh` | EGO packaging & validation script producing `bytey-ai-companion@c42759.gitlab.com.shell-extension.zip` |

---

## Building and Testing Locally

1. **Run Python unit and integration tests**:
   ```bash
   PYTHONPATH=/home/c42759/Git/pet/core-daemon python3 -m unittest discover -s /home/c42759/Git/pet/tests -v
   ```

2. **Validate & package the GNOME extension**:
   ```bash
   ./gnome-extension/build.sh
   ```

3. **Compile GSettings schema and symlink for local testing**:
   ```bash
   glib-compile-schemas /home/c42759/Git/pet/gnome-extension/bytey-ai-companion@c42759.gitlab.com/schemas
   mkdir -p ~/.local/share/gnome-shell/extensions
   ln -sfn /home/c42759/Git/pet/gnome-extension/bytey-ai-companion@c42759.gitlab.com ~/.local/share/gnome-shell/extensions/bytey-ai-companion@c42759.gitlab.com
   gnome-extensions enable bytey-ai-companion@c42759.gitlab.com
   ```

4. **Test in a nested Wayland GNOME Shell session**:
   ```bash
   dbus-run-session -- gnome-shell --nested --wayland
   ```

5. **Open native Extension Preferences & inspect logs**:
   ```bash
   gnome-extensions prefs bytey-ai-companion@c42759.gitlab.com
   journalctl --user -u bytey-ai-companion-daemon.service -f
   ```

---

## Coding & Review Guidelines

### GNOME Extensions Review Compliance

- **Lifecycle Cleanup**: Every resource created in `enable()` (actors, signals, timers, WebSocket/HTTP sessions, GSettings bindings) MUST be cleanly disconnected, cancelled, and destroyed in `disable()` / `destroy()`.
- **Versioned Imports**: Use `import Soup from 'gi://Soup?version=3.0';` (never unversioned `gi://Soup`).
- **Timer Cleanup**: Always store timer IDs and clear them with `GLib.Source.remove(id)` (resetting the ID variable to `0`/`null`) in `disable()` / `destroy()`.
- **`prefs.js` Isolation**: `prefs.js` runs in a separate GTK4/Adwaita process and MUST NOT import GNOME Shell modules (`gi://St`, `gi://Shell`, `resource:///org/gnome/shell/...`).
- **Portability**: Never hardcode `/home/<user>/` paths in extension, daemon, or UI source files; use `GLib` / `Gio` / `Path.home()` or environment/XDG helpers.
- **No Compiled Schemas in EGO Zip**: `schemas/gschemas.compiled` must remain gitignored and excluded from EGO submission archives.

### Python Daemon & Settings UI

- **No Hardcoded Secrets**: Never commit API keys, tokens, or `.env` files. Use `.env` or `libsecret` keyring entries.
- **Async Safety**: Avoid blocking the `asyncio` event loop in `core-daemon/` (run blocking I/O or ML inference in executors/subprocesses).

---

## License

This project is licensed under **GPL-3.0-or-later**. See [LICENSE](./gnome-extension/bytey-ai-companion@c42759.gitlab.com/LICENSE).
