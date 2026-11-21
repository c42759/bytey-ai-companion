# Bytey AI Companion

<div align="center">
  <img src="./docs/banner.svg" alt="Bytey AI Companion Banner" width="100%" />
  <p><strong>Always-on-top Dynamic Island desktop AI assistant for GNOME Shell 48–51 (Wayland / PipeWire)</strong></p>
</div>

---

## Features

### Overlay & Visuals
- **Always-On-Top Dynamic Island**: Native GNOME Shell compositor overlay (`Main.layoutManager.addTopChrome`) pinned above all windows on Wayland, with multi-monitor selection.
- **Flexible Placement Modes**: Choose **Fixed** (9-point screen alignment grid with X/Y pixel offsets), **Floating** (mouse drag-and-drop with persisted coordinates), or **Notch** (MacBook-style concave-flare silhouette centered flush against the GNOME top bar).
- **Procedural Cairo Eyes & Animations**: Vector eyes reacting to daemon states (`IDLE`, `LISTENING`, `THINKING`, `TOOL_CALLING`, `SPEAKING`, `MUTED`, `ERROR`) and LLM `[emotion:<tag>]` expressions (`happy`, `curious`, `surprised`, `sleepy`, `error`), with live cursor tracking, 10-second idle screen wandering, and a circling rainbow pill border while thinking.
- **Customizable Appearance & Animation Modes**: Configure eye, background, and border colors (with one-click reset to defaults) and switch between `Full`, `State-Only`, or `Disabled` animation modes.

### Interactions & Transcript Drawer
- **Expandable Transcript Drawer (Left-Click)**: Opens an anchored popover displaying SQLite-persisted conversation history, a text input bar for silent prompting, and a **Clear / New Chat** action.
- **Tool Badges, Replay & Layout Controls**: View collapsible MCP tool-call status and results inline on assistant messages, replay any message via TTS, switch UI density (`compact`, `comfortable`, `spacious`), resize the drawer with the bottom-right drag grip, and check daemon health via the 3-state connection indicator (`connected`, `error`, `disconnected`).
- **Quick Context Menu (Right-Click)**: Access **Preferences** (`prefs.js`), **Configuration** (standalone Settings UI), quick toggles (**Mute Mic**, **Mute TTS**, **Lock/Float Position**), **New Chat Session**, and **Exit** (cleanly stops the daemon and hides the overlay).

### Voice Input & Output (PipeWire)
- **Open-Vocabulary Wake-Word**: Continuous PipeWire 16 kHz audio capture with Silero VAD and local phonetic matching (`"Hey Bytey"` or any custom `"Hey <name>"` phrase) requiring zero model retraining.
- **Configurable Speech-to-Text (STT) & Follow-Up Listening**: Choose local `faster-whisper` or an OpenAI/custom Whisper-compatible HTTP endpoint, with automatic VAD silence cutoff, manual mic toggle, and continuous follow-up listening after assistant replies.
- **Streaming Text-to-Speech (TTS)**: Sentence-chunked streaming playback with real-time eye amplitude sync and barge-in interruption, supporting **Kokoro** (HTTP + voice discovery), **Piper** (local ONNX/CLI synthesis with model download plus speed, pitch, noise, and speaker controls), **ElevenLabs** (streaming API + voice list), or **Disabled / Muted**.

### AI Providers & MCP Tools
- **Multi-Provider LLM Router**: Connects to **Google Gemini** (`google-genai`), **OpenAI**, **Ollama**, **Anthropic**, and **Custom OpenAI-Compatible** endpoints with live model discovery.
- **Model-Aware Context Size & Tool Detection**: Automatically detects model tool-calling support (with manual override) and `context_length`, exposing power-of-2 **Context Size** presets capped to the active model's limit.
- **Customizable Companion Identity & System Prompt**: Customize companion name, wake phrase, and system prompt directly in Preferences or Settings UI, with defaults loaded from `default_system_prompt.md`.
- **Full MCP (Model Context Protocol) Host**: Connect `stdio` and `SSE` / `Streamable HTTP` MCP servers via a structured GUI form or raw `mcpServers` JSON editor, with connection testing, per-tool toggles, and optional confirmation prompts before execution.

### Configuration, Backup & Security
- **Dual Native Configuration UIs**: Lightweight GNOME Extension Preferences (`prefs.js`) and standalone GTK4 / Libadwaita Settings UI (`settings-ui`), both synced with the daemon REST API (with offline `config.json` fallback).
- **JSON Import / Export**: Export all non-secret settings to a `.json` file and import a `.json` backup to replace the active configuration.
- **Isolated Secrets & Legacy Fallback**: Stores API keys exclusively in `.env` (`0600`) or the OS keyring (`libsecret`)—never in `config.json`—and automatically falls back to legacy `ai-pet` config/database/keyring paths when Bytey equivalents are absent.

---

## Architecture Overview

- **`core-daemon/` (`bytey_ai_companion`)**: Python `asyncio` + HTTP/WebSocket daemon (`127.0.0.1:8765`, `/api/v1/*` + `/ws`) handling PipeWire audio capture (`pw-record`/`pw-play`), VAD/wake-word/STT, multi-provider LLM routing, MCP tool hosting, sentence-chunked TTS playback, and SQLite conversation history (`~/.local/share/bytey-ai-companion/bytey.db`).
- **`gnome-extension/bytey-ai-companion@c42759.gitlab.com/`**: GNOME Shell 48–51 ESM extension rendering procedural Cairo eyes, left-click expandable transcript drawer (with 3-state connection indicator, tool-call badges, density modes, and resize grip), right-click context menu, and native `prefs.js`.
- **`settings-ui/` (`bytey_settings`)**: Native GTK4 / Libadwaita (`Adw.PreferencesWindow`) settings panel talking to the daemon REST API (with offline fallback to `~/.config/bytey-ai-companion/config.json`).
- **`systemd/`**: `systemd --user` service unit (`bytey-ai-companion-daemon.service`) managing the background Python daemon lifecycle.

---

## Supported GNOME Versions

| GNOME Shell | Session / Audio | Status |
| :--- | :--- | :--- |
| **48** | Wayland / PipeWire | Supported |
| **49** | Wayland / PipeWire | Supported |
| **50** | Wayland / PipeWire | Supported |
| **51** | Wayland / PipeWire | Supported |

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
- GNOME Shell 48–51 (`libsoup3`, `gjs`, `glib-compile-schemas`)

Install the core daemon package (installs required Python dependencies and the `bytey-ai-companion-daemon` command):
```bash
pip install --user -e ./core-daemon
# Or inside an active virtualenv:
# pip install -e ./core-daemon
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

The extension targets **GNOME Shell 48–51** (`bytey-ai-companion@c42759.gitlab.com`).

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

Install and start the background daemon user unit (auto-detects `bytey-ai-companion-daemon` or your active `python3` + repository path):

```bash
./systemd/install-service.sh
```

Or, if `bytey-ai-companion-daemon` is installed in `~/.local/bin`, you can symlink the portable unit directly:
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

# Uninstall service (or add --with-extension to also remove the GNOME extension)
./systemd/uninstall-service.sh
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
./systemd/install-service.sh

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
5. Verify metadata (`uuid`: `bytey-ai-companion@c42759.gitlab.com`, `shell-version`: `48`, `49`, `50`, `51`, `license`: `GPL-3.0-or-later`, `url`: `https://gitlab.com/c42759/bytey`) and submit for review.

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
