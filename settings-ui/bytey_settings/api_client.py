"""Synchronous REST API client for Bytey AI Companion core daemon with offline config fallback."""

from __future__ import annotations

import copy
import json
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

DEFAULT_DAEMON_URL = "http://127.0.0.1:8765"
PRIMARY_CONFIG_PATH = Path.home() / ".config" / "bytey-ai-companion" / "config.json"
LEGACY_CONFIG_PATH = Path.home() / ".config" / "ai-pet" / "config.json"
LOCAL_CONFIG_PATH = PRIMARY_CONFIG_PATH


def resolve_local_config_path() -> Path:
    """Resolve local config.json path with env override and legacy ~/.config/ai-pet fallback."""
    env_dir = os.environ.get("BYTEY_CONFIG_DIR") or os.environ.get("AI_PET_CONFIG_DIR")
    if env_dir:
        return Path(env_dir) / "config.json"
    primary = Path.home() / ".config" / "bytey-ai-companion" / "config.json"
    legacy = Path.home() / ".config" / "ai-pet" / "config.json"
    if primary.exists():
        return primary
    if legacy.exists():
        return legacy
    return primary

def resolve_default_skills_dir() -> Path:
    """Resolve default user skills directory, honoring env overrides."""
    env_skills = os.environ.get("BYTEY_SKILLS_DIR") or os.environ.get("AI_PET_SKILLS_DIR")
    if env_skills:
        return Path(env_skills).expanduser()
    return resolve_local_config_path().parent / "skills"


def _load_default_system_prompt() -> str:
    """Load the default system prompt from the core-daemon Markdown file."""
    repo_md = (
        Path(__file__).resolve().parents[2]
        / "core-daemon"
        / "bytey_ai_companion"
        / "llm"
        / "default_system_prompt.md"
    )
    if repo_md.is_file():
        return repo_md.read_text(encoding="utf-8").strip()
    try:
        from bytey_ai_companion.llm.emotions import load_default_system_prompt

        return load_default_system_prompt()
    except Exception:
        return ""


DEFAULT_SYSTEM_PROMPT = _load_default_system_prompt()

SECRET_KEY_NAMES = frozenset(
    {
        "GEMINI_API_KEY",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "CUSTOM_LLM_API_KEY",
        "ELEVENLABS_API_KEY",
        "KOKORO_API_KEY",
        "STT_API_KEY",
        "api_key",
    }
)

PROVIDER_SECRET_MAP = {
    "gemini": "GEMINI_API_KEY",
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "custom": "CUSTOM_LLM_API_KEY",
}

DEFAULT_CONFIG: dict[str, Any] = {
    "general": {
        "pet_name": "Bytey",
        "wake_phrase": "Hey Bytey",
        "system_prompt": DEFAULT_SYSTEM_PROMPT,
    },
    "llm": {
        "provider": "ollama",
        "base_url": "http://localhost:11434",
        "model": "",
        "supports_tools": False,
        "force_enable_tools": False,
        "context_size": None,
        "max_context_size": None,
        "max_tool_rounds": 25,
    },
    "voice_input": {
        "mic_muted": False,
        "wake_sensitivity": 0.72,
        "vad_silence_timeout_ms": 1300,
        "stt_backend": "local_whisper",
        "whisper_model": "base",
        "stt_api_url": "",
    },
    "voice_output": {
        "tts_muted": False,
        "provider": "kokoro",
        "kokoro_base_url": "http://localhost:8880/v1",
        "kokoro_voice": "af_heart",
        "kokoro_speed": 1.0,
        "piper_model": "en_US-lessac-medium",
        "piper_models_dir": "",
        "piper_speed": 1.0,
        "piper_pitch": 0.0,
        "piper_noise_scale": 0.667,
        "piper_speaker_id": None,
        "elevenlabs_voice_id": "",
        "elevenlabs_model_id": "eleven_flash_v2_5",
    },
    "visuals": {
        "animation_mode": "full",
        "cursor_tracking": True,
        "position_mode": "fixed",
        "monitor_index": 0,
        "fixed_alignment": "top-center",
        "offset_x": 0,
        "offset_y": 12,
        "float_x": 100,
        "float_y": 80,
        "chat_width": 380,
        "chat_height": 420,
        "chat_density": "comfortable",
        "eye_color": "#F7FAFF",
        "background_color": "#0A0A0C",
        "border_color": "#2A2C34",
    },
    "mcpServers": {},
    "skills": {
        "enabled": True,
        "auto_discover": True,
        "allow_script_execution": False,
        "skills_dir": "",
        "extra_dirs": [],
        "overrides": {},
    },
    "workspace": {
        "active_path": "",
        "recent_paths": [],
        "auto_load_skills": True,
        "auto_load_rules": True,
        "require_write_approval": True,
        "require_command_approval": True,
    },
}


@dataclass
class ApiResult:
    """Normalized response returned by DaemonClient methods."""

    ok: bool
    offline: bool = False
    data: Any = None
    error: str = ""
    status_code: int | None = None


def deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge *patch* into a copy of *base*."""
    merged = copy.deepcopy(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def _strip_secrets(obj: Any) -> Any:
    """Remove secret keys before persisting config locally."""
    if not isinstance(obj, dict):
        return obj
    cleaned: dict[str, Any] = {}
    for key, val in obj.items():
        if key in SECRET_KEY_NAMES:
            continue
        cleaned[key] = _strip_secrets(val) if isinstance(val, dict) else val
    return cleaned


def _extract_error(response: httpx.Response) -> str:
    try:
        payload = response.json()
        if isinstance(payload, dict):
            detail = payload.get("detail") or payload.get("error") or payload.get("message")
            if detail:
                return str(detail)
    except ValueError:
        pass
    text = response.text.strip()
    return text or f"HTTP {response.status_code}"


class DaemonClient:
    """Synchronous HTTP client for Bytey AI Companion daemon REST endpoints."""

    def __init__(
        self,
        base_url: str = DEFAULT_DAEMON_URL,
        config_path: Path | None = None,
        timeout: float = 4.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.config_path = config_path or resolve_local_config_path()
        self._timeout = timeout
        self._lock = threading.Lock()
        self._client = httpx.Client(base_url=self.base_url, timeout=timeout)

    def close(self) -> None:
        with self._lock:
            self._client.close()

    def __enter__(self) -> DaemonClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _request(
        self,
        method: str,
        path: str,
        *,
        timeout: float | None = None,
        **kwargs: Any,
    ) -> tuple[httpx.Response | None, str, bool]:
        """Execute an HTTP request, distinguishing daemon-offline errors."""
        try:
            with self._lock:
                response = self._client.request(
                    method,
                    path,
                    timeout=timeout if timeout is not None else self._timeout,
                    **kwargs,
                )
            return response, "", False
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
            return None, str(exc), True
        except httpx.TimeoutException as exc:
            return None, f"Request timed out: {exc}", False
        except httpx.HTTPError as exc:
            return None, str(exc), True

    def _read_local_config(self) -> dict[str, Any]:
        if not self.config_path.exists():
            return copy.deepcopy(DEFAULT_CONFIG)
        try:
            raw = json.loads(self.config_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                return deep_merge(DEFAULT_CONFIG, raw)
        except (OSError, json.JSONDecodeError):
            pass
        return copy.deepcopy(DEFAULT_CONFIG)

    def _write_local_config(self, data: dict[str, Any]) -> None:
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        sanitized = _strip_secrets(data)
        tmp_path = self.config_path.with_suffix(".json.tmp")
        tmp_path.write_text(json.dumps(sanitized, indent=2) + "\n", encoding="utf-8")
        tmp_path.replace(self.config_path)

    def get_health(self) -> ApiResult:
        """Check daemon health."""
        resp, err, offline = self._request("GET", "/api/v1/health", timeout=2.0)
        if resp is not None and resp.status_code == 404:
            resp, err, offline = self._request("GET", "/health", timeout=2.0)
        if resp is None:
            return ApiResult(ok=False, offline=offline, error=err or "Daemon unreachable")
        if resp.is_success:
            try:
                data = resp.json()
            except ValueError:
                data = {"status": "ok"}
            return ApiResult(ok=True, offline=False, data=data, status_code=resp.status_code)
        return ApiResult(
            ok=False,
            offline=False,
            error=_extract_error(resp),
            status_code=resp.status_code,
        )

    def get_config(self) -> ApiResult:
        """Fetch current configuration from daemon or local config file fallback."""
        resp, err, offline = self._request("GET", "/api/v1/config")
        if resp is None:
            if offline:
                return ApiResult(
                    ok=True,
                    offline=True,
                    data=self._read_local_config(),
                    error=err or "Daemon offline",
                )
            return ApiResult(ok=False, offline=False, error=err)

        if resp.is_success:
            try:
                payload = resp.json()
            except ValueError as exc:
                return ApiResult(ok=False, error=f"Invalid JSON from daemon: {exc}")
            return ApiResult(ok=True, offline=False, data=payload, status_code=resp.status_code)

        return ApiResult(
            ok=False,
            offline=False,
            error=_extract_error(resp),
            status_code=resp.status_code,
        )

    def patch_config(self, patch: dict[str, Any]) -> ApiResult:
        """Apply partial config update via daemon or local file fallback."""
        resp, err, offline = self._request("PATCH", "/api/v1/config", json=patch)
        if resp is None:
            if offline:
                current = self._read_local_config()
                updated = deep_merge(current, _strip_secrets(patch))
                try:
                    self._write_local_config(updated)
                except OSError as write_exc:
                    return ApiResult(ok=False, offline=True, error=str(write_exc))
                return ApiResult(ok=True, offline=True, data=updated)
            return ApiResult(ok=False, offline=False, error=err)

        if resp.is_success:
            try:
                payload = resp.json()
            except ValueError:
                payload = patch
            return ApiResult(ok=True, offline=False, data=payload, status_code=resp.status_code)

        return ApiResult(
            ok=False,
            offline=False,
            error=_extract_error(resp),
            status_code=resp.status_code,
        )

    def replace_config(self, config_data: dict[str, Any]) -> ApiResult:
        """Replace all configuration via daemon PUT or local file fallback."""
        if not isinstance(config_data, dict):
            return ApiResult(ok=False, error="Configuration payload must be a JSON object")

        resp, err, offline = self._request("PUT", "/api/v1/config", json=config_data)
        if resp is None:
            if offline:
                allowed = set(DEFAULT_CONFIG.keys()) | {"mcp_servers", "voice_input", "voice_output", "mcpServers"}
                if not any(k in config_data for k in allowed):
                    return ApiResult(
                        ok=False,
                        offline=True,
                        error="Invalid configuration JSON: no recognized configuration sections",
                    )
                updated = deep_merge(copy.deepcopy(DEFAULT_CONFIG), _strip_secrets(config_data))
                try:
                    self._write_local_config(updated)
                except OSError as write_exc:
                    return ApiResult(ok=False, offline=True, error=str(write_exc))
                return ApiResult(ok=True, offline=True, data=updated)
            return ApiResult(ok=False, offline=False, error=err)

        if resp.is_success:
            try:
                payload = resp.json()
            except ValueError:
                payload = config_data
            return ApiResult(ok=True, offline=False, data=payload, status_code=resp.status_code)

        return ApiResult(
            ok=False,
            offline=False,
            error=_extract_error(resp),
            status_code=resp.status_code,
        )

    def put_secrets(self, secrets: dict[str, str]) -> ApiResult:
        """Store secret values via daemon keyring/env endpoint."""
        clean = {k: v for k, v in secrets.items() if k and v is not None}
        if not clean:
            return ApiResult(ok=False, error="No secrets provided")

        resp, err, offline = self._request("PUT", "/api/v1/secrets", json={"secrets": clean})
        if resp is not None and resp.status_code == 422:
            # Compatibility fallback if daemon accepts a flat {key: value} body.
            resp, err, offline = self._request("PUT", "/api/v1/secrets", json=clean)

        if resp is None:
            return ApiResult(
                ok=False,
                offline=offline,
                error="Daemon offline: secrets require running daemon or .env file"
                if offline
                else err,
            )

        if resp.is_success:
            try:
                data = resp.json()
            except ValueError:
                data = {"updated": list(clean.keys())}
            return ApiResult(ok=True, offline=False, data=data, status_code=resp.status_code)

        return ApiResult(
            ok=False,
            offline=False,
            error=_extract_error(resp),
            status_code=resp.status_code,
        )

    def list_models(self, provider: str, base_url: str = "") -> ApiResult:
        """Fetch available models and tool capabilities for *provider*."""
        params = {"base_url": base_url} if base_url else {}
        resp, err, offline = self._request(
            "GET",
            f"/api/v1/providers/{provider}/models",
            params=params,
            timeout=15.0,
        )
        if resp is None:
            return ApiResult(
                ok=False,
                offline=offline,
                error="Daemon offline: cannot fetch provider models" if offline else err,
            )

        if resp.is_success:
            try:
                payload = resp.json()
            except ValueError as exc:
                return ApiResult(ok=False, error=f"Invalid models payload: {exc}")
            return ApiResult(ok=True, offline=False, data=payload, status_code=resp.status_code)

        return ApiResult(
            ok=False,
            offline=False,
            error=_extract_error(resp),
            status_code=resp.status_code,
        )

    def get_mcp_json(self) -> ApiResult:
        """Fetch raw mcpServers JSON string from daemon or local config."""
        for endpoint in ("/api/v1/mcp/json", "/api/v1/mcp"):
            resp, err, offline = self._request("GET", endpoint)
            if resp is None:
                if offline:
                    local_cfg = self._read_local_config()
                    servers = _extract_mcp_servers(local_cfg)
                    raw = json.dumps({"mcpServers": servers}, indent=2)
                    return ApiResult(ok=True, offline=True, data={"raw_json": raw, "mcpServers": servers})
                return ApiResult(ok=False, offline=False, error=err)

            if resp.is_success:
                try:
                    payload = resp.json()
                except ValueError:
                    return ApiResult(ok=True, data={"raw_json": resp.text})
                if isinstance(payload, dict) and "raw_json" in payload:
                    return ApiResult(ok=True, data=payload, status_code=resp.status_code)
                servers = _extract_mcp_servers(payload)
                return ApiResult(
                    ok=True,
                    data={"raw_json": json.dumps({"mcpServers": servers}, indent=2), "mcpServers": servers},
                    status_code=resp.status_code,
                )
            if resp.status_code != 404:
                return ApiResult(ok=False, error=_extract_error(resp), status_code=resp.status_code)

        # Fallback: extract mcpServers from /api/v1/config if no dedicated MCP GET endpoint exists.
        cfg_res = self.get_config()
        if not cfg_res.ok:
            return cfg_res
        servers = _extract_mcp_servers(cfg_res.data)
        return ApiResult(
            ok=True,
            offline=cfg_res.offline,
            data={"raw_json": json.dumps({"mcpServers": servers}, indent=2), "mcpServers": servers},
        )

    def put_mcp_json(self, raw_json: str) -> ApiResult:
        """Validate and apply raw mcpServers JSON."""
        try:
            parsed = json.loads(raw_json)
        except json.JSONDecodeError as exc:
            return ApiResult(ok=False, error=f"Invalid JSON: {exc}")

        if not isinstance(parsed, dict):
            return ApiResult(ok=False, error="Root JSON value must be an object")

        servers = parsed.get("mcpServers", parsed if "mcpServers" not in parsed else {})
        if not isinstance(servers, dict):
            return ApiResult(ok=False, error='"mcpServers" must be a JSON object')

        normalized_raw = json.dumps({"mcpServers": servers}, indent=2)

        resp, err, offline = self._request("PUT", "/api/v1/mcp/json", json={"raw_json": normalized_raw}, timeout=15.0)
        if resp is not None and resp.status_code == 422:
            resp, err, offline = self._request("PUT", "/api/v1/mcp/json", json={"mcpServers": servers}, timeout=15.0)
        if resp is not None and resp.status_code == 404:
            resp, err, offline = self._request("PUT", "/api/v1/mcp", json={"mcpServers": servers}, timeout=15.0)

        if resp is None:
            if offline:
                cfg = self._read_local_config()
                cfg["mcpServers"] = servers
                try:
                    self._write_local_config(cfg)
                except OSError as write_exc:
                    return ApiResult(ok=False, offline=True, error=str(write_exc))
                return ApiResult(
                    ok=True,
                    offline=True,
                    data={"raw_json": normalized_raw, "mcpServers": servers},
                )
            return ApiResult(ok=False, offline=False, error=err)

        if resp.status_code == 404:
            return self.patch_config({"mcpServers": servers})

        if resp.is_success:
            try:
                data = resp.json()
            except ValueError:
                data = {"raw_json": normalized_raw, "mcpServers": servers}
            return ApiResult(ok=True, offline=False, data=data, status_code=resp.status_code)

        return ApiResult(
            ok=False,
            offline=False,
            error=_extract_error(resp),
            status_code=resp.status_code,
        )

    def get_mcp_host_info(self) -> ApiResult:
        """Fetch built-in Bytey MCP Host metadata and tool descriptions."""
        resp, err, offline = self._request("GET", "/api/v1/mcp/host", timeout=5.0)
        if resp is None:
            return ApiResult(
                ok=False,
                offline=offline,
                error="Daemon offline: cannot inspect built-in MCP Host" if offline else err,
            )
        if resp.is_success:
            return ApiResult(ok=True, data=resp.json(), status_code=resp.status_code)
        return ApiResult(
            ok=False,
            error=_extract_error(resp),
            status_code=resp.status_code,
        )

    def list_agents(self) -> ApiResult:
        """Fetch supported external CLI agents and detection status."""
        resp, err, offline = self._request("GET", "/api/v1/agents", timeout=5.0)
        if resp is None:
            return ApiResult(
                ok=False,
                offline=offline,
                error="Daemon offline: cannot list CLI agents" if offline else err,
            )
        if resp.is_success:
            return ApiResult(ok=True, data=resp.json(), status_code=resp.status_code)
        return ApiResult(ok=False, error=_extract_error(resp), status_code=resp.status_code)

    def detect_agents(self) -> ApiResult:
        """Trigger CLI agent re-detection on the daemon."""
        resp, err, offline = self._request("POST", "/api/v1/agents/detect", json={}, timeout=10.0)
        if resp is None:
            return ApiResult(
                ok=False,
                offline=offline,
                error="Daemon offline: cannot detect CLI agents" if offline else err,
            )
        if resp.is_success:
            return ApiResult(ok=True, data=resp.json(), status_code=resp.status_code)
        return ApiResult(ok=False, error=_extract_error(resp), status_code=resp.status_code)

    def patch_agent(self, agent_id: str, patch: dict[str, Any]) -> ApiResult:
        """Update per-CLI agent override settings (enabled, model, permission_mode, etc.)."""
        resp, err, offline = self._request(
            "PATCH", f"/api/v1/agents/{agent_id}", json=patch, timeout=5.0
        )
        if resp is None:
            return ApiResult(
                ok=False,
                offline=offline,
                error="Daemon offline: cannot update CLI agent" if offline else err,
            )
        if resp.is_success:
            return ApiResult(ok=True, data=resp.json(), status_code=resp.status_code)
        return ApiResult(ok=False, error=_extract_error(resp), status_code=resp.status_code)

    def test_mcp_server(self, name: str, cfg: dict[str, Any]) -> ApiResult:
        """Test connectivity and discover tools for a single MCP server."""
        resp, err, offline = self._request(
            "POST",
            "/api/v1/mcp/test",
            json={"name": name, "config": cfg},
            timeout=20.0,
        )
        if resp is not None and resp.status_code == 422:
            resp, err, offline = self._request(
                "POST",
                "/api/v1/mcp/test",
                json={"name": name, **cfg},
                timeout=20.0,
            )

        if resp is None:
            return ApiResult(
                ok=False,
                offline=offline,
                error="Daemon offline: cannot test MCP server" if offline else err,
            )

        if resp.is_success:
            try:
                data = resp.json()
            except ValueError:
                data = {"ok": True, "tools": []}
            return ApiResult(ok=True, offline=False, data=data, status_code=resp.status_code)

        return ApiResult(
            ok=False,
            offline=False,
            error=_extract_error(resp),
            status_code=resp.status_code,
        )

    def _post_json_action(
        self,
        path: str,
        payload: dict[str, Any],
        *,
        timeout: float,
        offline_error: str,
        default_data: dict[str, Any],
    ) -> ApiResult:
        """Execute a POST JSON request returning a normalized ApiResult."""
        resp, err, offline = self._request("POST", path, json=payload, timeout=timeout)
        if resp is None:
            return ApiResult(ok=False, offline=offline, error=offline_error if offline else err)
        if resp.is_success:
            try:
                data = resp.json()
            except ValueError:
                data = dict(default_data)
            return ApiResult(ok=True, offline=False, data=data, status_code=resp.status_code)
        return ApiResult(
            ok=False,
            offline=False,
            error=_extract_error(resp),
            status_code=resp.status_code,
        )

    def list_tts_voices(
        self,
        provider: str,
        base_url: str = "",
        *,
        refresh: bool = False,
        language: str = "",
    ) -> ApiResult:
        """Fetch available TTS voices for Kokoro, Piper, or ElevenLabs."""
        params: dict[str, str] = {"provider": provider}
        if base_url:
            params["base_url"] = base_url
        if refresh:
            params["refresh"] = "1"
        if language:
            params["language"] = language

        resp, err, offline = self._request(
            "GET",
            "/api/v1/tts/voices",
            params=params,
            timeout=30.0 if refresh else 15.0,
        )
        if resp is None:
            return ApiResult(
                ok=False,
                offline=offline,
                error="Daemon offline: cannot fetch TTS voices" if offline else err,
            )

        if resp.is_success:
            try:
                data = resp.json()
            except ValueError as exc:
                return ApiResult(ok=False, error=f"Invalid voices payload: {exc}")
            return ApiResult(ok=True, offline=False, data=data, status_code=resp.status_code)

        return ApiResult(
            ok=False,
            offline=False,
            error=_extract_error(resp),
            status_code=resp.status_code,
        )

    def download_piper_model(self, model_id: str) -> ApiResult:
        """Download a Piper voice model via the core daemon."""
        cleaned = model_id.strip()
        if not cleaned:
            return ApiResult(ok=False, error="No Piper model specified")
        return self._post_json_action(
            "/api/v1/tts/piper/download",
            {"model": cleaned},
            timeout=300.0,
            offline_error="Daemon offline: cannot download Piper model",
            default_data={"ok": True, "model": cleaned},
        )

    def replay_tts(self, text: str) -> ApiResult:
        """Speak *text* using the daemon's active TTS provider."""
        return self._post_json_action(
            "/api/v1/tts/replay",
            {"text": text},
            timeout=15.0,
            offline_error="Daemon offline: cannot preview TTS voice",
            default_data={"ok": True},
        )

    def preview_animation(
        self,
        state: str = "IDLE",
        emotion: str = "neutral",
        *,
        reset: bool = False,
    ) -> ApiResult:
        """Trigger or reset a live eye/island animation preview on the daemon."""
        payload: dict[str, Any] = {"reset": True} if reset else {"state": state, "emotion": emotion}
        return self._post_json_action(
            "/api/v1/visuals/preview",
            payload,
            timeout=5.0,
            offline_error="Daemon offline: cannot preview animation",
            default_data={"ok": True},
        )

    # ------------------------------------------------------------------
    # Skills API
    # ------------------------------------------------------------------

    def list_skills(self) -> ApiResult:
        """Fetch discovered skills from daemon, falling back gracefully if offline."""
        resp, err, offline = self._request("GET", "/api/v1/skills", timeout=10.0)
        if resp is None:
            if offline:
                return ApiResult(
                    ok=True,
                    offline=True,
                    data={
                        "skills": [],
                        "offline": True,
                        "skills_dir": str(resolve_default_skills_dir()),
                    },
                    error=err or "Daemon offline",
                )
            return ApiResult(ok=False, offline=False, error=err)
        if resp.is_success:
            try:
                payload = resp.json()
            except ValueError as exc:
                return ApiResult(ok=False, error=f"Invalid skills payload: {exc}")
            return ApiResult(ok=True, offline=False, data=payload, status_code=resp.status_code)
        return ApiResult(
            ok=False,
            offline=False,
            error=_extract_error(resp),
            status_code=resp.status_code,
        )

    def rescan_skills(self) -> ApiResult:
        """Trigger a daemon skill rescan, falling back gracefully if offline."""
        resp, err, offline = self._request("POST", "/api/v1/skills/rescan", timeout=15.0)
        if resp is None:
            if offline:
                return ApiResult(
                    ok=True,
                    offline=True,
                    data={"skills": [], "offline": True},
                    error=err or "Daemon offline",
                )
            return ApiResult(ok=False, offline=False, error=err)
        if resp.is_success:
            try:
                payload = resp.json()
            except ValueError:
                payload = {"skills": []}
            return ApiResult(ok=True, offline=False, data=payload, status_code=resp.status_code)
        return ApiResult(
            ok=False,
            offline=False,
            error=_extract_error(resp),
            status_code=resp.status_code,
        )

    def get_skill(self, name: str) -> ApiResult:
        """Fetch full details and raw SKILL.md content for *name*."""
        resp, err, offline = self._request("GET", f"/api/v1/skills/{name}")
        if resp is None:
            return ApiResult(
                ok=False,
                offline=offline,
                error="Daemon offline: cannot load skill details" if offline else err,
            )
        if resp.is_success:
            try:
                payload = resp.json()
            except ValueError as exc:
                return ApiResult(ok=False, error=f"Invalid skill payload: {exc}")
            return ApiResult(ok=True, offline=False, data=payload, status_code=resp.status_code)
        return ApiResult(
            ok=False,
            offline=False,
            error=_extract_error(resp),
            status_code=resp.status_code,
        )

    def create_skill(self, payload: dict[str, Any]) -> ApiResult:
        """Create a new user skill via daemon."""
        return self._post_json_action(
            "/api/v1/skills",
            payload,
            timeout=10.0,
            offline_error="Daemon offline: cannot create skill",
            default_data={"ok": True},
        )

    def update_skill(self, name: str, payload: dict[str, Any]) -> ApiResult:
        """Update an existing user skill's SKILL.md or metadata via daemon."""
        resp, err, offline = self._request("PUT", f"/api/v1/skills/{name}", json=payload, timeout=10.0)
        if resp is None:
            return ApiResult(
                ok=False,
                offline=offline,
                error="Daemon offline: cannot update skill" if offline else err,
            )
        if resp.is_success:
            try:
                data = resp.json()
            except ValueError:
                data = {"ok": True}
            return ApiResult(ok=True, offline=False, data=data, status_code=resp.status_code)
        return ApiResult(
            ok=False,
            offline=False,
            error=_extract_error(resp),
            status_code=resp.status_code,
        )

    def update_skill_override(self, name: str, patch: dict[str, Any]) -> ApiResult:
        """Update per-skill override (e.g. enabled) via daemon or local config.json fallback."""
        resp, err, offline = self._request("PATCH", f"/api/v1/skills/{name}", json=patch, timeout=10.0)
        if resp is None:
            if offline:
                current = self._read_local_config()
                skills_cfg = current.setdefault("skills", {})
                if not isinstance(skills_cfg, dict):
                    skills_cfg = {}
                    current["skills"] = skills_cfg
                overrides = skills_cfg.setdefault("overrides", {})
                if not isinstance(overrides, dict):
                    overrides = {}
                    skills_cfg["overrides"] = overrides
                existing = overrides.get(name) if isinstance(overrides.get(name), dict) else {}
                overrides[name] = deep_merge(existing, patch)
                try:
                    self._write_local_config(current)
                except OSError as write_exc:
                    return ApiResult(ok=False, offline=True, error=str(write_exc))
                return ApiResult(
                    ok=True,
                    offline=True,
                    data={"name": name, "override": overrides[name]},
                )
            return ApiResult(ok=False, offline=False, error=err)
        if resp.is_success:
            try:
                data = resp.json()
            except ValueError:
                data = {"name": name, **patch}
            return ApiResult(ok=True, offline=False, data=data, status_code=resp.status_code)
        return ApiResult(
            ok=False,
            offline=False,
            error=_extract_error(resp),
            status_code=resp.status_code,
        )

    def import_skill(self, path: str) -> ApiResult:
        """Import a skill from a local folder or .zip archive via daemon."""
        return self._post_json_action(
            "/api/v1/skills/import",
            {"path": path},
            timeout=15.0,
            offline_error="Daemon offline: cannot import skill",
            default_data={"ok": True},
        )

    def delete_skill(self, name: str) -> ApiResult:
        """Delete a user-installed skill via daemon."""
        resp, err, offline = self._request("DELETE", f"/api/v1/skills/{name}", timeout=10.0)
        if resp is None:
            return ApiResult(
                ok=False,
                offline=offline,
                error="Daemon offline: cannot delete skill" if offline else err,
            )
        if resp.is_success:
            try:
                data = resp.json()
            except ValueError:
                data = {"ok": True, "deleted": name}
            return ApiResult(ok=True, offline=False, data=data, status_code=resp.status_code)
        return ApiResult(
            ok=False,
            offline=False,
            error=_extract_error(resp),
            status_code=resp.status_code,
        )

    # ------------------------------------------------------------------
    # Workspace API
    # ------------------------------------------------------------------

    def get_workspace(self) -> ApiResult:
        """Fetch active workspace state from daemon or local config.json fallback."""
        resp, err, offline = self._request("GET", "/api/v1/workspace", timeout=5.0)
        if resp is None:
            if offline:
                cfg = self._read_local_config()
                ws = cfg.get("workspace")
                if not isinstance(ws, dict):
                    ws = copy.deepcopy(DEFAULT_CONFIG["workspace"])
                return ApiResult(
                    ok=True,
                    offline=True,
                    data=ws,
                    error=err or "Daemon offline",
                )
            return ApiResult(ok=False, offline=False, error=err)
        if resp.is_success:
            try:
                payload = resp.json()
            except ValueError as exc:
                return ApiResult(ok=False, error=f"Invalid workspace payload: {exc}")
            return ApiResult(ok=True, offline=False, data=payload, status_code=resp.status_code)
        return ApiResult(
            ok=False,
            offline=False,
            error=_extract_error(resp),
            status_code=resp.status_code,
        )

    def set_workspace(self, path: str) -> ApiResult:
        """Set or clear active workspace path via daemon or local config.json fallback."""
        cleaned = (path or "").strip()
        resp, err, offline = self._request(
            "PUT",
            "/api/v1/workspace",
            json={"path": cleaned},
            timeout=10.0,
        )
        if resp is None:
            if offline:
                resolved = ""
                if cleaned:
                    try:
                        candidate = Path(cleaned).expanduser().resolve()
                    except OSError as exc:
                        return ApiResult(ok=False, offline=True, error=str(exc))
                    if not candidate.is_dir():
                        return ApiResult(
                            ok=False,
                            offline=True,
                            error=f"Workspace path is not an existing directory: {cleaned!r}",
                        )
                    resolved = str(candidate)

                current = self._read_local_config()
                ws_cfg = current.setdefault("workspace", copy.deepcopy(DEFAULT_CONFIG["workspace"]))
                if not isinstance(ws_cfg, dict):
                    ws_cfg = copy.deepcopy(DEFAULT_CONFIG["workspace"])
                    current["workspace"] = ws_cfg

                ws_cfg["active_path"] = resolved
                recent = ws_cfg.get("recent_paths")
                if not isinstance(recent, list):
                    recent = []
                if resolved:
                    recent = [resolved] + [str(p) for p in recent if str(p) and str(p) != resolved]
                ws_cfg["recent_paths"] = recent[:10]

                try:
                    self._write_local_config(current)
                except OSError as write_exc:
                    return ApiResult(ok=False, offline=True, error=str(write_exc))
                return ApiResult(ok=True, offline=True, data=copy.deepcopy(ws_cfg))
            return ApiResult(ok=False, offline=False, error=err)

        if resp.is_success:
            try:
                payload = resp.json()
            except ValueError:
                payload = {"active_path": cleaned}
            return ApiResult(ok=True, offline=False, data=payload, status_code=resp.status_code)
        return ApiResult(
            ok=False,
            offline=False,
            error=_extract_error(resp),
            status_code=resp.status_code,
        )


def _extract_mcp_servers(payload: Any) -> dict[str, Any]:
    """Normalize mcpServers dict from varied API or config shapes."""
    if not isinstance(payload, dict):
        return {}
    if isinstance(payload.get("mcpServers"), dict):
        return copy.deepcopy(payload["mcpServers"])
    if isinstance(payload.get("mcp_servers"), dict):
        return copy.deepcopy(payload["mcp_servers"])
    mcp_block = payload.get("mcp")
    if isinstance(mcp_block, dict):
        if isinstance(mcp_block.get("mcpServers"), dict):
            return copy.deepcopy(mcp_block["mcpServers"])
        if isinstance(mcp_block.get("servers"), dict):
            return copy.deepcopy(mcp_block["servers"])
    cfg_block = payload.get("config")
    if isinstance(cfg_block, dict):
        return _extract_mcp_servers(cfg_block)
    return {}
