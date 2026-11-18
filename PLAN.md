# AI Pet (`pet`) — Architecture & Implementation Plan

> **Target Platform (Initial):** GNOME Shell 50 on Wayland (Linux, PipeWire)
> **Cross-Platform Strategy:** Thin GNOME Shell presentation layer coupled over localhost IPC to a portable Python 3 core daemon (`core-daemon/`), enabling future macOS/Windows overlays without rewriting AI, voice, or MCP subsystems.

---

## 1. Description of the Request

Build an always-on-top **AI Pet** inspired by the iOS Dynamic Island: a compact matte-black pill featuring two playful, Grok-style expressive eyes that react to listening, thinking, tool usage, speaking, and emotional state. Users summon the pet hands-free using a customizable wake phrase (e.g., `"Hey <name>"`), hold natural voice or typed conversations backed by configurable AI providers and MCP tools, hear streaming responses via Kokoro or ElevenLabs TTS, inspect or continue full transcripts on left-click, and manage all preferences or exit via right-click.

---

## 2. Possible Use Cases

1. **Hands-Free Voice Assistant While Working**: Trigger `"Hey Nova"` while coding or reading full-screen docs; the island floats above all Wayland windows without stealing focus, captures the prompt until silence, executes tools if needed, and speaks a concise reply.
2. **MCP-Powered Desktop & Dev Copilot**: Attach local `stdio` or remote `SSE` / `Streamable HTTP` MCP servers (filesystem, calendar, GitHub, smart-home) when a tool-capable LLM is selected, watching live tool execution status directly on the island.
3. **Silent Typed Assistant**: Toggle Mic/TTS mute from the right-click menu and left-click the island to expand the anchored conversation drawer for quiet keyboard chatting and transcript review.
4. **Adaptive Workspace Placement**: Anchor the island to a fixed monitor alignment (`top-center`, `bottom-right`, etc. with custom pixel offsets) or unlock Floating mode to drag it anywhere across monitors with saved coordinates.
5. **Zero-Distraction / Low-Power Mode**: Switch eye animations from `Full` to `State-Only` or `Disabled` in Settings while preserving full voice and drawer functionality.

---

## 3. Active Agents

| Agent | Specialty | Assigned Responsibility |
| :--- | :--- | :--- |
| **ArchitectBot** | System Architecture | Hybrid daemon/extension split, IPC schema, config & SQLite data models, portability boundaries |
| **MicroServicesBot** | Python Async & Voice/AI | FastAPI/WebSocket daemon, PipeWire audio, Silero VAD, open-vocabulary wake-word, STT, TTS streaming, LLM router, MCP host |
| **FrontendBot** | GNOME Shell & GTK4/Adw | GNOME Shell 50 ESM extension, Cairo procedural eye engine, Fixed/Float positioning, left-click drawer, right-click menu, Libadwaita Settings UI |
| **SecurityBot** | AppSec & Isolation | Secret handling (`.env` / `libsecret`), loopback token auth, MCP subprocess/HTTP sanitization, tool confirmation gates |
| **TestingBot** | QA & Automation | `pytest` unit/integration suites, WAV audio pipeline tests, nested GNOME 50 Wayland shell harness |
| **DocumentationBot** | Technical Docs | `PLAN.md`, `README.md`, `systemd --user` setup guide, `.agents-memory/` upkeep |

---

## 4. Agent Goals

- **ArchitectBot**: Maintain strict decoupling between UI frontends (`gnome-extension/`, `settings-ui/`) and the OS-agnostic `core-daemon/`, ensuring clean IPC contracts and reversible future ports.
- **MicroServicesBot**: Deliver sub-second voice turnaround using local VAD + tiny STT wake-word matching, non-blocking sentence-chunked TTS playback with barge-in interruption, and reliable multi-provider LLM + MCP execution.
- **FrontendBot**: Render crisp DPI-independent Cairo vector eyes on Wayland top chrome (`addTopChrome`) with minimal idle CPU, intuitive click-vs-drag detection, and native GNOME Libadwaita preferences.
- **SecurityBot**: Prevent credential leaks (never store API keys in plaintext config or logs), restrict daemon IPC to `127.0.0.1`, and enforce optional user confirmation before executing sensitive MCP tools.
- **TestingBot**: Guarantee zero regressions via isolated unit tests (mocked LLM/TTS/MCP/audio) and real Wayland nested compositor checks before merging any phase.
- **DocumentationBot**: Keep developer setup, MCP JSON examples, and session memory files synchronized with implementation reality.

---

## 5. Task Assignment (Parallel vs Sequential Execution Plan)

### Phase 1: Core Daemon Skeleton, Config & Persistence
1. `[SEQUENTIAL]` **ArchitectBot**: Finalize `config.json` schema, SQLite DDL, REST endpoints, and WebSocket event contracts.
2. `[PARALLEL]` **MicroServicesBot**: Scaffold `core-daemon/` FastAPI + WebSocket server, `aiosqlite` storage (`sessions`, `messages`, `tool_calls`), and configuration/secret loader.
3. `[PARALLEL]` **SecurityBot**: Implement OS keyring (`libsecret`) + `.env` secret resolution, secret redaction in API responses/logs, and localhost binding guard.
4. `[SEQUENTIAL]` **TestingBot**: Write and execute `pytest` unit/integration tests for config schema, SQLite persistence, and WebSocket event delivery.

### Phase 2: GNOME Shell 50 Extension & Dynamic Island UI
1. `[PARALLEL]` **FrontendBot**: Implement `gnome-extension/` (`metadata.json` targeting GNOME `50`, `extension.js`, `islandWidget.js`, `eyeRenderer.js`) using `Main.layoutManager.addTopChrome`, Cairo Grok-style eyes, and `Soup` WebSocket client.
2. `[PARALLEL]` **FrontendBot**: Implement `positionManager.js` (Fixed 9-point monitor alignment + offsets, Floating drag with 5px threshold & bounds clamping) and `contextMenu.js` (Right-click menu: `Configuration`, quick toggles, `Exit`).
3. `[SEQUENTIAL]` **TestingBot**: Verify extension load/unload, positioning persistence, and Cairo drawing inside `dbus-run-session -- gnome-shell --nested --wayland`.

### Phase 3: AI Provider Router, MCP Host & Transcript Drawer
1. `[PARALLEL]` **MicroServicesBot**: Build unified AI Provider Router (Gemini, OpenAI, Ollama, Anthropic, Custom OpenAI-Compatible), live model list discovery, automatic tool-capability inspection + manual override, and `[emotion:...]` parser.
2. `[PARALLEL]` **MicroServicesBot**: Build MCP Client Manager (`mcp` Python SDK) supporting `stdio` and `SSE` / `Streamable HTTP`, tool discovery, and structured/JSON config synchronization.
3. `[PARALLEL]` **FrontendBot**: Implement `transcriptDrawer.js` (Left-click anchored expandable popover displaying SQLite history, tool-call badges, `Replay TTS`, `Clear / New Chat`, and text input bar).
4. `[SEQUENTIAL]` **SecurityBot + TestingBot**: Audit MCP env isolation and confirmation prompts; run `pytest` against mocked providers, Ollama `/api/show`, and test MCP servers.

### Phase 4: Voice Pipeline (Wake-Word, STT & Streaming TTS)
1. `[PARALLEL]` **MicroServicesBot**: Implement PipeWire audio stream (`sounddevice`), `silero-vad`, open-vocabulary wake-word detector (`faster-whisper` tiny / `sherpa-onnx` + phonetic fuzzy matching), and configurable STT (Local `faster-whisper` / Whisper HTTP API).
2. `[PARALLEL]` **MicroServicesBot**: Implement sentence-chunked streaming TTS engine for **Kokoro HTTP** and **ElevenLabs API**, real-time playback amplitude emitter, and barge-in interruption on new wake-word or click.
3. `[SEQUENTIAL]` **TestingBot**: Validate wake-word accuracy, VAD silence cutoff, sentence splitting, and barge-in audio cancellation using recorded/synthetic WAV fixtures.

### Phase 5: Native GTK4/Libadwaita Settings UI & Packaging
1. `[PARALLEL]` **FrontendBot**: Build `settings-ui/` (`Adw.PreferencesWindow`) with tabs for General/Prompt, AI & Tools (Provider/Model/MCP Form + Raw JSON), Voice Input, Voice Output, and Appearance & Position.
2. `[PARALLEL]` **DocumentationBot**: Add `systemd/ai-pet-daemon.service` user unit, installation helpers, `README.md`, and final `.agents-memory/progress.md` update.
3. `[SEQUENTIAL]` **TestingBot + SecurityBot**: End-to-end validation of lifecycle (`systemd` start, `Exit` clean shutdown, config round-trip, nested GNOME Wayland UX).

---

## 6. System Architecture & Cross-Platform Strategy

### Why a Hybrid Architecture for GNOME 50 Wayland?
Under **GNOME Shell 50 on Wayland**, standard desktop applications (GTK4, Qt, Electron, Tauri) cannot programmatically read or set global screen `(x, y)` coordinates, nor can they force true always-on-top placement above all native Wayland windows and top bars without compositor cooperation.

To solve this cleanly without sacrificing future OS portability:

```
┌─────────────────────────────────────────────────────────────────────────┐
│ GNOME 50 Wayland Compositor (Linux Presentation Tier — Replaceable)     │
│                                                                         │
│  ┌──────────────────────────────┐    ┌───────────────────────────────┐  │
│  │ gnome-extension/ (GJS ESM)   │    │ settings-ui/ (GTK4 / Adw)     │  │
│  │ • addTopChrome pill overlay  │    │ • Provider / Model / MCP UI   │  │
│  │ • Cairo Grok eye animations  │    │ • Wake-word / STT / TTS prefs │  │
│  │ • Fixed / Float positioning  │    │ • Visual & Position controls  │  │
│  │ • Left-click Drawer & Menu   │    │                               │  │
│  └──────────────┬───────────────┘    └──────────────┬────────────────┘  │
└─────────────────┼───────────────────────────────────┼───────────────────┘
                  │ ws://127.0.0.1:8765/ws            │ http://127.0.0.1:8765/api/v1
┌─────────────────▼───────────────────────────────────▼───────────────────┐
│ core-daemon/ (Portable Python 3 asyncio + FastAPI Engine — 85%+ Code)   │
│                                                                         │
│  • Audio Loop (PipeWire / sounddevice) -> Silero VAD -> Wake-Word / STT │
│  • AI Provider Router (Gemini, OpenAI, Ollama, Anthropic, Custom)       │
│  • MCP Client Host (stdio + SSE / Streamable HTTP)                      │
│  • Streaming TTS Engine (Kokoro HTTP + ElevenLabs + Barge-In)           │
│  • SQLite Conversation Store + Config / Secret Manager                  │
└─────────────────────────────────────────────────────────────────────────┘
```

### Process Lifecycle
- **Auto-Start**: Managed via a `systemd --user` service (`ai-pet-daemon.service`) started at GNOME login, with fallback spawn/reconnect logic if the extension enables before the socket is ready.
- **Clean Exit**: Selecting `Exit` in the Right-Click menu triggers `POST /api/v1/system/exit`, which stops audio capture/TTS, closes active MCP child processes, terminates the daemon gracefully (using `systemctl --user stop` or clean process exit without triggering unwanted auto-restart), and hides the overlay until re-enabled.

---

## 7. Dynamic Island Visuals, Grok-Style Eyes & Animation Engine

### Visual Aesthetic
- **Container**: Matte-black pill (`#0A0A0C`) with subtle `1px` inner border (`rgba(255, 255, 255, 0.08)`), rounded stadium corners (`border-radius: 999px`), and soft drop shadow.
- **Compact Dimensions**: ~`120px × 38px` in `IDLE`; smoothly morphs wider (`~164px × 42px`) when actively `LISTENING`, `TOOL_CALLING`, or `SPEAKING` (showing a subtle mic-level ring or active tool badge).

### Procedural Cairo Vector Eyes (`St.DrawingArea`)
Rendered procedurally in Cairo rather than static sprites for crisp scaling at any monitor DPI and fluid expression transitions:

| State / Emotion | Eye Geometry & Behavior |
| :--- | :--- |
| **`IDLE`** | Two rounded white/silver capsule/oval Grok-inspired eyes, blinking naturally every 3–6 seconds; subtle cursor-tracking glance when `Full` animation mode is enabled. |
| **`LISTENING`** | Eyes widen slightly and brighten; outer pill border pulses softly proportional to live microphone RMS amplitude (`audio_level` events). |
| **`THINKING`** | Eyes tilt slightly upward or perform a smooth horizontal orbital scan while streaming tokens. |
| **`TOOL_CALLING`** | Pupils compress into focused geometric ticks alongside a tiny pill indicator showing active MCP tool name (e.g., `running: search_docs`). |
| **`SPEAKING`** | Eyes pulse/squint rhythmically synced to live TTS playback amplitude envelope. |
| **`MUTED` / `SLEEPY`** | Half-closed downward-curved eyelids with dimmed contrast (`50%` opacity). |
| **`ERROR`** | Brief amber/coral tint (`#FF5A5F`) and asymmetrical alert brow slant before returning to `IDLE`. |
| **LLM Emotions** | `[emotion:happy]` (upward crescent arcs), `[emotion:curious]` (one eye raised/larger), `[emotion:surprised]` (wide circles), `[emotion:sleepy]`, `[emotion:error]`. |

### Animation Modes in Settings
1. **`Full` (Default)**: 60fps `Clutter.Timeline` transitions during state changes, blinks, audio reactivity, and optional mouse cursor look-at (pauses timer between blinks to keep idle CPU near 0%).
2. **`State-Only`**: Redraws static vector eye shapes strictly when daemon state or emotion changes (no continuous frame loop, no cursor look).
3. **`Disabled`**: Static default Grok-style eyes with color/label hint only; zero procedural motion.

---

## 8. Screen Positioning Engine (Fixed vs Floating & Multi-Monitor)

- **Monitor Target**: Defaults to `Main.layoutManager.primaryIndex`; Settings exposes a monitor dropdown populated from `Main.layoutManager.monitors` (e.g., `Primary`, `Monitor 1 (2560x1440)`, `Monitor 2 (1920x1080)`).
- **Fixed Mode**:
  - 9-point alignment anchor within selected monitor work area:
    `top-left`, `top-center`, `top-right`, `center-left`, `center`, `center-right`, `bottom-left`, `bottom-center`, `bottom-right`.
  - Plus signed pixel offsets `offset_x` and `offset_y` (e.g., `top-center` with `offset_y: 12` to sit cleanly beneath the GNOME top bar).
- **Floating Mode**:
  - Mouse press + drag moves the island anywhere across monitor space.
  - **5px Click-vs-Drag Threshold**: Pointer travel `< 5px` on release registers as a **Left-Click** (opens transcript drawer); travel `>= 5px` initiates drag, clamps final `(float_x, float_y)` inside valid monitor bounds so the pet can never be lost off-screen, and persists coordinates via `PATCH /api/v1/config`.
  - When toggled in Settings or right-click menu (`Lock Position / Float`), immediately snaps to the corresponding mode.

---

## 9. Mouse Interactions: Left-Click Transcript Drawer & Right-Click Context Menu

### Left-Click: Anchored Transcript Drawer
Clicking the island (without dragging) opens an anchored floating panel directly below the island (or above it automatically if the island sits in the lower half of the monitor):
- **Header Bar**: Active provider/model pill, current session title, and `Clear / New Chat` button.
- **Scrollable History (`St.ScrollView`)**:
  - Persisted in SQLite (`messages` & `tool_calls`), showing both voice-transcribed prompts (tagged with mic badge) and typed messages, alongside pet responses.
  - **Tool Call Pills**: Collapsible summary chips showing MCP server, tool name, arguments, and execution status (`running`, `ok`, `error`).
  - **Replay TTS Button**: Speaker icon on each pet response to re-synthesize/replay via active TTS engine.
- **Footer Input Bar**: Text input box + `Send` button (for silent prompting without using voice) and a manual `Push-to-Talk` mic button.

### Right-Click: Context Menu (`PopupMenu`)
Right-clicking the island opens a native GNOME `PopupMenu` with:
1. **`Configuration`** — Launches/focuses the native GTK4/Libadwaita Settings window.
2. **Quick Toggles**:
   - `Mute Microphone` (pauses wake-word/VAD capture)
   - `Mute Voice (TTS)` (switches TTS output to silent while keeping text responses)
   - `Lock Position (Fixed)` / `Float Position` (quick switch between Fixed and Floating)
3. **`New Chat Session`** — Starts a fresh conversation context while preserving past sessions in SQLite.
4. **`Exit`** — Sends `POST /api/v1/system/exit`, cleanly shuts down the daemon, and hides the island overlay.

---

## 10. Voice Input Pipeline (Custom Wake-Word + Configurable STT)

### Continuous Capture & Open-Vocabulary Wake-Word
1. **Audio Capture**: `sounddevice` opens a 16kHz, 16-bit mono PCM stream through PipeWire (`30ms` / `512`-sample frames).
2. **Silero VAD**: Runs lightweight local voice activity detection on incoming frames, maintaining a rolling `1.5s` pre-roll ring buffer.
3. **Custom Wake-Word Detection (Zero Retraining)**:
   - User types any wake phrase in Settings (e.g., `"Hey Nova"`, `"Hey Jarvis"`, `"Ok Buddy"`).
   - When Silero VAD detects speech onset, the daemon captures the opening `1.2s–1.8s` utterance chunk and runs a fast local tiny model (`faster-whisper` `tiny.en`/`base` on CPU/GPU or `sherpa-onnx` keyword spotter).
   - Transcribed onset text is normalized (lowercase, stripped punctuation, phonetic normalization via Metaphone/Soundex + `rapidfuzz` ratio threshold configurable via `"Wake Sensitivity"` slider).
   - **Same-Utterance Command Support**: If the user speaks continuously (`"Hey Nova, what's the weather today?"`), the text *after* the wake phrase is preserved immediately rather than forcing the user to pause and repeat!
   - **Barge-In**: Detecting the wake phrase while the pet is speaking immediately cancels TTS playback and switches to `LISTENING`.

### Configurable Prompt STT
Once awake (via wake-word, manual drawer mic button, or optional global hotkey):
- Continues recording until Silero VAD observes configurable trailing silence (`silence_timeout_ms`, default `1300ms`) or `max_utterance_s` (`30s`).
- Transitions island to `THINKING` and transcribes the full utterance using the configured STT backend:
  - **Local `faster-whisper` (Default)**: Model size selectable (`tiny`, `base`, `small`), language selectable (`auto` or ISO code), CPU/CUDA compute type.
  - **OpenAI / Custom Whisper HTTP API**: Configurable endpoint URL (`/v1/audio/transcriptions`), model name (`whisper-1`, `gpt-4o-mini-transcribe`), and API key from `.env`/keyring.

---

## 11. Voice Output Pipeline (Kokoro & ElevenLabs Streaming TTS)

### Sentence-Chunked Streaming Playback
To minimize perceived latency, the LLM response stream feeds a **Sentence Boundary Chunker**:
1. Strips leading `[emotion:<tag>]` markers and emits emotion events to the island immediately.
2. Buffers streamed tokens until a natural sentence boundary (`.`, `!`, `?`, `\n`, respecting abbreviations and code blocks/URLs so it doesn't read raw syntax badly).
3. Enqueues complete sentences into an ordered async TTS synthesis pipeline while LLM continues generating subsequent sentences.

### Supported Backends
- **Kokoro HTTP Instance**:
  - Configurable `base_url` (e.g., `http://localhost:8880/v1` for Kokoro-FastAPI), optional API key.
  - Auto-fetches available voices from `GET {base_url}/audio/voices` (with fallback manual voice entry like `af_heart`).
  - Synthesizes via `POST {base_url}/audio/speech` (`model: "kokoro"`, `voice`, `speed`, `response_format: "pcm"` or `"wav"`).
- **ElevenLabs API**:
  - API key resolved from `.env` (`ELEVENLABS_API_KEY`) or OS keyring.
  - Auto-fetches voices from `GET https://api.elevenlabs.io/v1/voices`.
  - Streams low-latency audio via `POST https://api.elevenlabs.io/v1/text-to-speech/{voice_id}/stream`, supporting `model_id` (e.g., `eleven_flash_v2_5`) and voice stability/similarity sliders.
- **Disabled / Muted**: Skips audio synthesis completely; text appears immediately in the transcript drawer.

### Playback & Sync
- Audio frames play through `sounddevice` while calculating per-frame RMS amplitude emitted to the island (`audio_level` WebSocket event) to animate the `SPEAKING` eyes.
- Clicking the island or triggering the wake phrase immediately flushes the playback queue (**Barge-In**).

---

## 12. AI Provider Router, Model Discovery & System Prompt

### Providers & Live Discovery
Unified async adapter interface (`LLMProvider`) supporting:
1. **Google Gemini** (`GEMINI_API_KEY`) — discovers models via Gemini Models API.
2. **OpenAI / ChatGPT** (`OPENAI_API_KEY`) — discovers models via `GET /v1/models`.
3. **Ollama** (configurable `base_url`, default `http://localhost:11434`) — discovers installed models via `GET /api/tags`.
4. **Anthropic** (`ANTHROPIC_API_KEY`) — discovers models via `GET /v1/models` (or curated fallback list if key lacks list permission).
5. **Custom OpenAI-Compatible** (user-defined `base_url`, optional API key, e.g., LM Studio, vLLM, OpenRouter, Groq) — discovers via `GET {base_url}/models`.

### Automatic Tool-Support Detection & Override
When a model is selected, the daemon automatically resolves `supports_tools: bool`:
- **Ollama**: Calls `POST /api/show` for the chosen model and inspects the returned `capabilities` array for `"tools"`.
- **Gemini / OpenAI / Anthropic**: Evaluated against known capability rules / model metadata (e.g., excluding non-tool embedding/reasoning-only variants).
- **Manual Override**: Settings shows the detected badge (`Tools: Supported` / `Tools: Not Supported`) alongside a `"Force Enable Tool Calling"` override switch for custom endpoints or newly released models.
- Only when tool calling is active (detected or overridden) does the UI enable MCP server binding and pass MCP tool schemas to the LLM.

### System Prompt & Default Fast-Assistant Template
Settings provides a multiline editable **System Prompt** with a `"Reset to Default"` action. Default prompt:

```text
You are Pet, a fast, helpful, and friendly desktop AI assistant.
Keep spoken answers concise, direct, and natural (1-3 sentences unless asked for detail).
Avoid markdown tables or raw ASCII clutter when replying to voice queries.
Begin every reply with a single mood tag from: [emotion:happy], [emotion:curious], [emotion:surprised], [emotion:sleepy], [emotion:error], or [emotion:neutral].
If tools are available and needed, call them concisely and summarize results clearly.
```

---

## 13. MCP (Model Context Protocol) Integration

Built on the official Python `mcp` SDK (`ClientSession`), supporting both transports:
1. **`stdio`**: Local command, argument list, working directory, and scoped environment variables (`VAR=${ENV_VAR}` interpolation supported so secrets stay in `.env`/keyring).
2. **`SSE` / `Streamable HTTP`**: Remote URL and custom HTTP headers (with secret header masking).

### Dual Configuration Experience in Settings
Shown and enabled when the active model supports tools:
- **Structured GUI Form**:
  - List of configured servers with live connection status dot (`Connected`, `Disconnected`, `Error`).
  - Add / Edit / Remove server modal (`name`, `transport`, `command`/`args` or `url`/`headers`).
  - **`Test Connection`** button: spawns/connects temporarily, lists discovered tools with descriptions, and reports any startup stderr errors.
  - Per-tool enable/disable checkboxes and a per-server **`Require Confirmation Before Tool Execution`** toggle (when enabled, clicking `Allow` / `Deny` appears directly in the island drawer before execution).
- **Raw `mcpServers` JSON Editor**:
  - Two-way synchronized monospace editor supporting the standard format:
    ```json
    {
      "mcpServers": {
        "filesystem": {
          "command": "npx",
          "args": ["-y", "@modelcontextprotocol/server-filesystem", "/home/user/Documents"]
        },
        "remote-docs": {
          "url": "http://localhost:8080/mcp"
        }
      }
    }
    ```
  - Validates JSON schema live before applying and restarting affected MCP client sessions.

---

## 14. Proposed Repository Structure, IPC Protocol & SQLite Schema

### Repository Layout (Planned)
```text
pet/
├── PLAN.md
├── README.md
├── .env.example
├── core-daemon/
│   ├── pyproject.toml
│   └── ai_pet/
│       ├── __init__.py
│       ├── main.py                 # FastAPI + uvicorn entrypoint & lifecycle
│       ├── config.py               # Pydantic settings + .env / keyring secret loader
│       ├── storage.py              # aiosqlite schema & session/message repositories
│       ├── ipc.py                  # WebSocket ConnectionManager + REST routers
│       ├── audio/
│       │   ├── capture.py          # PipeWire / sounddevice input stream
│       │   ├── vad.py              # Silero VAD frame processor
│       │   ├── wakeword.py         # Open-vocab tiny STT + phonetic fuzzy matcher
│       │   └── stt.py              # Local faster-whisper & OpenAI Whisper API clients
│       ├── tts/
│       │   ├── base.py             # TTS adapter + sentence boundary chunker
│       │   ├── kokoro.py           # Kokoro HTTP (/v1/audio/speech) client
│       │   ├── elevenlabs.py       # ElevenLabs streaming client
│       │   └── player.py           # Non-blocking playback + RMS amplitude + barge-in
│       ├── llm/
│       │   ├── router.py           # Provider factory, model discovery, tool detection
│       │   ├── gemini.py
│       │   ├── openai_compat.py    # OpenAI, Ollama (/api/show), Custom endpoints
│       │   ├── anthropic_client.py
│       │   └── emotions.py         # [emotion:<tag>] parser & stripper
│       └── mcp_host/
│           ├── manager.py          # stdio + SSE/Streamable HTTP ClientSession pool
│           └── schema_sync.py      # Bidirectional GUI <-> mcpServers JSON adapter
├── gnome-extension/
│   └── ai-pet@local/
│       ├── metadata.json           # shell-version: ["50"] (and compatible 46-49 if tested)
│       ├── extension.js            # Extension lifecycle & IPC reconnect loop
│       ├── islandWidget.js         # Matte-black pill, Cairo DrawingArea eyes, click/drag
│       ├── eyeRenderer.js          # Procedural Grok-eye expressions & animation timeline
│       ├── positionManager.js      # Monitor selection, 9-point Fixed grid, Float persistence
│       ├── transcriptDrawer.js     # Left-click anchored chat & tool-call drawer
│       ├── contextMenu.js          # Right-click menu (Config, Mute toggles, Exit)
│       └── stylesheet.css
├── settings-ui/
│   └── ai_pet_settings/
│       ├── __init__.py
│       ├── app.py                  # GTK4 / Libadwaita Adw.PreferencesWindow
│       └── pages/                  # General, AI & MCP, Voice Input, Voice Output, Visuals
├── systemd/
│   └── ai-pet-daemon.service
└── tests/
    ├── test_wakeword.py
    ├── test_tts_chunker.py
    ├── test_providers_and_mcp.py
    ├── test_storage_and_ipc.py
    └── fixtures/
```

### IPC Protocol (Localhost `127.0.0.1:8765`)
- **WebSocket (`ws://127.0.0.1:8765/ws`)**:
  - `state_changed`: `{ "type": "state_changed", "state": "IDLE|LISTENING|THINKING|TOOL_CALLING|SPEAKING|MUTED|ERROR", "emotion": "neutral|happy|curious|surprised|sleepy|error", "label": "..." }`
  - `audio_level`: `{ "type": "audio_level", "source": "mic|tts", "rms": 0.0..1.0 }`
  - `transcript_delta`: `{ "type": "transcript_delta", "session_id": "...", "message_id": "...", "role": "user|assistant", "source": "voice|text", "text": "...", "is_final": true|false }`
  - `tool_call_status`: `{ "type": "tool_call_status", "call_id": "...", "server": "...", "tool": "...", "status": "awaiting_confirm|running|ok|error", "summary": "..." }`
  - `config_updated`: `{ "type": "config_updated", "config": { ...redacted... } }`
  - `shutdown`: `{ "type": "shutdown" }`
- **REST Endpoints (`/api/v1/*`)**:
  - `GET / PATCH /api/v1/config` — Read/update non-secret config (`~/.config/ai-pet/config.json`)
  - `PUT /api/v1/secrets` — Store provider/TTS secrets into OS keyring (`libsecret`) or `.env` (never returned in plaintext)
  - `GET /api/v1/providers/{provider}/models` — Fetch models & tool-calling capability flags
  - `POST /api/v1/mcp/test` — Test an MCP server config and return discovered tools
  - `GET /api/v1/tts/voices` — Fetch available voices for active Kokoro or ElevenLabs config
  - `GET / POST / DELETE /api/v1/sessions` — Query SQLite history, send typed prompt, trigger TTS replay, clear session
  - `POST /api/v1/voice/trigger` & `POST /api/v1/voice/barge-in` — Manual listen / stop speaking
  - `POST /api/v1/system/exit` — Clean daemon shutdown

### SQLite Schema (`~/.local/share/ai-pet/pet.db`)
- `sessions (id TEXT PRIMARY KEY, title TEXT, created_at TIMESTAMP, updated_at TIMESTAMP)`
- `messages (id TEXT PRIMARY KEY, session_id TEXT, role TEXT, input_mode TEXT, content TEXT, emotion TEXT, created_at TIMESTAMP)`
- `tool_calls (id TEXT PRIMARY KEY, message_id TEXT, server_name TEXT, tool_name TEXT, arguments_json TEXT, result_summary TEXT, status TEXT, created_at TIMESTAMP)`

---

## 15. Verification & Testing Strategy

1. **Automated Unit & Integration Tests (`pytest`)**:
   - **Wake-Word Matching**: Verify `"Hey Nova"`, `"hey nova check this"`, and minor phonetic variants match above threshold while unrelated background phrases do not trigger false positives.
   - **Sentence Chunker & Emotion Parser**: Verify `[emotion:curious] Let's check v1.2! Done.` emits `curious`, strips the tag, and splits cleanly without breaking decimals/URLs.
   - **Provider Capability & MCP Sync**: Verify Ollama `/api/show` `"tools"` detection, manual override toggle, bidirectional form-to-`mcpServers` JSON conversion, and stdio/HTTP mock tool loop.
   - **Storage & IPC**: Verify SQLite CRUD, secret redaction in `GET /api/v1/config`, and real-time WebSocket event delivery across simulated turns.
2. **GNOME Shell 50 Wayland Nested Testing**:
   - Launch isolated shell via `dbus-run-session -- gnome-shell --nested --wayland` and verify:
     - Extension enables cleanly with zero `journalctl` GJS errors.
     - Island renders on top of test windows across all 9 Fixed alignments + pixel offsets.
     - Floating drag-and-drop works (`>= 5px` threshold) and persists coordinates while clicks (`< 5px`) toggle the anchored transcript drawer.
     - Right-click opens menu, launches GTK4/Libadwaita `settings-ui`, toggles Mute/Lock, and `Exit` stops the daemon and hides the overlay.
3. **End-to-End Hardware & Service Smoke Check**:
   - Test live PipeWire mic input -> Silero VAD -> custom wake-word -> `faster-whisper` -> Ollama/Gemini + sample MCP server -> Kokoro HTTP / ElevenLabs streaming playback -> barge-in interruption.
