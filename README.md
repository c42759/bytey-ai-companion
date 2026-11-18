# Bytey AI Companion

Always-on-top **Dynamic Island** desktop AI assistant for **GNOME Shell 50 (Wayland / PipeWire)**, split into a portable Python core daemon (`core-daemon/`, package `bytey_ai_companion`), a thin GNOME Shell extension (`gnome-extension/bytey-ai-companion@local/`), and a native GTK4 / Libadwaita Settings UI (`settings-ui/`, package `bytey_settings`).

---

## Architecture Overview

- **`core-daemon/` (`bytey_ai_companion`)**: Python `asyncio` + HTTP/WebSocket daemon (`127.0.0.1:8765`) handling PipeWire audio capture, VAD/wake-word/STT, multi-provider LLM routing, MCP tool hosting, sentence-chunked TTS playback, and SQLite conversation history (`~/.local/share/bytey-ai-companion/bytey.db`).
- **`gnome-extension/bytey-ai-companion@local/`**: GNOME Shell 50 ESM overlay (`Main.layoutManager.addTopChrome`) rendering procedural Cairo eyes, Fixed 9-point or Floating drag positioning, left-click transcript drawer, and right-click context menu.
- **`settings-ui/` (`bytey_settings`)**: Native GTK4 / Libadwaita (`Adw.PreferencesWindow`) settings panel talking to the daemon REST API (with offline fallback to `~/.config/bytey-ai-companion/config.json`).

---

## Setup

### 1. Base Runtime Requirements

System packages (Fedora / Ubuntu / Arch equivalents):
- Python 3.11+ (tested on Python 3.14)
- PyGObject (`python3-gobject`), GTK4, Libadwaita (`libadwaita`)
- PipeWire tools (`pipewire-utils` for `pw-record` / `pw-play`)
- GNOME Shell 45–50 (`libsoup3`, `gjs`)

Required Python packages (when not already provided by system packages):
```bash
pip install fastapi uvicorn websockets pydantic httpx python-dotenv keyring mcp google-genai
```

### 2. Optional Voice & Wake-Word Packages

The daemon runs with graceful fallback in text-only / PipeWire CLI mode if ML audio packages are absent. To enable local Silero VAD, open-vocabulary wake-word detection, and local `faster-whisper` STT:

```bash
pip install faster-whisper silero-vad rapidfuzz sounddevice numpy
```

### 3. Secrets & `.env` Usage

Never store API keys in `~/.config/bytey-ai-companion/config.json`. Provide secrets in one of two ways:

1. **`.env` file** (copy template to workspace root or `~/.config/bytey-ai-companion/.env`; legacy `~/.config/ai-pet/.env` is also supported):
   ```bash
   cp .env.example .env
   # or for systemd --user service:
   mkdir -p ~/.config/bytey-ai-companion
   cp .env.example ~/.config/bytey-ai-companion/.env
   chmod 600 ~/.config/bytey-ai-companion/.env
   ```
   Supported keys: `GEMINI_API_KEY`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `CUSTOM_LLM_API_KEY`, `ELEVENLABS_API_KEY`, `KOKORO_API_KEY`, `STT_API_KEY`.

2. **Settings UI / OS Keyring (`libsecret`)**:
   Enter API keys directly in **AI & MCP**, **Voice Input**, or **Voice Output** tabs and click apply (`PUT /api/v1/secrets`).

---

## GNOME Shell Extension Installation

Symlink the extension into your user GNOME Shell extensions directory and enable it:

```bash
mkdir -p ~/.local/share/gnome-shell/extensions
ln -sfn /home/c42759/Git/pet/gnome-extension/bytey-ai-companion@local ~/.local/share/gnome-shell/extensions/bytey-ai-companion@local
gnome-extensions enable bytey-ai-companion@local
```

> On Wayland, log out and back in once after first linking a new extension so GNOME Shell discovers `bytey-ai-companion@local`.

---

## `systemd --user` Service

Install and start the background daemon user unit:

```bash
mkdir -p ~/.config/systemd/user
ln -sfn /home/c42759/Git/pet/systemd/bytey-ai-companion-daemon.service ~/.config/systemd/user/bytey-ai-companion-daemon.service
systemctl --user daemon-reload
systemctl --user enable --now bytey-ai-companion-daemon.service
```

Useful commands:
```bash
# Check status and logs
systemctl --user status bytey-ai-companion-daemon.service
journalctl --user -u bytey-ai-companion-daemon.service -f

# Stop / restart manually
systemctl --user stop bytey-ai-companion-daemon.service
systemctl --user restart bytey-ai-companion-daemon.service
```

`bytey-ai-companion-daemon.service` uses `Restart=on-failure`, so choosing **Exit** in the island right-click menu cleanly shuts down the daemon (exit code `0`) without auto-restarting.

---

## Launching the Settings UI

You can open Settings from the island right-click menu (**Configuration**) or directly from a terminal:

```bash
python3 /home/c42759/Git/pet/settings-ui/run_settings.py
```

If the daemon is stopped, the Settings UI shows an offline banner and reads/writes non-secret settings directly in `~/.config/bytey-ai-companion/config.json`.

---

## Migrating from AI Pet

If upgrading from an earlier `ai-pet` installation, switch your `systemd --user` unit and GNOME Shell extension symlinks to Bytey AI Companion:

```bash
# 1. Switch systemd --user daemon unit
systemctl --user disable --now ai-pet-daemon.service || true
rm -f ~/.config/systemd/user/ai-pet-daemon.service
ln -sfn /home/c42759/Git/pet/systemd/bytey-ai-companion-daemon.service ~/.config/systemd/user/bytey-ai-companion-daemon.service
systemctl --user daemon-reload
systemctl --user enable --now bytey-ai-companion-daemon.service

# 2. Switch GNOME Shell extension symlink
gnome-extensions disable ai-pet@local || true
rm -f ~/.local/share/gnome-shell/extensions/ai-pet@local
ln -sfn /home/c42759/Git/pet/gnome-extension/bytey-ai-companion@local ~/.local/share/gnome-shell/extensions/bytey-ai-companion@local
gnome-extensions enable bytey-ai-companion@local
```

> On Wayland, log out and back in once after linking `bytey-ai-companion@local` so GNOME Shell picks up the new extension UUID.

### Automatic Legacy Fallback

Existing user configuration, history, environment overrides, and keyring secrets continue to work automatically when the new Bytey paths or keys are not yet present:
- **Config & `.env`**: Falls back to `~/.config/ai-pet/config.json` and `~/.config/ai-pet/.env` when `~/.config/bytey-ai-companion/` equivalents do not exist.
- **SQLite Database**: Falls back to `~/.local/share/ai-pet/pet.db` when `~/.local/share/bytey-ai-companion/bytey.db` does not exist.
- **Environment Variables**: `BYTEY_*` environment variables take precedence, with automatic fallback to legacy `AI_PET_*` variables.
- **OS Keyring (`libsecret`)**: Checks Bytey keyring entries first and falls back to legacy `ai-pet` `libsecret` entries.

---

## Testing

### Python Unit & Integration Tests
```bash
PYTHONPATH=/home/c42759/Git/pet/core-daemon python3 -m unittest discover -s /home/c42759/Git/pet/tests -v
```

Or with `pytest`:
```bash
PYTHONPATH=/home/c42759/Git/pet/core-daemon pytest /home/c42759/Git/pet/tests
```

### Nested GNOME Shell Wayland Verification
Test the GNOME Shell extension in an isolated nested Wayland session without affecting your active desktop:
```bash
dbus-run-session -- gnome-shell --nested --wayland
```
Inside the nested shell, enable `bytey-ai-companion@local` and monitor logs with `journalctl --user -f` or stderr.
