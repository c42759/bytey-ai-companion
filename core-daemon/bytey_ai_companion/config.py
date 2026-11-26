"""Configuration models, secret store, and config manager for Bytey AI Companion."""

from __future__ import annotations

import copy
import json
import logging
import math
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator

from .llm.emotions import DEFAULT_SYSTEM_PROMPT

try:
    import gi

    gi.require_version("Secret", "1")
    from gi.repository import Secret as _LibSecret
except Exception:
    _LibSecret = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

_ENV_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_KEY_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_MERGE_SECTIONS = ("general", "provider", "voice_in", "voice_out", "visuals", "skills", "workspace", "agents")

DEFAULT_EYE_COLOR = "#F7FAFF"
DEFAULT_BG_COLOR = "#0A0A0C"
DEFAULT_BORDER_COLOR = "#2A2C34"
_HEX_COLOR_RE = re.compile(r"^#(?:[0-9A-Fa-f]{3}|[0-9A-Fa-f]{6}|[0-9A-Fa-f]{8})$")
_VISUAL_COLOR_DEFAULTS: dict[str, str] = {
    "eye_color": DEFAULT_EYE_COLOR,
    "background_color": DEFAULT_BG_COLOR,
    "border_color": DEFAULT_BORDER_COLOR,
}
VALID_POSITION_MODES: tuple[str, ...] = ("fixed", "floating", "notch")
VALID_TTS_PROVIDERS: tuple[str, ...] = ("kokoro", "piper", "elevenlabs", "disabled")
VALID_AGENT_PERMISSION_MODES: tuple[str, ...] = ("default", "accept_edits", "autonomous")
PIPER_SPEED_RANGE: tuple[float, float] = (0.4, 2.5)
PIPER_PITCH_RANGE: tuple[float, float] = (-12.0, 12.0)
PIPER_NOISE_SCALE_RANGE: tuple[float, float] = (0.0, 1.5)
PIPER_VOICE_PRESETS: dict[str, dict[str, Any]] = {
    "normal": {"label": "Normal", "speed": 1.0, "pitch": 0.0, "noise_scale": 0.667},
    "chipmunk": {"label": "Chipmunk", "speed": 1.25, "pitch": 8.0, "noise_scale": 0.8},
    "helium": {"label": "Helium Squeak", "speed": 1.15, "pitch": 10.5, "noise_scale": 0.9},
    "deep_monster": {"label": "Deep Monster", "speed": 0.82, "pitch": -8.0, "noise_scale": 0.667},
    "sleepy_giant": {"label": "Sleepy Giant", "speed": 0.68, "pitch": -5.5, "noise_scale": 0.55},
    "hyper_gremlin": {"label": "Hyper Gremlin", "speed": 1.55, "pitch": 5.5, "noise_scale": 1.0},
    "flat_robot": {"label": "Flat Robot", "speed": 1.05, "pitch": -2.5, "noise_scale": 0.1},
}
_PIPER_FLOAT_SPECS: dict[str, tuple[float, tuple[float, float]]] = {
    "piper_speed": (1.0, PIPER_SPEED_RANGE),
    "piper_pitch": (0.0, PIPER_PITCH_RANGE),
    "piper_noise_scale": (0.667, PIPER_NOISE_SCALE_RANGE),
}


def _coerce_clamped_float(
    value: Any, default: float, bounds: tuple[float, float]
) -> float:
    """Coerce value to a finite float clamped to [lo, hi], or fall back to default."""
    if value is None or isinstance(value, bool):
        return default
    if isinstance(value, str):
        cleaned = value.strip()
        if not cleaned:
            return default
        value = cleaned
    try:
        num = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(num):
        return default
    lo, hi = bounds
    return max(lo, min(hi, num))


def _normalize_hex_color(value: Any, default: str) -> str:
    """Normalize a hex color (#RGB, #RRGGBB, or #RRGGBBAA) or fall back to default."""
    if not isinstance(value, str):
        return default
    cleaned = value.strip()
    if not cleaned:
        return default
    if not cleaned.startswith("#"):
        candidate = f"#{cleaned}"
        if _HEX_COLOR_RE.match(candidate):
            cleaned = candidate
    if not _HEX_COLOR_RE.match(cleaned):
        return default
    if len(cleaned) == 4:
        r, g, b = cleaned[1], cleaned[2], cleaned[3]
        cleaned = f"#{r}{r}{g}{g}{b}{b}"
    return cleaned.upper()


def _primary_config_dir() -> Path:
    return Path.home() / ".config" / "bytey-ai-companion"


def _legacy_config_dir() -> Path:
    return Path.home() / ".config" / "ai-pet"


def resolve_config_dir(config_dir: Path | None = None) -> Path:
    """Resolve configuration directory, honoring BYTEY_CONFIG_DIR (or AI_PET_CONFIG_DIR) and legacy ~/.config/ai-pet fallback."""
    if config_dir is not None:
        return Path(config_dir)
    env_dir = os.environ.get("BYTEY_CONFIG_DIR") or os.environ.get("AI_PET_CONFIG_DIR")
    if env_dir:
        return Path(env_dir)
    primary = _primary_config_dir()
    legacy = _legacy_config_dir()
    if (primary / "config.json").exists():
        return primary
    if (legacy / "config.json").exists():
        return legacy
    return primary


def resolve_default_skills_dir() -> Path:
    """Resolve default user skills directory, honoring BYTEY_SKILLS_DIR / AI_PET_SKILLS_DIR."""
    env_dir = (
        os.environ.get("BYTEY_SKILLS_DIR") or os.environ.get("AI_PET_SKILLS_DIR") or ""
    ).strip()
    if env_dir:
        return Path(env_dir).expanduser()
    return resolve_config_dir() / "skills"


def resolve_skills_dir(configured_dir: str = "") -> Path:
    """Resolve active user skills directory from explicit config or default resolution."""
    cleaned = (configured_dir or "").strip()
    if cleaned:
        return Path(cleaned).expanduser()
    return resolve_default_skills_dir()


class GeneralConfig(BaseModel):
    """General assistant identity and system prompt settings."""

    model_config = ConfigDict(extra="ignore")

    pet_name: str = "Bytey"
    wake_phrase: str = "Hey Bytey"
    system_prompt: str = DEFAULT_SYSTEM_PROMPT


PROVIDER_DEFAULT_URLS: dict[str, str] = {
    "ollama": "http://localhost:11434",
    "gemini": "",
    "openai": "https://api.openai.com/v1",
    "anthropic": "https://api.anthropic.com",
    "custom": "http://localhost:8000/v1",
}

_PROVIDER_PROFILE_FIELDS: tuple[str, ...] = (
    "model",
    "base_url",
    "supports_tools_override",
    "temperature",
    "context_size",
    "focus_window",
    "max_tool_rounds",
)


def _normalize_focus_window_value(value: Any) -> int:
    """Normalize focus_window to >= 0 (0 = full history), defaulting to 0 on blank/None."""
    if value is None or isinstance(value, bool):
        return 0
    if isinstance(value, str):
        cleaned = value.strip()
        if not cleaned:
            return 0
        value = int(cleaned)
    if isinstance(value, (int, float)):
        return max(0, int(value))
    raise ValueError(f"Invalid focus_window: {value!r}")


def _normalize_max_tool_rounds_value(value: Any) -> int:
    """Clamp max_tool_rounds to [1, 100], defaulting to 25 on blank/None."""
    if value is None or isinstance(value, bool):
        return 25
    if isinstance(value, str):
        cleaned = value.strip()
        if not cleaned:
            return 25
        value = int(cleaned)
    if isinstance(value, (int, float)):
        return max(1, min(100, int(value)))
    raise ValueError(f"Invalid max_tool_rounds: {value!r}")


def _normalize_context_size_value(value: Any) -> int | None:
    """Normalize context_size, treating auto/empty/non-positive values as None."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        cleaned = value.strip().lower()
        if not cleaned or cleaned in ("auto", "default", "none", "0"):
            return None
        value = int(cleaned)
    if isinstance(value, (int, float)):
        iv = int(value)
        return iv if iv > 0 else None
    raise ValueError(f"Invalid context_size: {value!r}")


class ProviderProfile(BaseModel):
    """Saved per-provider LLM settings restored when switching back to a provider."""

    model_config = ConfigDict(extra="ignore")

    model: str = ""
    base_url: str = ""
    supports_tools_override: bool | None = None
    temperature: float = 0.7
    context_size: int | None = None
    focus_window: int = 0
    max_tool_rounds: int = 25

    @field_validator("focus_window", mode="before")
    @classmethod
    def _validate_focus_window(cls, value: Any) -> int:
        return _normalize_focus_window_value(value)

    @field_validator("max_tool_rounds", mode="before")
    @classmethod
    def _validate_max_tool_rounds(cls, value: Any) -> int:
        return _normalize_max_tool_rounds_value(value)

    @field_validator("context_size", mode="before")
    @classmethod
    def _validate_context_size(cls, value: Any) -> int | None:
        return _normalize_context_size_value(value)


class ProviderConfig(BaseModel):
    """LLM provider selection and generation parameters."""

    model_config = ConfigDict(extra="ignore")

    active_provider: str = "ollama"
    model: str = "llama3.2"
    base_url: str = "http://localhost:11434"
    supports_tools_override: bool | None = None
    temperature: float = 0.7
    context_size: int | None = None
    focus_window: int = 0
    max_tool_rounds: int = 25
    profiles: dict[str, ProviderProfile] = Field(default_factory=dict)

    @field_validator("active_provider", mode="before")
    @classmethod
    def _validate_active_provider(cls, value: Any) -> str:
        cleaned = str(value or "").strip().lower()
        return cleaned or "ollama"

    @field_validator("focus_window", mode="before")
    @classmethod
    def _validate_focus_window(cls, value: Any) -> int:
        return _normalize_focus_window_value(value)

    @field_validator("max_tool_rounds", mode="before")
    @classmethod
    def _validate_max_tool_rounds(cls, value: Any) -> int:
        return _normalize_max_tool_rounds_value(value)

    @field_validator("context_size", mode="before")
    @classmethod
    def _validate_context_size(cls, value: Any) -> int | None:
        return _normalize_context_size_value(value)


class VoiceInConfig(BaseModel):
    """Microphone, VAD, wake-word, and speech-to-text configuration."""

    model_config = ConfigDict(extra="ignore")

    mic_muted: bool = False
    stt_backend: str = "local_whisper"
    whisper_model: str = "tiny.en"
    stt_api_url: str = "https://api.openai.com/v1/audio/transcriptions"
    wake_sensitivity: float = 0.72
    silence_timeout_ms: int = 1300
    vad_threshold: float = 0.015


class VoiceOutConfig(BaseModel):
    """Text-to-speech output provider and voice configuration."""

    model_config = ConfigDict(extra="ignore")

    tts_muted: bool = False
    tts_provider: str = "kokoro"
    kokoro_base_url: str = "http://localhost:8880/v1"
    kokoro_voice: str = "af_heart"
    kokoro_speed: float = 1.0
    piper_model: str = "en_US-lessac-medium"
    piper_models_dir: str = ""
    piper_speed: float = 1.0
    piper_pitch: float = 0.0
    piper_noise_scale: float = 0.667
    piper_speaker_id: int | None = None
    elevenlabs_voice_id: str = "21m00Tcm4TlvDq8ikWAM"
    elevenlabs_model_id: str = "eleven_flash_v2_5"

    @field_validator("tts_provider", mode="before")
    @classmethod
    def _validate_tts_provider(cls, value: Any) -> str:
        """Normalize tts_provider to stripped lowercase string, defaulting empty/None to 'kokoro'."""
        if value is None or isinstance(value, bool):
            return "kokoro"
        cleaned = str(value).strip().lower()
        return cleaned or "kokoro"

    @field_validator("piper_model", mode="before")
    @classmethod
    def _validate_piper_model(cls, value: Any) -> str:
        """Strip piper_model and fall back to default model ID when empty or None."""
        if value is None or isinstance(value, bool):
            return "en_US-lessac-medium"
        cleaned = str(value).strip()
        return cleaned or "en_US-lessac-medium"

    @field_validator("piper_models_dir", mode="before")
    @classmethod
    def _validate_piper_models_dir(cls, value: Any) -> str:
        """Normalize piper_models_dir to a stripped string."""
        if value is None or isinstance(value, bool):
            return ""
        return str(value).strip()

    @field_validator("piper_speed", "piper_pitch", "piper_noise_scale", mode="before")
    @classmethod
    def _validate_piper_float(cls, value: Any, info: ValidationInfo) -> float:
        """Coerce and clamp Piper float parameters to their supported ranges."""
        field_name = info.field_name or "piper_speed"
        default, bounds = _PIPER_FLOAT_SPECS[field_name]
        return _coerce_clamped_float(value, default, bounds)

    @field_validator("piper_speaker_id", mode="before")
    @classmethod
    def _validate_piper_speaker_id(cls, value: Any) -> int | None:
        """Normalize piper_speaker_id to None or a non-negative integer."""
        if value is None or isinstance(value, bool):
            return None
        if isinstance(value, str):
            cleaned = value.strip()
            if not cleaned:
                return None
            value = cleaned
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            return None


class VisualsConfig(BaseModel):
    """Island animation mode and monitor positioning settings."""

    model_config = ConfigDict(extra="ignore")

    animation_mode: str = "full"
    cursor_tracking: bool = True
    position_mode: str = "fixed"
    monitor_index: int = 0
    fixed_alignment: str = "top-center"
    offset_x: int = 0
    offset_y: int = 12
    float_x: int = 500
    float_y: int = 40
    island_width: int = 140
    chat_width: int = 380
    chat_height: int = 420
    chat_density: str = "comfortable"
    chat_opacity: int = 96
    chat_sidebar_open: bool = False
    eye_color: str = DEFAULT_EYE_COLOR
    background_color: str = DEFAULT_BG_COLOR
    border_color: str = DEFAULT_BORDER_COLOR

    @field_validator("position_mode", mode="before")
    @classmethod
    def _validate_position_mode(cls, value: Any) -> str:
        """Normalize position_mode to a supported mode or fall back to 'fixed'."""
        if value is None:
            return "fixed"
        normalized = str(value).strip().lower()
        return normalized if normalized in VALID_POSITION_MODES else "fixed"

    @field_validator("island_width", mode="before")
    @classmethod
    def _validate_island_width(cls, value: Any) -> int:
        try:
            num = int(round(float(value)))
        except (TypeError, ValueError):
            return 140
        return max(140, min(450, num))

    @field_validator("chat_opacity", mode="before")
    @classmethod
    def _validate_chat_opacity(cls, value: Any) -> int:
        try:
            num = int(round(float(value)))
        except (TypeError, ValueError):
            return 96
        return max(10, min(100, num))

    @field_validator("eye_color", "background_color", "border_color", mode="before")
    @classmethod
    def _validate_color_field(cls, value: Any, info: ValidationInfo) -> str:
        default = _VISUAL_COLOR_DEFAULTS[info.field_name or "eye_color"]
        return _normalize_hex_color(value, default)


class MCPServerConfig(BaseModel):
    """Configuration for a single stdio or HTTP/SSE MCP server."""

    model_config = ConfigDict(extra="ignore")

    enabled: bool = True
    transport: str = "stdio"
    command: str = ""
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    url: str = ""
    headers: dict[str, str] = Field(default_factory=dict)
    require_confirmation: bool = False
    disabled_tools: list[str] = Field(default_factory=list)


class SkillOverrideConfig(BaseModel):
    """Per-skill override settings."""

    model_config = ConfigDict(extra="ignore")

    enabled: bool = True
    require_confirmation: bool | None = None


class SkillsConfig(BaseModel):
    """Skills discovery, execution, and per-skill override settings."""

    model_config = ConfigDict(extra="ignore")

    enabled: bool = True
    auto_discover: bool = True
    allow_script_execution: bool = False
    skills_dir: str = ""
    extra_dirs: list[str] = Field(default_factory=list)
    overrides: dict[str, SkillOverrideConfig] = Field(default_factory=dict)


class WorkspaceConfig(BaseModel):
    """Active project workspace directory and coding/agent policy settings."""

    model_config = ConfigDict(extra="ignore")

    active_path: str = ""
    recent_paths: list[str] = Field(default_factory=list)
    auto_load_skills: bool = True
    auto_load_rules: bool = True
    require_write_approval: bool = True
    require_command_approval: bool = True

    @field_validator("active_path", mode="before")
    @classmethod
    def _validate_active_path(cls, value: Any) -> str:
        """Normalize active_path; when non-empty, verify it resolves to an existing directory."""
        if value is None or isinstance(value, bool):
            return ""
        cleaned = str(value).strip()
        if not cleaned:
            return ""
        resolved = Path(cleaned).expanduser().resolve()
        if not resolved.is_dir():
            raise ValueError(f"Workspace path is not an existing directory: {cleaned!r}")
        return str(resolved)

    @field_validator("recent_paths", mode="before")
    @classmethod
    def _validate_recent_paths(cls, value: Any) -> list[str]:
        """Normalize recent_paths to at most 10 unique resolved directory strings."""
        if value is None:
            return []
        if not isinstance(value, (list, tuple)):
            raise ValueError("recent_paths must be a list of paths")
        seen: set[str] = set()
        result: list[str] = []
        for item in value:
            if item is None or isinstance(item, bool):
                continue
            cleaned = str(item).strip()
            if not cleaned:
                continue
            try:
                resolved = str(Path(cleaned).expanduser().resolve())
            except OSError:
                continue
            if resolved not in seen:
                seen.add(resolved)
                result.append(resolved)
            if len(result) >= 10:
                break
        return result


class ExternalAgentOverride(BaseModel):
    """Per-CLI agent configuration overrides and opt-in permission settings."""

    model_config = ConfigDict(extra="ignore")

    enabled: bool = True
    model: str = ""
    permission_mode: str = "accept_edits"
    custom_path: str = ""
    extra_args: list[str] = Field(default_factory=list)
    allow_autonomous_flags: bool = False

    @field_validator("permission_mode", mode="before")
    @classmethod
    def _validate_permission_mode(cls, value: Any) -> str:
        cleaned = str(value or "accept_edits").strip().lower()
        if cleaned not in VALID_AGENT_PERMISSION_MODES:
            return "accept_edits"
        return cleaned

    @field_validator("extra_args", mode="before")
    @classmethod
    def _validate_extra_args(cls, value: Any) -> list[str]:
        if not isinstance(value, (list, tuple)):
            return []
        return [str(item).strip() for item in value if str(item).strip()]


class CustomCLIAgentConfig(BaseModel):
    """User-defined external AI CLI adapter configuration."""

    model_config = ConfigDict(extra="ignore")

    id: str
    display_name: str
    command: str
    non_interactive_args: list[str] = Field(default_factory=lambda: ["-p", "{prompt}"])
    model_flag: str = "--model"
    enabled: bool = True


class AgentsConfig(BaseModel):
    """God Mode and external AI CLI delegation settings."""

    model_config = ConfigDict(extra="ignore")

    god_mode_enabled: bool = False
    require_run_approval: bool = True
    timeout_seconds: int = 300
    max_output_chars: int = 32768
    pass_workspace_cwd: bool = True
    overrides: dict[str, ExternalAgentOverride] = Field(default_factory=dict)
    custom_agents: list[CustomCLIAgentConfig] = Field(default_factory=list)

    @field_validator("timeout_seconds", mode="before")
    @classmethod
    def _clamp_timeout_seconds(cls, value: Any) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return 300
        return max(10, min(1800, parsed))

    @field_validator("max_output_chars", mode="before")
    @classmethod
    def _clamp_max_output_chars(cls, value: Any) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return 32768
        return max(1024, min(262144, parsed))


class ByteyConfig(BaseModel):
    """Root non-secret configuration schema persisted to config.json."""

    model_config = ConfigDict(extra="ignore")

    general: GeneralConfig = Field(default_factory=GeneralConfig)
    provider: ProviderConfig = Field(default_factory=ProviderConfig)
    voice_in: VoiceInConfig = Field(default_factory=VoiceInConfig)
    voice_out: VoiceOutConfig = Field(default_factory=VoiceOutConfig)
    visuals: VisualsConfig = Field(default_factory=VisualsConfig)
    mcp_servers: dict[str, MCPServerConfig] = Field(default_factory=dict)
    skills: SkillsConfig = Field(default_factory=SkillsConfig)
    workspace: WorkspaceConfig = Field(default_factory=WorkspaceConfig)
    agents: AgentsConfig = Field(default_factory=AgentsConfig)


class SecretStore:
    """Manage secrets across the OS keyring (libsecret) and os.environ."""

    KNOWN_SECRETS: tuple[str, ...] = (
        "GEMINI_API_KEY",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "CUSTOM_LLM_API_KEY",
        "ELEVENLABS_API_KEY",
        "KOKORO_API_KEY",
        "STT_API_KEY",
    )

    _SCHEMA_ID = "io.github.bytey_ai_companion.Secrets"
    _LEGACY_SCHEMA_ID = "io.github.ai_pet.Secrets"

    @staticmethod
    def _build_schema(schema_id: str) -> Any:
        if _LibSecret is None:
            return None
        try:
            return _LibSecret.Schema.new(
                schema_id,
                _LibSecret.SchemaFlags.NONE,
                {
                    "key": _LibSecret.SchemaAttributeType.STRING,
                    "config_dir": _LibSecret.SchemaAttributeType.STRING,
                },
            )
        except Exception:
            return None

    def __init__(self, config_dir: Path | None = None) -> None:
        self.config_dir = resolve_config_dir(config_dir)
        self._allow_legacy_fallback = (
            config_dir is None
            and not os.environ.get("BYTEY_CONFIG_DIR")
            and not os.environ.get("AI_PET_CONFIG_DIR")
        )
        self._schema: Any = self._build_schema(self._SCHEMA_ID)
        self._legacy_schema: Any = self._build_schema(self._LEGACY_SCHEMA_ID)

    def __repr__(self) -> str:
        return f"SecretStore(config_dir={self.config_dir!r})"

    @staticmethod
    def _validate_key(key: str) -> str:
        cleaned = key.strip()
        if not _KEY_NAME_RE.match(cleaned):
            raise ValueError(f"Invalid secret key name: {key!r}")
        return cleaned

    def _keyring_enabled(self) -> bool:
        if _LibSecret is None or self._schema is None:
            return False
        disabled = (
            os.environ.get("BYTEY_DISABLE_KEYRING")
            or os.environ.get("AI_PET_DISABLE_KEYRING")
            or ""
        ).strip().lower()
        return disabled not in ("1", "true", "yes")

    def _keyring_attrs(self, key: str) -> dict[str, str]:
        return {"key": key, "config_dir": str(self.config_dir)}

    @staticmethod
    def _keyring_lookup(schema: Any, attrs: dict[str, str]) -> str | None:
        if schema is None:
            return None
        try:
            value = _LibSecret.password_lookup_sync(schema, attrs, None)
            return value if value else None
        except Exception:
            return None

    @staticmethod
    def _keyring_clear(schema: Any, attrs: dict[str, str]) -> None:
        if schema is None:
            return
        try:
            _LibSecret.password_clear_sync(schema, attrs, None)
        except Exception:
            pass

    def _keyring_get(self, key: str) -> str | None:
        if not self._keyring_enabled():
            return None
        attrs = self._keyring_attrs(key)
        value = self._keyring_lookup(self._schema, attrs)
        if value:
            return value
        if self._legacy_schema is not None:
            value = self._keyring_lookup(self._legacy_schema, attrs)
            if value:
                return value
            if self._allow_legacy_fallback:
                legacy_dir = _legacy_config_dir()
                if legacy_dir != self.config_dir:
                    return self._keyring_lookup(
                        self._legacy_schema,
                        {"key": key, "config_dir": str(legacy_dir)},
                    )
        return None

    def _keyring_set(self, key: str, value: str) -> None:
        if not self._keyring_enabled():
            raise RuntimeError("OS keyring (libsecret) is unavailable or disabled")
        try:
            _LibSecret.password_store_sync(
                self._schema,
                self._keyring_attrs(key),
                _LibSecret.COLLECTION_DEFAULT,
                f"Bytey AI Companion: {key}",
                value,
                None,
            )
        except Exception as exc:
            raise RuntimeError(f"Failed to store secret {key} in OS keyring") from exc

    def _keyring_delete(self, key: str) -> None:
        if not self._keyring_enabled():
            return
        attrs = self._keyring_attrs(key)
        self._keyring_clear(self._schema, attrs)
        if self._legacy_schema is not None:
            self._keyring_clear(self._legacy_schema, attrs)
            if self._allow_legacy_fallback:
                legacy_dir = _legacy_config_dir()
                if legacy_dir != self.config_dir:
                    self._keyring_clear(
                        self._legacy_schema,
                        {"key": key, "config_dir": str(legacy_dir)},
                    )

    def get_secret(self, key: str) -> str | None:
        """Retrieve a secret from libsecret or os.environ without logging it."""
        cleaned = key.strip()
        if not _KEY_NAME_RE.match(cleaned):
            return None

        keyring_val = self._keyring_get(cleaned)
        if keyring_val:
            return keyring_val

        proc_val = os.environ.get(cleaned)
        if proc_val:
            return proc_val

        return None

    def set_secret(self, key: str, value: str) -> None:
        """Persist a secret to the OS keyring (libsecret)."""
        cleaned = self._validate_key(key)
        if "\n" in value or "\r" in value:
            raise ValueError("Secret value must not contain newline characters")
        if value == "":
            self.delete_secret(cleaned)
            return

        self._keyring_set(cleaned, value)

    def delete_secret(self, key: str) -> None:
        """Remove a secret from the OS keyring (libsecret)."""
        cleaned = self._validate_key(key)
        self._keyring_delete(cleaned)

    def list_configured_secrets(self) -> dict[str, bool]:
        """Return presence flags for all KNOWN_SECRETS without exposing values."""
        return {key: bool(self.get_secret(key)) for key in self.KNOWN_SECRETS}

    def resolve_env_value(self, val: str) -> str:
        """Expand ${VAR} references in a string using SecretStore and os.environ."""
        if not val or "${" not in val:
            return val
        return _ENV_VAR_RE.sub(lambda match: self.get_secret(match.group(1)) or "", val)


def _deep_merge_dicts(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge nested dictionaries without mutating inputs."""
    merged = copy.deepcopy(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge_dicts(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


class ConfigManager:
    """Load, validate, update, and redact ByteyConfig persisted in config.json."""

    def __init__(
        self,
        config_dir: Path | None = None,
        secret_store: SecretStore | None = None,
    ) -> None:
        self.config_dir = resolve_config_dir(config_dir)
        self.config_path = self.config_dir / "config.json"
        self.secret_store = secret_store or SecretStore(config_dir=config_dir)
        self._config: ByteyConfig = self.load()

    @property
    def config(self) -> ByteyConfig:
        """Return the currently loaded configuration."""
        return self._config

    def load(self) -> ByteyConfig:
        """Load config.json from disk, falling back to defaults if absent or invalid."""
        if not self.config_path.is_file():
            self._config = ByteyConfig()
            return self._config

        try:
            raw = json.loads(self.config_path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("config.json root must be a JSON object")
            self._config = ByteyConfig.model_validate(raw)
        except Exception as exc:
            logger.warning("Failed to load config from %s: %s", self.config_path, exc)
            self._config = ByteyConfig()
        return self._config

    def save(self) -> None:
        """Atomically persist the current non-secret configuration to config.json."""
        self.config_dir.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self._config.model_dump(mode="json"), indent=2) + "\n"
        fd, tmp_name = tempfile.mkstemp(
            prefix=".config.", suffix=".json.tmp", dir=str(self.config_dir)
        )
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
            os.chmod(tmp_path, 0o600)
            os.replace(tmp_path, self.config_path)
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise

    def update_config(self, patch: dict[str, Any]) -> ByteyConfig:
        """Validate and persist a partial configuration update.

        Standard nested sections (`general`, `provider`, `voice_in`, `voice_out`,
        `visuals`) are deep-merged so single-key patches preserve sibling fields.
        When `"mcp_servers"` is included in `patch`, the server map is replaced as
        a whole so server removals take effect cleanly.
        """
        if not isinstance(patch, dict):
            raise ValueError("Configuration patch must be a dictionary")

        current_data = self._config.model_dump(mode="json")
        prev_provider = (
            str(current_data.get("provider", {}).get("active_provider") or "ollama").strip().lower()
            or "ollama"
        )
        provider_patch = patch.get("provider")
        if isinstance(provider_patch, dict):
            prov_section = current_data.setdefault("provider", {})
            profiles = prov_section.get("profiles")
            if not isinstance(profiles, dict):
                profiles = {}
                prov_section["profiles"] = profiles
            profiles[prev_provider] = {
                field: prov_section.get(field) for field in _PROVIDER_PROFILE_FIELDS
            }

        for section in _MERGE_SECTIONS:
            if section in patch:
                section_patch = patch[section]
                if not isinstance(section_patch, dict):
                    raise ValueError(f"Section {section!r} must be a dictionary")
                current_data[section] = _deep_merge_dicts(
                    current_data.get(section, {}), section_patch
                )

        if isinstance(provider_patch, dict):
            prov_section = current_data["provider"]
            new_provider = (
                str(prov_section.get("active_provider") or prev_provider).strip().lower() or "ollama"
            )
            prov_section["active_provider"] = new_provider
            profiles = prov_section.setdefault("profiles", {})
            if new_provider != prev_provider:
                saved = profiles.get(new_provider)
                defaults = {
                    "model": "",
                    "base_url": PROVIDER_DEFAULT_URLS.get(new_provider, ""),
                    "supports_tools_override": None,
                    "temperature": 0.7,
                    "context_size": None,
                    "focus_window": 0,
                    "max_tool_rounds": 25,
                }
                source = saved if isinstance(saved, dict) else defaults
                for field in _PROVIDER_PROFILE_FIELDS:
                    if field not in provider_patch:
                        prov_section[field] = source.get(field, defaults[field])
            profiles[new_provider] = {
                field: prov_section.get(field) for field in _PROVIDER_PROFILE_FIELDS
            }

        if "mcp_servers" in patch:
            servers_patch = patch["mcp_servers"]
            if not isinstance(servers_patch, dict):
                raise ValueError("Section 'mcp_servers' must be a dictionary")
            from .mcp_host.schema_sync import validate_unique_mcp_urls

            validate_unique_mcp_urls(servers_patch)
            current_data["mcp_servers"] = copy.deepcopy(servers_patch)

        updated = ByteyConfig.model_validate(current_data)
        self._config = updated
        self.save()
        return self._config

    def replace_config(self, new_config: dict[str, Any]) -> ByteyConfig:
        """Validate and persist a full configuration replacement."""
        if not isinstance(new_config, dict):
            raise ValueError("Configuration payload must be a JSON object")

        allowed_sections = set(_MERGE_SECTIONS) | {"mcp_servers"}
        cleaned_data = {
            key: copy.deepcopy(value)
            for key, value in new_config.items()
            if key in allowed_sections
        }
        if not cleaned_data:
            raise ValueError("Invalid configuration JSON: no recognized configuration sections")
        if "mcp_servers" in cleaned_data and isinstance(cleaned_data["mcp_servers"], dict):
            from .mcp_host.schema_sync import validate_unique_mcp_urls

            validate_unique_mcp_urls(cleaned_data["mcp_servers"])

        updated = ByteyConfig.model_validate(cleaned_data)
        self._config = updated
        self.save()
        return self._config

    def get_redacted_dict(self) -> dict[str, Any]:
        """Serialize configuration with secret presence flags and zero secret values."""
        data = self._config.model_dump(mode="json")
        data["secrets_configured"] = self.secret_store.list_configured_secrets()
        return data
