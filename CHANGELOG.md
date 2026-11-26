# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.7.1] - 2026-11-26

### Fixed

- Added GFM pipe-table parsing and `St.BoxLayout` table rendering (`parseTableRow()`, `parseTableSeparator()`, `renderMarkdownTable()`) with column alignment, inline markup, zebra striping, and `Clutter.Text.line_alignment` fix in `markdownRenderer.js` and `stylesheet.css` (`f78523d`, `5a4e387`).

## [1.7.0] - 2026-11-26

### Added

- Per-provider AI configuration persistence (`ProviderProfile` and `ProviderConfig.profiles` in `core-daemon/bytey_ai_companion/config.py`) so switching providers and returning restores saved `model`, `base_url`, `context_size`, `focus_window`, `max_tool_rounds`, `temperature`, and `supports_tools_override` across `gnome-extension` (`prefs/aiPage.js`) and `settings-ui` (`window.py`) (`1c9f0f2`).
- On-demand API key reveal via `GET /api/v1/secrets/{key}` (`core-daemon/bytey_ai_companion/main.py`) when clicking the eye icon on `Adw.PasswordEntryRow` in `gnome-extension` (`prefs/widgets.js`, `prefs.js`) and `settings-ui` (`api_client.py`, `window.py`) (`1c9f0f2`).

### Fixed

- Bidirectional real-time model synchronization between `gnome-extension` `prefs`, `settings-ui`, and the Bytey chat drawer over `/ws` `config_updated` events, and hiding stale open `_modelMenuBox` when the active provider or model changes (`52f1fda`).

## [1.6.1] - 2026-11-26

### Changed

- Modularized GNOME Shell extension (`extension.js` -> `daemonClient.js`, `prefs.js` -> `prefs/` with `mcpSection.js`, `transcriptDrawer.js` -> `markdownRenderer.js`, `searchableMenu.js`, `attachmentPicker.js`, `contextRing.js`, `toolCallCard.js`, `sessionSidebar.js`, `islandWidget.js` -> `islandBorderPainter.js`, plus `colorUtils.js` and `sharedUtils.js`) and updated `gnome-extension/build.sh` (`a4cec19`, `b2696fc`, `d334ece`, `e8be5e0`, `a192b53`, `450a5f2`, `a41aeac`).
- Replaced Unicode emoji/glyph UI placeholders with `St.Icon` / `Gtk.Image` and consolidated duplicate CSS/dialog/color/URL helpers (`a4cec19`, `b2696fc`, `d334ece`).
- Removed redundant `_disabled`/`_destroyed` guard flags, unnecessary `try-catch` wrappers, and legacy compatibility fallbacks for GNOME Shell 48–51 (`a4cec19`, `509028a`).

### Fixed

- Tracked GLib idle sources, portal D-Bus subscriptions, and `zenity` subprocess cancellables cleanly across `destroy()` teardown (`a4cec19`).
- Fixed `_renderSessions()` clearing `_copyTimers`, `prefs/visualsPage.js` live-preview `httpJson` error reporting, and `metadata.json` description length (`a7aa9cd`, `509028a`).

## [1.6.0] - 2026-11-26

### Added

- Configurable chat history **Focus Window** (`focus_window`, `0` = full history, `X > 0` = latest `X` history messages + current user message) across `core-daemon` (`config.py`, `storage.py`, `main.py`), `settings-ui` (`api_client.py`, `window.py`), and `gnome-extension` (`prefs.js`) without deleting database messages (`caf8de5`).
- Context window token usage tracking across `LLMRouter.generate_chat()`, SQLite `usage_json` persistence (`storage.py`), `transcript_delta` WebSocket broadcasts, and a Cairo donut ring indicator with hover tooltip in the GNOME Shell transcript drawer (`transcriptDrawer.js`, `stylesheet.css`) (`eb50528`).
- Context-aware emotion capture from message context (`emotions.py`, `router.py`, `main.py`) (`26b4dc7`).

## [1.5.0] - 2026-11-25

### Added

- Rich Markdown rendering in GNOME Shell transcript drawer (`transcriptDrawer.js`, `stylesheet.css`): headings (`#`–`######`), bold (`**`/`__`), italic (`*`/`_`), bold+italic (`***`/`___`), strikethrough (`~~`), inline code spans, `[text](url)` links, unordered/ordered lists with indentation, blockquotes (`>`), and horizontal rules (`---`/`***`/`___`), with safe Pango markup escaping for streaming replies (`422a007`).

### Fixed

- Duplicate MCP server URL validation across `core-daemon` (`schema_sync.py`, `config.py`), `settings-ui` (`window.py`), and `gnome-extension` (`prefs.js`) so no two configured servers can share the same normalized URL (`b039196`).

## [1.4.0] - 2026-11-25

### Added

- Searchable, live-filtering select boxes across `prefs.js` (`makeSearchableComboRow`), `settings-ui/bytey_settings/window.py` (`_make_string_combo`), and GNOME drawer model/workspace menus (`_populateSearchableMenu`) (`72827f8`).

### Changed

- Sorted all model selector lists A-Z case-insensitively across `LLMRouter.discover_models()`, `settings-ui`, `prefs.js`, and drawer model menu (`72827f8`).
- Expanded multiline chat input to 6 lines maximum height (`ENTRY_MAX_LINES = 6`) with `Enter` to send, `Shift+Enter` for newline, visible scrollbar, and cursor auto-scrolling (`72827f8`).

### Fixed

- Fixed `Shift+Enter` newline insertion in GNOME drawer chat input (`ct.insert_text('\n', ct.get_cursor_position())` instead of `insert_unichar(0x0A)`) (`72827f8`).

## [1.3.0] - 2026-11-25

### Added

- Configurable island width (`island_width`, `140–450px`), drawer opacity (`chat_opacity`, `10–100%`), and persistent sidebar toggle (`chat_sidebar_open`) across `core-daemon`, `settings-ui`, and `gnome-extension` (`ec9ef5d`, `f92f8ae`).
- `BYTEY / ECHO` procedural eye expressions (muted, love-blink, annoyed, sleepy), assistant `avatar.png`, and animated activity borders (`THINKING` rainbow, `TOOL_CALLING` 2-tone green, `God Mode` gold) (`ec9ef5d`, `f92f8ae`).

### Changed

- Constrained chat drawer width to `30%–90%` of screen width (`260–3000px` config bounds) and removed box shadows across island states and drawer (`ec9ef5d`, `f92f8ae`).

## [1.2.0] - 2026-11-25

### Added

- File and image attachment support with per-session storage (`<data_dir>/attachments/<session_id>/`), Wayland portal file picker, composer/message attachment chips, and multimodal `images` routing across OpenAI-compatible, Ollama, Gemini, and Anthropic providers (`6bd413d`).
- Interleaved `parts` (`text` / `tool_call`) message persistence in `storage.py` and ordered segment rendering in `transcriptDrawer.js` (`aa46beb`).
- Workspace `.agents` rules and specialized agent skills (`release`, `code-review`, `security`, `testing`, `architecture`, `documentation`, `unslop`, `backend`, `database`) (`d0f6210`).
- README screenshots (`screenshots/1.jpg`–`7.jpg`) (`0521b41`).

### Changed

- Redesigned transcript drawer header, model dropdown, grouped collapsible tool call layout, message timestamps, and composer bar (`aa46beb`, `6bd413d`).

## [1.1.0] - 2026-11-24

### Added

- God Mode and custom subagent orchestration (`agents/registry.py`, `agents/runner.py`) with configurable tool-call iteration limits (`cf8a917`, `37174b1`, `b3ebd4b`).
- Built-in Git MCP toolchain (`bytey_git__*`, `workspace/git_tools.py`) (`4d16376`).
- Custom symbolic SVG action buttons (`bytey-double-check-symbolic.svg`, `bytey-god-mode-symbolic.svg`) (`c4be2eb`).

### Changed

- Removed `.env`, `.env.example`, and `python-dotenv` usage; secrets now persist exclusively in OS keyring (`libsecret`) with read-only `os.environ` fallback (`9b587dc`).

### Fixed

- Hardened CLI subagent and Git MCP execution: process-group termination on cancel/timeout, bounded output streaming, incremental UTF-8 decoding, Git worktree root & option-argument validation, blame line clamping, `agents` settings patch registration, and `long_cli.py` signal handling (`3e9bf93`).

## [1.0.0] - 2026-11-22

### Added

- Initial **Bytey AI Companion** architecture: Python `asyncio` single-port HTTP/WebSocket `IPCServer` (`core-daemon/`), GNOME Shell Dynamic Island extension with procedural Cairo eyes (`gnome-extension/`), GTK4 / Libadwaita Settings UI (`settings-ui/`), systemd user unit, and test suite (`630be88`).
- TTS backends: Kokoro TTS API (`36c4efa`) and local Piper TTS (`piper_provider.py`, `PIPER_VOICE_PRESETS`) (`6e62040`).
- PipeWire audio capture (`pw-record`/`pw-play`), VAD speech-onset handling, and `faster-whisper` STT with hotwords and prompt hints (`31685b6`).
- Eye animations and expressions: 10s cursor-idle wandering (`59ee5c8`), expanded emotion set in `eyeRenderer.js` (`f253fd7`), and improved speaking animation (`d6218a1`).
- GNOME Extensions (EGO) compliance & packaging: native `prefs.js`, GSettings schema `org.gnome.shell.extensions.bytey-ai-companion` (`daemon-url`), icon suite (`bytey-ai-companion.svg`, `bytey-ai-companion-symbolic.svg`, `bytey-ai-companion-icon-128.png`), `./gnome-extension/build.sh`, `GPL-3.0-or-later` `LICENSE`, `CONTRIBUTING.md`, `CHANGELOG.md`, and `README.md` banner & feature list (`4af0312`, `d76d57c`, `9f6f1ec`, `93f0b8f`, `428e5f1`).
- UI and customization:
  - Eye, background, and border color customization (`VisualsConfig`, `prefs.js`) (`e3bbb4c`, `ece8135`).
  - Notch mode (`dcf480f`).
  - Multi-row chat input with send icon (`61e92e4`).
  - Conversation sidebar to switch, rename, and delete conversations (`73851cd`).
  - GNOME theme sync (`b294181`) and auto-hide during GNOME Activities overview (`eff3418`).
  - Auto-accept tool confirmation toggle (`130f910`).
  - GNOME Shell 51 support (`0ce0b4d`).
- AI, skills, and workspace tools:
  - Context size control with `clamp_context_size` (`f2cf06c`).
  - Config export/import (`PUT /api/v1/config`, `ConfigManager.replace_config`) and externalized `default_system_prompt.md` (`014f328`).
  - Skills system (`skills/loader.py`, `skills/runner.py`) with built-in `code-review`, `daily-briefing`, and `summarize-clipboard` skills (`9869530`).
  - Built-in Workspace MCP (`WorkspaceToolchain` in `workspace/tools.py`) with approval flow (`91c8724`), plus `delete_file` and `rename_file` tools (`4176e62`).
  - Built-in MCP Host info endpoint (`GET /api/v1/mcp/host`, `DaemonClient.get_mcp_host_info`) and info panel in `prefs.js` & `window.py` (`c1c7c97`).
- Install and uninstall scripts: `systemd/install-service.sh` (`06f1b10`), `systemd/uninstall-service.sh` (`428e5f1`).

### Changed

- Rebranded assistant from **Nova** / **AI Pet** to **Bytey AI Companion** (`bytey_ai_companion`, `bytey_settings`, `bytey-ai-companion-daemon.service`) (`d8453b8`, `c1401a2`).
- Standardized extension UUID to `bytey-ai-companion@c42759.gitlab.com` (`4af0312`), then renamed extension folder, UUID, and repo links to `bytey-ai-companion@c42759.github.com` (`dc1f655`).
- Organized Voice & AI configs and MCP Servers UI in `prefs.js` (`02d6023`).
- Removed 600px `Adw.Clamp` width limit in `prefs.js` and `window.py` (`_expand_preferences_page`) (`3d578b6`).
- Documented Python 3.15 installation alternatives (`uv` Python 3.14 venv / HTTP Whisper) in `README.md` (`c246466`).

### Fixed

- Gemini provider connection and streaming/tool-calling integration via `google-genai` (`bb96c53`).
- Documentation paths in `README.md` and `CONTRIBUTING.md` (`8a3db76`).
- `STT_API_KEY` priority and `DEFAULT_OPENAI_STT_URL` handling in `capture.py` / `stt.py` (`02d6023`).
- Raised WebSocket `max_incoming_payload_size` to `16 * 1024 * 1024` (`WS_MAX_INCOMING_PAYLOAD`) to prevent MCP payload truncation (`8b47a2e`).
- ElevenLabs connection and voice listing in `prefs.js` (`fcdbf54`).
- `IPCServer.start` compatibility across `websockets` 14/15 (`WsServer.wrap` on Python 3.14 / Fedora 44) and `websockets` 16+ (`WsServer(_create_server, _protocol_handler)` on Python 3.15 / Fedora 45) (`ef0a8a9`, `2196409`).

### Removed

- Removed `PLAN.md` (`5f16fb1`).
- Removed floating mode option from right-click context menu (`f253fd7`).

[Unreleased]: https://github.com/c42759/bytey-ai-companion/compare/v1.7.1...HEAD
[1.7.1]: https://github.com/c42759/bytey-ai-companion/compare/v1.7.0...v1.7.1
[1.7.0]: https://github.com/c42759/bytey-ai-companion/compare/v1.6.1...v1.7.0
[1.6.1]: https://github.com/c42759/bytey-ai-companion/compare/v1.6.0...v1.6.1
[1.6.0]: https://github.com/c42759/bytey-ai-companion/compare/v1.5.0...v1.6.0
[1.5.0]: https://github.com/c42759/bytey-ai-companion/compare/v1.4.0...v1.5.0
[1.4.0]: https://github.com/c42759/bytey-ai-companion/compare/v1.3.0...v1.4.0
[1.3.0]: https://github.com/c42759/bytey-ai-companion/compare/v1.2.0...v1.3.0
[1.2.0]: https://github.com/c42759/bytey-ai-companion/compare/v1.1.0...v1.2.0
[1.1.0]: https://github.com/c42759/bytey-ai-companion/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/c42759/bytey-ai-companion/releases/tag/v1.0.0
