# Bytey AI Companion

<div align="center">
  <img src="./docs/banner.svg" alt="Bytey AI Companion Banner" width="100%" />
  <p><strong>Always-on-top Dynamic Island desktop AI assistant for GNOME Shell 48–50 (Wayland / PipeWire)</strong></p>
</div>

---

## Features

- **Dynamic Island Overlay**: Always-on-top GNOME Shell pill (`Main.layoutManager.addTopChrome`) with Fixed 9-point or Floating drag positioning, multi-monitor support, and customizable island/eye/border colors.
- **Procedural Cairo Eyes & Animations**: Expressive eyes reacting to daemon state (`IDLE`, `LISTENING`, `THINKING`, `TOOL_CALLING`, `SPEAKING`), LLM emotion tags, live cursor tracking, 10-second idle screen wandering, and a circling rainbow pill border while thinking.
- **Voice Wake-Word, STT & TTS via PipeWire**: Local VAD (`silero-vad`) + open-vocabulary phonetic `"Hey Bytey"` wake-word detection, `faster-whisper` or HTTP Whisper STT, continuous follow-up listening, and sentence-chunked Kokoro / ElevenLabs TTS playback with barge-in interruption.
- **Multi-Provider LLM & MCP Tools**: Supports Gemini (`google-genai`), OpenAI, Ollama, Anthropic, and Custom OpenAI-compatible endpoints, plus Model Context Protocol (`stdio` and `SSE`/`HTTP`) tool hosting with per-tool toggles and confirmation prompts.
- **Native Preferences & Full Settings UI**: Built-in GNOME Extension Preferences (`prefs.js` via `org.gnome.shell.extensions.bytey-ai-companion`) for daemon URL configuration, paired with a standalone GTK4 / Libadwaita (`Adw.PreferencesWindow`) Settings UI for API keys, MCP servers, voice testing, and appearance.

---

## Architecture Overview

- **`core-daemon/` (`bytey_ai_companion`)**: Python `asyncio` + HTTP/WebSocket daemon (`127.0.0.1:8765`, `/api/v1/*` + `/ws`) handling PipeWire audio capture (`pw-record`/`pw-play`), VAD/wake-word/STT, multi-provider LLM routing, MCP tool hosting, sentence-chunked TTS playback, and SQLite conversation history (`~/.local/share/bytey-ai-companion/bytey.db`).
- **`gnome-extension/bytey-ai-companion@c42759.gitlab.com/`**: GNOME Shell 48–50 ESM extension rendering procedural Cairo eyes, left-click expandable transcript drawer (with 3-state connection indicator, tool-call badges, density modes, and resize grip), right-click context menu, and native `prefs.js`.
- **`settings-ui/` (`bytey_settings`)**: Native GTK4 / Libadwaita (`Adw.PreferencesWindow`) settings panel talking to the daemon REST API (with offline fallback to `~/.config/bytey-ai-companion/config.json`).
- **`systemd/`**: `systemd --user` service unit (`bytey-ai-companion-daemon.service`) managing the background Python daemon lifecycle.

---

## Supported GNOME Versions

| GNOME Shell | Session / Audio | Status |
| :--- | :--- | :--- |
| **48** | Wayland / PipeWire | Supported |
| **49** | Wayland / PipeWire | Supported |
| **50** | Wayland / PipeWire | Supported |

---

## Building and Installation

Clone the repository and run all commands below from the repository root:

```bash
git clone https://gitlab.com/c42759/bytey.git
cd bytey
```

### 1. Base Runtime Requirements

System packages (Fedora / Ubuntu / Arch equivalents):
- Python 3.11+ (tested on Python 3.14)
- PyGObject (`python3-gobject`), GTK4, Libadwaita (`libadwaita`)
- PipeWire tools (`pipewire-utils` for `pw-record` / `pw-play`)
- GNOME Shell 48–50 (`libsoup3`, `gjs`, `glib-compile-schemas`)

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

### 4. GNOME Shell Extension Installation

The extension targets **GNOME Shell 48–50** (`bytey-ai-companion@c42759.gitlab.com`).

**Standard install (recommended)**:
```bash
./gnome-extension/build.sh
gnome-extensions install gnome-extension/bytey-ai-companion@c42759.gitlab.com.shell-extension.zip --force
gnome-extensions enable bytey-ai-companion@c42759.gitlab.com
```

**Symlink development install**: Compile the GSettings schema in-place first (`schemas/gschemas.compiled` is gitignored and excluded from EGO upload zips), symlink the extension into your user extensions directory, and enable it:

```bash
glib-compile-schemas gnome-extension/bytey-ai-companion@c42759.gitlab.com/schemas
mkdir -p ~/.local/share/gnome-shell/extensions
ln -sfn "$(pwd)/gnome-extension/bytey-ai-companion@c42759.gitlab.com" ~/.local/share/gnome-shell/extensions/bytey-ai-companion@c42759.gitlab.com
gnome-extensions enable bytey-ai-companion@c42759.gitlab.com
```

> On Wayland, log out and back in once after first linking or installing a new extension so GNOME Shell discovers `bytey-ai-companion@c42759.gitlab.com`.

### 5. `systemd --user` Service

Install and start the background daemon user unit:

```bash
mkdir -p ~/.config/systemd/user
ln -sfn "$(pwd)/systemd/bytey-ai-companion-daemon.service" ~/.config/systemd/user/bytey-ai-companion-daemon.service
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

### 6. Launching the Settings UI

You can open standalone full-featured Settings (API keys, MCP JSON editor, voice test, provider/model selection, colors) from the island right-click menu (**Configuration**) or directly from a terminal:

```bash
python3 settings-ui/run_settings.py
```

If the daemon is stopped, the Settings UI shows an offline banner and reads/writes non-secret settings directly in `~/.config/bytey-ai-companion/config.json`.

### 7. Migrating from AI Pet

If upgrading from an earlier `ai-pet` installation, switch your `systemd --user` unit and GNOME Shell extension symlinks to Bytey AI Companion:

```bash
# 1. Switch systemd --user daemon unit
systemctl --user disable --now ai-pet-daemon.service || true
rm -f ~/.config/systemd/user/ai-pet-daemon.service
ln -sfn "$(pwd)/systemd/bytey-ai-companion-daemon.service" ~/.config/systemd/user/bytey-ai-companion-daemon.service
systemctl --user daemon-reload
systemctl --user enable --now bytey-ai-companion-daemon.service

# 2. Switch GNOME Shell extension symlink
gnome-extensions disable ai-pet@local || true
rm -f ~/.local/share/gnome-shell/extensions/ai-pet@local
glib-compile-schemas gnome-extension/bytey-ai-companion@c42759.gitlab.com/schemas
ln -sfn "$(pwd)/gnome-extension/bytey-ai-companion@c42759.gitlab.com" ~/.local/share/gnome-shell/extensions/bytey-ai-companion@c42759.gitlab.com
gnome-extensions enable bytey-ai-companion@c42759.gitlab.com
```

> On Wayland, log out and back in once after linking `bytey-ai-companion@c42759.gitlab.com` so GNOME Shell picks up the new extension UUID.

**Automatic Legacy Fallback**: Existing user configuration, history, environment overrides, and keyring secrets continue to work automatically when the new Bytey paths or keys are not yet present:
- **Config & `.env`**: Falls back to `~/.config/ai-pet/config.json` and `~/.config/ai-pet/.env` when `~/.config/bytey-ai-companion/` equivalents do not exist.
- **SQLite Database**: Falls back to `~/.local/share/ai-pet/pet.db` when `~/.local/share/bytey-ai-companion/bytey.db` does not exist.
- **Environment Variables**: `BYTEY_*` environment variables take precedence, with automatic fallback to legacy `AI_PET_*` variables.
- **OS Keyring (`libsecret`)**: Checks Bytey keyring entries first and falls back to legacy `ai-pet` `libsecret` entries.

---

## Publishing to GNOME Extensions (extensions.gnome.org)

Build an `extensions.gnome.org` (EGO) submission archive with:

```bash
./gnome-extension/build.sh
```

This validates `metadata.json`, SVG icons, GSettings XML (`glib-compile-schemas --strict --dry-run`), and JS syntax (`node --check` if available), then creates `gnome-extension/bytey-ai-companion@c42759.gitlab.com.shell-extension.zip` (gitignored, 13-file allowlist, excluding `schemas/gschemas.compiled`).

To publish on [extensions.gnome.org](https://extensions.gnome.org/):
1. Run `./gnome-extension/build.sh` to generate `gnome-extension/bytey-ai-companion@c42759.gitlab.com.shell-extension.zip`.
2. Sign in to [extensions.gnome.org](https://extensions.gnome.org/) and open **Upload Extension**.
3. Upload `gnome-extension/bytey-ai-companion@c42759.gitlab.com.shell-extension.zip`.
4. Upload `./gnome-extension/bytey-ai-companion@c42759.gitlab.com/icons/bytey-ai-companion-icon-128.png` (`128×128` PNG) as the extension listing icon on the EGO web form.
5. Verify metadata (`uuid`: `bytey-ai-companion@c42759.gitlab.com`, `shell-version`: `48`, `49`, `50`, `license`: `GPL-3.0-or-later`, `url`: `https://gitlab.com/c42759/bytey`) and submit for review.

---

## Usage

- **Left-Click Island**: Opens or closes the anchored **Transcript Drawer** showing SQLite-persisted conversation history, active provider pill, 3-state daemon connection indicator (green connected, red error, black disconnected), tool-call badges, TTS replay, Clear/New Chat, density toggle, bottom-right resize grip, and text/mic input bar.
- **Right-Click Island**: Opens the context menu with quick toggles, **New Chat Session**, **Preferences**, **Configuration**, and **Exit**.
- **Voice & Wake-Word**: Say `"Hey Bytey"` (configurable) or click the drawer mic button to speak; click the mic button again to submit early.
- **Preferences vs Configuration**:
  - **Preferences** (`prefs.js`): Native GNOME Extension Preferences (`org.gnome.shell.extensions.bytey-ai-companion`, e.g. `daemon-url`), also accessible from the GNOME **Extensions** app.
  - **Configuration** (`settings-ui/run_settings.py`): Full GTK4 / Libadwaita Settings UI for API keys, LLM providers/models, MCP servers, voice I/O testing, and island colors.

---

## Testing

### Python Unit & Integration Tests
```bash
PYTHONPATH=core-daemon python3 -m unittest discover -s tests -v
```

Or with `pytest`:
```bash
PYTHONPATH=core-daemon pytest tests
```

### Nested GNOME Shell Wayland Verification
Test the GNOME Shell extension in an isolated nested Wayland session without affecting your active desktop:
```bash
dbus-run-session -- gnome-shell --nested --wayland
```
Inside the nested shell, enable `bytey-ai-companion@c42759.gitlab.com` and monitor logs with `journalctl --user -f` or stderr.

---

## Support & Contributing

- **Repository**: [https://gitlab.com/c42759/bytey](https://gitlab.com/c42759/bytey)
- **Issue Tracker**: [https://gitlab.com/c42759/bytey/-/issues](https://gitlab.com/c42759/bytey/-/issues)
- **Contributing Guide**: [CONTRIBUTING.md](./CONTRIBUTING.md)
- **Changelog**: [CHANGELOG.md](./CHANGELOG.md)

---

## Contributors

<table>
  <tr>
    <td align="center">
      <a href="https://gitlab.com/c42759">
        <img src="https://unavatar.io/gitlab/c42759?fallback=https://www.gravatar.com/avatar/?d=mp" width="80" height="80" alt="Carlos Santos" /><br />
        <sub><b>Carlos Santos</b></sub>
      </a><br />
      <sub>@c42759 • Maintainer &amp; Author</sub>
    </td>
  </tr>
</table>

---

## License

This project is licensed under the **GPL-3.0-or-later** license. See [LICENSE](./gnome-extension/bytey-ai-companion@c42759.gitlab.com/LICENSE) for full details.
