"""Configuration models, secret store, and config manager for AI Pet."""

from __future__ import annotations

import copy
import json
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from dotenv import dotenv_values, set_key, unset_key
from pydantic import BaseModel, ConfigDict, Field

try:
    import gi

    gi.require_version("Secret", "1")
    from gi.repository import Secret as _LibSecret
except Exception:
    _LibSecret = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

DEFAULT_SYSTEM_PROMPT = (
    "You are Pet, a fast, helpful, and friendly desktop AI assistant.\n"
    "Keep spoken answers concise, direct, and natural (1-3 sentences unless asked for detail).\n"
    "Avoid markdown tables or raw ASCII clutter when replying to voice queries.\n"
    "Begin every reply with a single mood tag from: [emotion:happy], [emotion:curious], "
    "[emotion:surprised], [emotion:sleepy], [emotion:error], or [emotion:neutral].\n"
    "If tools are available and needed, call them concisely and summarize results clearly."
)

_ENV_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_KEY_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_MERGE_SECTIONS = ("general", "provider", "voice_in", "voice_out", "visuals")


def resolve_config_dir(config_dir: Path | None = None) -> Path:
    """Resolve the configuration directory, honoring AI_PET_CONFIG_DIR."""
    if config_dir is not None:
        return Path(config_dir)
    env_dir = os.environ.get("AI_PET_CONFIG_DIR")
    if env_dir:
        return Path(env_dir)
    return Path.home() / ".config" / "ai-pet"


class GeneralConfig(BaseModel):
    """General assistant identity and system prompt settings."""

    model_config = ConfigDict(extra="ignore")

    pet_name: str = "Nova"
    wake_phrase: str = "Hey Nova"
    system_prompt: str = DEFAULT_SYSTEM_PROMPT


class ProviderConfig(BaseModel):
    """LLM provider selection and generation parameters."""

    model_config = ConfigDict(extra="ignore")

    active_provider: str = "ollama"
    model: str = "llama3.2"
    base_url: str = "http://localhost:11434"
    supports_tools_override: bool | None = None
    temperature: float = 0.7


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
    elevenlabs_voice_id: str = "21m00Tcm4TlvDq8ikWAM"
    elevenlabs_model_id: str = "eleven_flash_v2_5"


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


class PetConfig(BaseModel):
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

    _SCHEMA_ID = "io.github.ai_pet.Secrets"

    def __init__(self, config_dir: Path | None = None) -> None:
        self.config_dir = resolve_config_dir(config_dir)
        self.env_file = self.config_dir / ".env"
        self._schema: Any = None
        if _LibSecret is not None:
            try:
                self._schema = _LibSecret.Schema.new(
                    self._SCHEMA_ID,
                    _LibSecret.SchemaFlags.NONE,
                    {
                        "key": _LibSecret.SchemaAttributeType.STRING,
                        "config_dir": _LibSecret.SchemaAttributeType.STRING,
                    },
                )
            except Exception:
                self._schema = None

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
        disabled = os.environ.get("AI_PET_DISABLE_KEYRING", "").strip().lower()
        return disabled not in ("1", "true", "yes")

    def _keyring_attrs(self, key: str) -> dict[str, str]:
        return {"key": key, "config_dir": str(self.config_dir)}

    def _keyring_get(self, key: str) -> str | None:
        if not self._keyring_enabled():
            return None
        try:
            value = _LibSecret.password_lookup_sync(
                self._schema, self._keyring_attrs(key), None
            )
            return value if value else None
        except Exception:
            return None

    def _keyring_set(self, key: str, value: str) -> None:
        if not self._keyring_enabled():
            return
        try:
            _LibSecret.password_store_sync(
                self._schema,
                self._keyring_attrs(key),
                _LibSecret.COLLECTION_DEFAULT,
                f"AI Pet: {key}",
                value,
                None,
            )
        except Exception:
            pass

    def _keyring_delete(self, key: str) -> None:
        if not self._keyring_enabled():
            return
        try:
            _LibSecret.password_clear_sync(
                self._schema, self._keyring_attrs(key), None
            )
        except Exception:
            pass

    def _read_env_file(self) -> dict[str, str | None]:
        if not self.env_file.is_file():
            return {}
        try:
            return dict(dotenv_values(self.env_file))
        except Exception:
            logger.warning("Failed to parse .env file at %s", self.env_file)
            return {}

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
    """Load, validate, update, and redact PetConfig persisted in config.json."""

    def __init__(
        self,
        config_dir: Path | None = None,
        secret_store: SecretStore | None = None,
    ) -> None:
        self.config_dir = resolve_config_dir(config_dir)
        self.config_path = self.config_dir / "config.json"
        self.secret_store = secret_store or SecretStore(config_dir=self.config_dir)
        self._config: PetConfig = self.load()

    @property
    def config(self) -> PetConfig:
        """Return the currently loaded configuration."""
        return self._config

    def load(self) -> PetConfig:
        """Load config.json from disk, falling back to defaults if absent or invalid."""
        if not self.config_path.is_file():
            self._config = PetConfig()
            return self._config

        try:
            raw = json.loads(self.config_path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("config.json root must be a JSON object")
            self._config = PetConfig.model_validate(raw)
        except Exception as exc:
            logger.warning("Failed to load config from %s: %s", self.config_path, exc)
            self._config = PetConfig()
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

    def update_config(self, patch: dict[str, Any]) -> PetConfig:
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

        updated = PetConfig.model_validate(current_data)
        self._config = updated
        self.save()
        return self._config

    def get_redacted_dict(self) -> dict[str, Any]:
        """Serialize configuration with secret presence flags and zero secret values."""
        data = self._config.model_dump(mode="json")
        data["secrets_configured"] = self.secret_store.list_configured_secrets()
        return data
