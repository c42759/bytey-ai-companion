# AI Pet (`pet`)

Always-on-top **Dynamic Island** desktop AI assistant for **GNOME Shell 50 (Wayland / PipeWire)**, split into a portable Python core daemon (`core-daemon/`), a thin GNOME Shell extension (`gnome-extension/ai-pet@local/`), and a native GTK4 / Libadwaita Settings UI (`settings-ui/`).

---

## Architecture Overview

- **`core-daemon/`**: Python `asyncio` + HTTP/WebSocket daemon (`127.0.0.1:8765`) handling PipeWire audio capture, VAD/wake-word/STT, multi-provider LLM routing, MCP tool hosting, sentence-chunked TTS playback, and SQLite conversation history (`~/.local/share/ai-pet/pet.db`).
- **`gnome-extension/ai-pet@local/`**: GNOME Shell 50 ESM overlay (`Main.layoutManager.addTopChrome`) rendering procedural Cairo eyes, Fixed 9-point or Floating drag positioning, left-click transcript drawer, and right-click context menu.
- **`settings-ui/`**: Native GTK4 / Libadwaita (`Adw.PreferencesWindow`) settings panel talking to the daemon REST API (with offline fallback to `~/.config/ai-pet/config.json`).

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
pip install fastapi uvicorn websockets pydantic httpx python-dotenv keyring mcp
```

### 2. Optional Voice & Wake-Word Packages

The daemon runs with graceful fallback in text-only / PipeWire CLI mode if ML audio packages are absent. To enable local Silero VAD, open-vocabulary wake-word detection, and local `faster-whisper` STT:

```bash
pip install faster-whisper silero-vad rapidfuzz sounddevice numpy
```

### 3. Secrets & `.env` Usage

Never store API keys in `~/.config/ai-pet/config.json`. Provide secrets in one of two ways:

1. **`.env` file** (copy template to workspace root or `~/.config/ai-pet/.env`):
   ```bash
   cp .env.example .env
   # or for systemd --user service:
   mkdir -p ~/.config/ai-pet
   cp .env.example ~/.config/ai-pet/.env
   chmod 600 ~/.config/ai-pet/.env
   ```
   Supported keys: `GEMINI_API_KEY`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `CUSTOM_LLM_API_KEY`, `ELEVENLABS_API_KEY`, `KOKORO_API_KEY`, `STT_API_KEY`.

2. **Settings UI / OS Keyring (`libsecret`)**:
   Enter API keys directly in **AI & MCP**, **Voice Input**, or **Voice Output** tabs and click apply (`PUT /api/v1/secrets`).

---

## GNOME Shell Extension Installation

Symlink the extension into your user GNOME Shell extensions directory and enable it:

```bash
mkdir -p ~/.local/share/gnome-shell/extensions
ln -sfn /home/c42759/Git/pet/gnome-extension/ai-pet@local ~/.local/share/gnome-shell/extensions/ai-pet@local
gnome-extensions enable ai-pet@local
```

> On Wayland, log out and back in once after first linking a new extension so GNOME Shell discovers `ai-pet@local`.

---

## `systemd --user` Service

Install and start the background daemon user unit:

```bash
mkdir -p ~/.config/systemd/user
ln -sfn /home/c42759/Git/pet/systemd/ai-pet-daemon.service ~/.config/systemd/user/ai-pet-daemon.service
systemctl --user daemon-reload
systemctl --user enable --now ai-pet-daemon.service
```

Useful commands:
```bash
# Check status and logs
systemctl --user status ai-pet-daemon.service
journalctl --user -u ai-pet-daemon.service -f

# Stop / restart manually
systemctl --user stop ai-pet-daemon.service
systemctl --user restart ai-pet-daemon.service
```

`ai-pet-daemon.service` uses `Restart=on-failure`, so choosing **Exit** in the island right-click menu cleanly shuts down the daemon (exit code `0`) without auto-restarting.

---

## Launching the Settings UI

You can open Settings from the island right-click menu (**Configuration**) or directly from a terminal:

```bash
python3 /home/c42759/Git/pet/settings-ui/run_settings.py
```

If the daemon is stopped, the Settings UI shows an offline banner and reads/writes non-secret settings directly in `~/.config/ai-pet/config.json`.

---

## Testing

### Python Unit & Integration Tests
```bash
PYTHONPATH=/home/c42759/Git/pet/core-daemon pytest /home/c42759/Git/pet/tests
```

### Nested GNOME Shell Wayland Verification
Test the GNOME Shell extension in an isolated nested Wayland session without affecting your active desktop:
```bash
dbus-run-session -- gnome-shell --nested --wayland
```
Inside the nested shell, enable `ai-pet@local` and monitor logs with `journalctl --user -f` or stderr.
