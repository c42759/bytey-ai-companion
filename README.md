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
- **Procedural Cairo Eyes & Animations**: Vector eyes reacting to 7 daemon states (`IDLE`, `LISTENING`, `THINKING`, `TOOL_CALLING`, `SPEAKING`, `MUTED`, `ERROR`) and 10 LLM `[emotion:<tag>]` expressions (`neutral`, `happy`, `love`, `annoyed`, `sleepy`, `focus`, `astonished`, `curious`, `surprised`, `error`), with live cursor tracking, 10-second idle screen wandering, a circling rainbow border while thinking, and a yellow pulsing border when God Mode is active.
- **Customizable Appearance & Animation Modes**: Configure eye, background, and border colors (with one-click reset to defaults), switch between `Full`, `State-Only`, or `Disabled` animation modes, and preview states/emotions live from Preferences or Settings UI.

### Interactions & Transcript Drawer
- **Expandable Transcript Drawer (Left-Click)**: Opens an anchored popover with a collapsible **Chat Sessions Sidebar** (multi-session SQLite history, inline rename, 2-step delete, **New Chat**), markdown and fenced code block rendering with copy buttons, multi-line input (`Shift+Enter` for newline, `Enter` to send), and `/` slash-command skill autocomplete.
- **Header Controls & Status**: Includes sidebar toggle, active **Provider / Model** pill, **Workspace** switcher pill, 3-state daemon connection indicator (`connected`, `error`, `disconnected`), **God Mode** toggle, **Auto-Accept** toggle, and **New Chat** button.
- **Tool Badges, Confirmation Cards, Replay & Layout**: View collapsible MCP tool-call status and results inline on assistant messages, approve or reject file diffs (`propose_edit`) and shell commands (`run_command`) via inline confirmation cards, play/pause/replay assistant messages via TTS, switch UI density (`compact`, `comfortable`, `spacious`), and resize the drawer with the bottom-right drag grip.
- **Quick Context Menu (Right-Click)**: Access **Preferences** (`prefs.js`), quick toggles (**Mute Microphone**, **Mute Voice (TTS)**), **New Chat Session**, and **Exit** (cleanly stops the daemon and hides the overlay).

### Voice Input & Output (PipeWire)
- **Open-Vocabulary Wake-Word**: Continuous PipeWire 16 kHz audio capture (`pw-record` or `sounddevice`) with Silero VAD (plus numpy RMS fallback) and local phonetic/fuzzy matching (`"Hey Bytey"` or any custom `"Hey <name>"` phrase) requiring zero model retraining.
- **Configurable Speech-to-Text (STT) & Follow-Up Listening**: Choose local `faster-whisper`, OpenAI Whisper, or a custom Whisper-compatible HTTP endpoint, with configurable wake sensitivity, VAD silence timeout, manual mic toggle, and continuous follow-up listening after assistant replies.
- **Streaming Text-to-Speech (TTS)**: Sentence-chunked streaming playback via `pw-play` with real-time eye amplitude sync, play/pause/replay, and barge-in interruption, supporting **Kokoro** (HTTP + voice discovery), **Piper** (3-tier local Python/CLI/ONNX synthesis with Hugging Face model download, funny presets, speed/pitch/expressiveness controls, and **Test Voice** preview), **ElevenLabs** (streaming API + voice list), or **Disabled / Muted**.

### AI Providers, Built-in MCP Host & God Mode
- **Multi-Provider LLM Router**: Connects to **Ollama**, **Google Gemini** (`google-genai`), **OpenAI**, **Anthropic**, and **Custom OpenAI-Compatible** endpoints with live model discovery.
- **Model-Aware Context Size & Tool Loop Controls**: Automatically detects model tool-calling support (with **Force Enable Tools** override) and `context_length`, exposes power-of-2 **Context Size** presets capped to the active model's limit, and configures **Max Tool Loops** (`1–100`, default `25`) with older tool-output compaction and loop detection.
- **Built-in Bytey MCP Host (Up to 38 Tools)**:
  - **`bytey_workspace` (9 tools)**: `list_files`, `grep_search`, `read_file`, `propose_edit`, `write_file`, `append_file`, `delete_file`, `rename_file`, `run_command` (in God Mode, the 5 file-mutating tools are omitted in favor of the CLI agent).
  - **`bytey_git` (26 tools)**: 9 read-only (`status`, `diff`, `log`, `show`, `blame`, `branch_list`, `tag_list`, `stash_list`, `remote_list`), 8 write (`init`, `stage`, `unstage`, `commit`, `branch_create`, `checkout`, `stash`, `tag_create`), and 9 always-confirm (`branch_delete`, `merge`, `rebase`, `cherry_pick`, `restore`, `reset`, `fetch`, `pull`, `push`).
  - **`bytey_skill` (2 tools)**: `bytey_skill__load` and `bytey_skill__run_script`.
  - **`bytey_agent` (1 tool)**: `bytey_agent__run` (enabled when God Mode is active).
- **External MCP Servers**: Connect `stdio`, `SSE`, and `Streamable HTTP` MCP servers via a structured GUI form or two-way synced `mcpServers` JSON editor (`${VAR}` and `${BYTEY_WORKSPACE}` expansion), with connection testing, per-tool toggles, and confirmation prompts.
- **God Mode & CLI Agents**: Delegate coding/system tasks via `bytey_agent__run` (or `/god` / `/godmode`) to 7 supported CLI agents (`claude`, `copilot`, `agy`, `gemini`, `codex`, `opencode`, `aider`) with auto-detection, per-agent model & permission modes (`default`, `accept_edits`, `autonomous`), inactivity timeout (`10–1800s`), and **Auto-Accept** / approval gating.
- **Skills & Workspace Management**: Ships with 3 built-in skills (`code-review` `/review`, `daily-briefing` `/briefing`, `summarize-clipboard` `/summarize`), auto-loads workspace skills (`.agents/skills`, `.bytey/skills`, `skills`) and project rules, supports folder/`.zip` skill import, `SKILL.md` editing, and sandboxed script execution (`30s` timeout, `32 KiB` cap).

### Configuration, Backup & Security
- **Dual Native Configuration UIs**: GNOME Extension Preferences (`prefs.js`, 8 Libadwaita pages) and standalone GTK4 / Libadwaita Settings UI (`settings-ui`, 9 Libadwaita pages), both synced with the daemon REST API (with offline `config.json` fallback).
- **JSON Import / Export**: Export non-secret settings (`general`, `provider`, `voice_in`, `voice_out`, `visuals`, `mcp_servers`) to a `.json` file and import a `.json` backup to replace the active configuration.
- **Isolated Secrets & Legacy Fallback**: Stores API keys exclusively in the OS keyring (`libsecret`, `io.github.bytey_ai_companion.Secrets`) with read-only process environment variable fallback—never in `config.json`—and supports automatic fallback to legacy `ai-pet` config/database/keyring entries when Bytey equivalents are absent.

---

## Architecture Overview

- **`core-daemon/` (`bytey_ai_companion`)**: Python `asyncio` daemon serving a single-port localhost HTTP/1.1 REST + WebSocket IPC server (`127.0.0.1:8765`, `/api/v1/*` + `/ws`) handling PipeWire audio capture/playback (`pw-record`/`pw-play`), Silero VAD, wake-word matching, STT, multi-provider LLM routing, built-in Workspace/Git/Skill/Agent tools, external MCP hosting, sentence-chunked TTS, and SQLite conversation history (`~/.local/share/bytey-ai-companion/bytey.db`).
- **`gnome-extension/bytey-ai-companion@c42759.github.com/`**: GNOME Shell 48–51 ESM extension rendering procedural Cairo eyes, left-click expandable transcript drawer (chat sidebar, workspace/provider pills, 3-state connection indicator, God Mode & Auto-Accept toggles, tool-call badges & confirmation cards, density modes, resize grip), right-click context menu, and 8-page `prefs.js`.
- **`settings-ui/` (`bytey_settings`)**: Standalone 9-page GTK4 / Libadwaita (`Adw.PreferencesWindow`) settings app talking to the daemon REST API (with offline fallback to `~/.config/bytey-ai-companion/config.json`).
- **`systemd/`**: `systemd --user` service unit (`bytey-ai-companion-daemon.service`) and install/uninstall scripts managing the background Python daemon lifecycle.

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
git clone https://github.com/c42759/bytey-ai-companion.git
cd bytey-ai-companion
```

### 1. Base Runtime Requirements

System packages (Fedora / Ubuntu / Arch equivalents):
- Python 3.11+ (`requires-python >= 3.11` in `core-daemon/pyproject.toml`)
- PyGObject (`python3-gobject`), GTK4, Libadwaita (`libadwaita`)
- PipeWire tools (`pipewire-utils` for `pw-record` / `pw-play`)
- GNOME Shell 48–51 (`libsoup3`, `gjs`, `glib-compile-schemas`)

Install the core daemon package (installs required Python dependencies and the `bytey-ai-companion-daemon` command):
```bash
pip install --user -e ./core-daemon
# Or inside an active virtualenv:
# pip install -e ./core-daemon
```

Optional extras defined in `core-daemon/pyproject.toml`:
- `[voice]`: `faster-whisper`, `silero-vad`, `rapidfuzz`, `sounddevice`, `piper-tts`
- `[mcp]`: `mcp` (official MCP Python SDK)
- `[dev]`: `pytest`, `pytest-asyncio`

### 2. Optional Voice & Wake-Word Packages

The daemon runs with graceful fallback in text-only / PipeWire CLI mode if ML audio packages are absent. To enable local Silero VAD, open-vocabulary wake-word detection, local `faster-whisper` STT, and local `piper-tts`:

```bash
pip install -e "./core-daemon[voice]"
# Or manually:
# pip install faster-whisper silero-vad rapidfuzz sounddevice piper-tts
```

> **Note for Python 3.15+ systems**: `faster-whisper` depends on native C/C++ Python extensions (`ctranslate2`, `onnxruntime`) that often lag behind newly released Python versions and lack prebuilt binary wheels on PyPI (causing `pip install` to fail with missing-wheel or source-compilation errors).
>
> **Why use a `uv` Python 3.14 virtualenv (recommended for local voice)**: [`uv`](https://docs.astral.sh/uv/) fetches a standalone Python 3.14 interpreter without modifying or downgrading your system Python 3.15. Because `./systemd/install-service.sh` prefers `bytey-ai-companion-daemon` on `PATH`, installing inside a 3.14 venv automatically wires the `systemd --user` service to that venv:
>
> ```bash
> uv venv --python 3.14 ~/.local/share/bytey-venv
> source ~/.local/share/bytey-venv/bin/activate
> pip install -e "./core-daemon[voice]"
> ./systemd/install-service.sh
> ```
>
> **Alternative without Python 3.14**: Install only the base daemon on system Python 3.15 (`pip install --user -e ./core-daemon`) and configure **Speech-to-Text** in Settings to use an OpenAI-compatible HTTP Whisper endpoint (e.g., `whisper.cpp` `whisper-server`, containerized `faster-whisper-server` / `speaches`, or the OpenAI Whisper API).

### 3. Secrets & OS Keyring

Never store API keys in `~/.config/bytey-ai-companion/config.json`. Secrets are persisted in the OS keyring (`libsecret`, schema `io.github.bytey_ai_companion.Secrets`) with read-only process environment variable fallback (`GEMINI_API_KEY`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `CUSTOM_LLM_API_KEY`, `ELEVENLABS_API_KEY`, `KOKORO_API_KEY`, `STT_API_KEY`):

- **Settings UI / Preferences**: Enter API keys directly in **AI & MCP**, **Voice Input**, or **Voice Output** tabs and click apply (`PUT /api/v1/secrets`) to store them in the OS keyring.

### 4. GNOME Shell Extension Installation

The extension targets **GNOME Shell 48–51** (`bytey-ai-companion@c42759.github.com`).

**Build & install (recommended)**:
```bash
./gnome-extension/build.sh -i
gnome-extensions enable bytey-ai-companion@c42759.github.com
```
(Or build the zip with `./gnome-extension/build.sh` and install manually with `gnome-extensions install gnome-extension/bytey-ai-companion@c42759.github.com.shell-extension.zip --force`.)

**Symlink development install**: Compile the GSettings schema in-place first (`schemas/gschemas.compiled` is gitignored and excluded from EGO upload zips), symlink the extension into your user extensions directory, and enable it:

```bash
glib-compile-schemas gnome-extension/bytey-ai-companion@c42759.github.com/schemas
mkdir -p ~/.local/share/gnome-shell/extensions
ln -sfn "$(pwd)/gnome-extension/bytey-ai-companion@c42759.github.com" ~/.local/share/gnome-shell/extensions/bytey-ai-companion@c42759.github.com
gnome-extensions enable bytey-ai-companion@c42759.github.com
```

> On Wayland, log out and back in once after first linking or installing a new extension so GNOME Shell discovers `bytey-ai-companion@c42759.github.com`.

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

# Uninstall service (or add --with-extension to also disable/remove the extension)
./systemd/uninstall-service.sh
```

`bytey-ai-companion-daemon.service` uses `Restart=on-failure` (`RestartSec=3`), so choosing **Exit** in the island right-click menu cleanly shuts down the daemon (exit code `0`) without auto-restarting.

### 6. Launching the Settings UI

Open the standalone 9-page GTK4 / Libadwaita Settings UI directly from a terminal:

```bash
python3 settings-ui/run_settings.py
```

If the daemon is stopped, the Settings UI shows an offline banner and reads/writes non-secret settings directly in `~/.config/bytey-ai-companion/config.json`.

---

## Publishing to GNOME Extensions (extensions.gnome.org)

Build an `extensions.gnome.org` (EGO) submission archive with:

```bash
./gnome-extension/build.sh
```

This validates `metadata.json`, 4 SVG icons, GSettings XML (`glib-compile-schemas --strict --dry-run`), and JS syntax (`node --check` if available), then creates `gnome-extension/bytey-ai-companion@c42759.github.com.shell-extension.zip` (gitignored, 15-file allowlist, excluding `schemas/gschemas.compiled` and `icons/bytey-ai-companion-icon-128.png`). Flags: `-b` (build, default), `-i` (install), `-l` (logout), `-o OUT_DIR`, `-h`.

To publish on [extensions.gnome.org](https://extensions.gnome.org/):
1. Run `./gnome-extension/build.sh` to generate `gnome-extension/bytey-ai-companion@c42759.github.com.shell-extension.zip`.
2. Sign in to [extensions.gnome.org](https://extensions.gnome.org/) and open **Upload Extension**.
3. Upload `gnome-extension/bytey-ai-companion@c42759.github.com.shell-extension.zip`.
4. Upload `./gnome-extension/bytey-ai-companion@c42759.github.com/icons/bytey-ai-companion-icon-128.png` (`128×128` PNG) as the extension listing icon on the EGO web form.
5. Verify metadata (`uuid`: `bytey-ai-companion@c42759.github.com`, `version-name`: `5`, `shell-version`: `48`, `49`, `50`, `51`, `url`: `https://github.com/c42759/bytey-ai-companion`) and submit for review.

---

## Usage

- **Left-Click Island**: Opens or closes the anchored **Transcript Drawer** showing the collapsible chat sessions sidebar, provider/model pill, workspace switcher pill, 3-state daemon connection indicator (green connected, red error, black disconnected), **God Mode** & **Auto-Accept** & **New Chat** icon buttons, tool-call badges & confirmation cards, TTS play/pause/replay, density toggle, bottom-right resize grip, and multi-line text/mic input bar with `/` skill autocomplete.
- **Right-Click Island**: Opens the context menu with **Preferences**, **Mute Microphone**, **Mute Voice (TTS)**, **New Chat Session**, and **Exit**.
- **Voice & Wake-Word**: Say `"Hey Bytey"` (configurable) or click the drawer mic button to speak; click the mic button again to submit early.
- **Preferences vs Settings UI**:
  - **Preferences** (`prefs.js`): Native 8-page GNOME Extension Preferences (`General`, `Visuals & Position`, `Voice`, `AI`, `God Mode`, `Skills`, `Workspace`, `Import / Export`), also accessible from the GNOME **Extensions** app.
  - **Settings UI** (`settings-ui/run_settings.py`): Full 9-page GTK4 / Libadwaita Settings UI (`General`, `AI & MCP`, `God Mode`, `Skills`, `Workspace`, `Voice Input`, `Voice Output`, `Visuals & Position`, `Import / Export`).

---

## Testing

### Python Unit & Integration Tests
```bash
BYTEY_DISABLE_KEYRING=1 PYTHONPATH=core-daemon python3 -m unittest discover -s tests -v
```

Or with `pytest` (requires `[dev]` extras):
```bash
BYTEY_DISABLE_KEYRING=1 PYTHONPATH=core-daemon pytest tests
```

### Nested GNOME Shell Wayland Verification
Test the GNOME Shell extension in an isolated nested Wayland session without affecting your active desktop:
```bash
dbus-run-session -- gnome-shell --nested --wayland
```
Inside the nested shell, enable `bytey-ai-companion@c42759.github.com` and monitor logs with `journalctl --user -f` or stderr.

---

## Support & Contributing

- **Repository**: [https://github.com/c42759/bytey-ai-companion](https://github.com/c42759/bytey-ai-companion)
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

This project is licensed under the **GPL-3.0-or-later** license. See [LICENSE](./gnome-extension/bytey-ai-companion@c42759.github.com/LICENSE) for full details.
