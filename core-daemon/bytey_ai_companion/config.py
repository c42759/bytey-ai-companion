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

from dotenv import dotenv_values, set_key, unset_key
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
_MERGE_SECTIONS = ("general", "provider", "voice_in", "voice_out", "visuals")

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
    if (primary / "config.json").exists() or (primary / ".env").exists():
        return primary
    if (legacy / "config.json").exists() or (legacy / ".env").exists():
        return legacy
    return primary


class GeneralConfig(BaseModel):
    """General assistant identity and system prompt settings."""

    model_config = ConfigDict(extra="ignore")

    pet_name: str = "Bytey"
    wake_phrase: str = "Hey Bytey"
    system_prompt: str = DEFAULT_SYSTEM_PROMPT


class ProviderConfig(BaseModel):
    """LLM provider selection and generation parameters."""

    model_config = ConfigDict(extra="ignore")

    active_provider: str = "ollama"
    model: str = "llama3.2"
    base_url: str = "http://localhost:11434"
    supports_tools_override: bool | None = None
    temperature: float = 0.7
    context_size: int | None = None

    @field_validator("context_size", mode="before")
    @classmethod
    def _validate_context_size(cls, value: Any) -> int | None:
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
    chat_width: int = 380
    chat_height: int = 420
    chat_density: str = "comfortable"
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


class ByteyConfig(BaseModel):
    """Root non-secret configuration schema persisted to config.json."""

    model_config = ConfigDict(extra="ignore")

    general: GeneralConfig = Field(default_factory=GeneralConfig)
    provider: ProviderConfig = Field(default_factory=ProviderConfig)
    voice_in: VoiceInConfig = Field(default_factory=VoiceInConfig)
    voice_out: VoiceOutConfig = Field(default_factory=VoiceOutConfig)
    visuals: VisualsConfig = Field(default_factory=VisualsConfig)
    mcp_servers: dict[str, MCPServerConfig] = Field(default_factory=dict)


class SecretStore:
    """Manage secrets across .env (0o600), optional libsecret keyring, and os.environ."""

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
        self.env_file = self.config_dir / ".env"
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
            return
        try:
            _LibSecret.password_store_sync(
                self._schema,
                self._keyring_attrs(key),
                _LibSecret.COLLECTION_DEFAULT,
                f"Bytey AI Companion: {key}",
                value,
                None,
            )
        except Exception:
            pass

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

    @staticmethod
    def _parse_env_path(path: Path) -> dict[str, str | None]:
        if not path.is_file():
            return {}
        try:
            return dict(dotenv_values(path))
        except Exception:
            logger.warning("Failed to parse .env file at %s", path)
            return {}

    def _read_env_file(self) -> dict[str, str | None]:
        primary_vals = self._parse_env_path(self.env_file)
        if not self._allow_legacy_fallback:
            return primary_vals
        legacy_env = _legacy_config_dir() / ".env"
        if legacy_env != self.env_file and legacy_env.is_file():
            legacy_vals = self._parse_env_path(legacy_env)
            merged = dict(legacy_vals)
            merged.update({k: v for k, v in primary_vals.items() if v})
            return merged
        return primary_vals

    def _ensure_env_file_permissions(self) -> None:
        if not self.env_file.exists():
            fd = os.open(self.env_file, os.O_CREAT | os.O_WRONLY, 0o600)
            os.close(fd)
        os.chmod(self.env_file, 0o600)

    def get_secret(self, key: str) -> str | None:
        """Retrieve a secret from .env, libsecret, or os.environ without logging it."""
        cleaned = key.strip()
        if not _KEY_NAME_RE.match(cleaned):
            return None

        env_values = self._read_env_file()
        env_val = env_values.get(cleaned)
        if env_val:
            return env_val

        keyring_val = self._keyring_get(cleaned)
        if keyring_val:
            return keyring_val

        proc_val = os.environ.get(cleaned)
        if proc_val:
            return proc_val

        return None

    def set_secret(self, key: str, value: str) -> None:
        """Persist a secret to config_dir/.env (chmod 0o600) and optionally libsecret."""
        cleaned = self._validate_key(key)
        if "\n" in value or "\r" in value:
            raise ValueError("Secret value must not contain newline characters")
        if value == "":
            self.delete_secret(cleaned)
            return

        self.config_dir.mkdir(parents=True, exist_ok=True)
        self._ensure_env_file_permissions()
        set_key(str(self.env_file), cleaned, value)
        os.chmod(self.env_file, 0o600)
        self._keyring_set(cleaned, value)

    def delete_secret(self, key: str) -> None:
        """Remove a secret from config_dir/.env and libsecret."""
        cleaned = self._validate_key(key)
        if self.env_file.is_file():
            unset_key(str(self.env_file), cleaned)
            os.chmod(self.env_file, 0o600)
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
        for section in _MERGE_SECTIONS:
            if section in patch:
                section_patch = patch[section]
                if not isinstance(section_patch, dict):
                    raise ValueError(f"Section {section!r} must be a dictionary")
                current_data[section] = _deep_merge_dicts(
                    current_data.get(section, {}), section_patch
                )

        if "mcp_servers" in patch:
            servers_patch = patch["mcp_servers"]
            if not isinstance(servers_patch, dict):
                raise ValueError("Section 'mcp_servers' must be a dictionary")
            current_data["mcp_servers"] = copy.deepcopy(servers_patch)

        updated = ByteyConfig.model_validate(current_data)
        self._config = updated
        self.save()
        return self._config

    def get_redacted_dict(self) -> dict[str, Any]:
        """Serialize configuration with secret presence flags and zero secret values."""
        data = self._config.model_dump(mode="json")
        data["secrets_configured"] = self.secret_store.list_configured_secrets()
        return data
