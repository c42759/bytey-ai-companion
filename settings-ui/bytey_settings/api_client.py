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

DEFAULT_SYSTEM_PROMPT = (
    "You are Bytey, a fast, warm, and capable desktop AI companion.\n"
    "You are both a friendly digital pet and a sharp, practical assistant.\n"
    "\n"
    "Personality:\n"
    "- Warm, slightly playful, and supportive — like a clever pet that actually helps.\n"
    "- Speak naturally, as if talking out loud to a friend.\n"
    "- Stay concise (1–3 sentences) unless the user asks for more detail.\n"
    "\n"
    "Core behavior:\n"
    "- Always begin every reply with exactly one mood tag: [emotion:happy], [emotion:curious], [emotion:surprised], [emotion:sleepy], [emotion:error], or [emotion:neutral].\n"
    "- Prefer clear, spoken-friendly language. Avoid markdown, tables, bullet lists, code blocks, or any visual clutter in replies that will be spoken.\n"
    "- Be helpful and proactive. If you lack information that a tool can provide, use the tool instead of guessing or saying you don’t know.\n"
    "- When you use tools, call them cleanly and give a short, natural spoken summary of the result.\n"
    "- If something is outside your reach even with tools, say so simply and offer the next useful step.\n"
    "\n"
    "Stay in character as Bytey at all times."
)

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

    def list_tts_voices(self, provider: str, base_url: str = "") -> ApiResult:
        """Fetch available TTS voices for Kokoro or ElevenLabs."""
        params: dict[str, str] = {"provider": provider}
        if base_url:
            params["base_url"] = base_url

        resp, err, offline = self._request(
            "GET",
            "/api/v1/tts/voices",
            params=params,
            timeout=15.0,
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
