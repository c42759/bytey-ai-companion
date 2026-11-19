# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Native GNOME Extension Preferences (`prefs.js`) and GSettings schema `org.gnome.shell.extensions.bytey-ai-companion` (`daemon-url`) for extensions.gnome.org (EGO) compliance.
- Extension icon suite (`icons/bytey-ai-companion.svg`, `icons/bytey-ai-companion-symbolic.svg`, and `icons/bytey-ai-companion-icon-128.png`) with `48px` brand icon display in `prefs.js`.
- EGO packaging and validation script `./gnome-extension/build.sh` producing `bytey-ai-companion@c42759.gitlab.com.shell-extension.zip` (13-file allowlist, `glib-compile-schemas --strict --dry-run`, SVG XML validation, optional `node --check`).
- `CONTRIBUTING.md`, `CHANGELOG.md`, and `Contributors` showcase in `README.md`.

### Changed
- Standardized extension UUID to `bytey-ai-companion@c42759.gitlab.com` targeting GNOME Shell `48`, `49`, and `50` with `GPL-3.0-or-later` `LICENSE`, `Soup?version=3.0`, and `GLib.Source.remove` lifecycle cleanup.
- Restructured `README.md` with EGO publishing instructions, icon header, supported GNOME versions table, and contributor details.

## [0.1.0] - 2026-11-18

### Added
- Initial **Bytey AI Companion** architecture: Python `asyncio` + HTTP/WebSocket daemon (`core-daemon/`), GNOME Shell Dynamic Island extension with procedural Cairo eyes, and GTK4 / Libadwaita Settings UI (`settings-ui/`) (`630be88`).
- Kokoro TTS API integration and improved island/drawer layout (`36c4efa`).
- PipeWire audio capture (`pw-record`/`pw-play`), VAD speech-onset handling, and `faster-whisper` STT processing with hotwords and prompt hints (`31685b6`).
- 10-second cursor-idle eye wandering animation (`59ee5c8`).

### Changed
- Rebranded assistant from **Nova** / **AI Pet** to **Bytey AI Companion** (`bytey_ai_companion`, `bytey_settings`, `bytey-ai-companion-daemon.service`) and updated system prompt (`d8453b8`, `c1401a2`).

### Fixed
- Gemini provider connection and streaming/tool-calling integration via `google-genai` (`bb96c53`).
