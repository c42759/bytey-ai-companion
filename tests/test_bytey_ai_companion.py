"""Comprehensive automated test suite for Bytey AI Companion."""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import math
import os
import py_compile
import re
import shutil
import signal
import socket
import sqlite3
import stat
import struct
import subprocess
import sys
import tempfile
import unittest
import wave
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import numpy as np
import websockets
from google.genai import types as genai_types
from pydantic import ValidationError

os.environ["BYTEY_DISABLE_KEYRING"] = "1"

REPO_ROOT = Path(__file__).resolve().parent.parent
CORE_DAEMON_DIR = REPO_ROOT / "core-daemon"
SETTINGS_UI_DIR = REPO_ROOT / "settings-ui"
GNOME_EXT_DIR = REPO_ROOT / "gnome-extension" / "bytey-ai-companion@c42759.gitlab.com"

for _p in (str(CORE_DAEMON_DIR), str(SETTINGS_UI_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from bytey_ai_companion.audio.capture import VoiceInputService  # noqa: E402
from bytey_ai_companion.audio.stt import DEFAULT_OPENAI_STT_URL, STTEngine  # noqa: E402
from bytey_ai_companion.audio.vad import VADProcessor  # noqa: E402
from bytey_ai_companion.audio.wakeword import WakeWordMatcher  # noqa: E402
from bytey_ai_companion.config import (  # noqa: E402
    PIPER_NOISE_SCALE_RANGE,
    PIPER_PITCH_RANGE,
    PIPER_SPEED_RANGE,
    PIPER_VOICE_PRESETS,
    ConfigManager,
    ProviderConfig,
    SecretStore,
    VisualsConfig,
    VoiceOutConfig,
    WorkspaceConfig,
    resolve_config_dir,
)
from bytey_ai_companion.llm.emotions import (  # noqa: E402
    EmotionStreamParser,
    VALID_EMOTIONS,
    parse_emotion_prefix,
    parse_reply_tags,
)
from bytey_ai_companion.llm.router import LLMRouter  # noqa: E402
from bytey_ai_companion.main import ByteyDaemon, _auto_title_from_prompt  # noqa: E402
from bytey_ai_companion.mcp_host import (  # noqa: E402
    MCPClientManager,
    from_mcp_servers_json,
    to_mcp_servers_json,
)
from bytey_ai_companion.skills.loader import (  # noqa: E402
    SkillRegistry,
    is_valid_skill_slug,
    parse_frontmatter,
)
from bytey_ai_companion.skills.runner import SkillScriptRunner  # noqa: E402
from bytey_ai_companion.workspace.tools import (  # noqa: E402
    WorkspaceToolchain,
    load_workspace_rules,
)
from bytey_ai_companion.storage import (  # noqa: E402
    DEFAULT_DB_FILENAME,
    LEGACY_DB_FILENAME,
    StorageManager,
    resolve_data_dir,
    resolve_db_path,
)
from bytey_ai_companion.tts import (  # noqa: E402
    CURATED_PIPER_VOICES,
    PiperTTSClient,
    SentenceChunker,
    StreamingTTSPlayer,
    apply_pitch_shift_wav,
)
from bytey_ai_companion.tts.piper_provider import (  # noqa: E402
    PIPER_VOICES_BASE_URL,
    PIPER_VOICES_INDEX_URL,
    _build_atempo_chain,
)
from bytey_settings.api_client import (  # noqa: E402
    DEFAULT_CONFIG as SETTINGS_DEFAULT_CONFIG,
    DaemonClient,
    resolve_local_config_path,
)
from bytey_settings.window import (  # noqa: E402
    PIPER_VOICE_PRESETS as SETTINGS_PIPER_VOICE_PRESETS,
    POSITION_MODE_OPTIONS as SETTINGS_POSITION_MODE_OPTIONS,
    TTS_PROVIDERS as SETTINGS_TTS_PROVIDERS,
    ByteyPreferencesWindow,
    _match_piper_preset_index,
    _piper_sample_text,
    build_context_size_presets,
    clamp_context_size,
)


MOCK_MCP_SERVER_SCRIPT = '''\
import json
import os
import sys

def main():
    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        req_id = req.get("id")
        method = req.get("method", "")
        params = req.get("params") or {}

        if method.startswith("notifications/"):
            continue

        if method == "initialize":
            result = {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "mock-stdio-mcp", "version": "1.0.0"},
            }
        elif method == "tools/list":
            result = {
                "tools": [
                    {
                        "name": "echo",
                        "description": "Echo text and optional env token.",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "text": {"type": "string"},
                                "include_env": {"type": "boolean"},
                            },
                            "required": ["text"],
                        },
                    },
                    {
                        "name": "secret_ping",
                        "description": "Disabled-by-config test tool.",
                        "inputSchema": {"type": "object", "properties": {}},
                    },
                ]
            }
        elif method == "tools/call":
            name = params.get("name")
            args = params.get("arguments") or {}
            if name == "echo":
                out_text = f"echo:{args.get('text', '')}"
                if args.get("include_env"):
                    out_text += f"|env={os.environ.get('MOCK_MCP_TOKEN', '')}"
                result = {
                    "content": [{"type": "text", "text": out_text}],
                    "isError": False,
                }
            else:
                result = {
                    "content": [{"type": "text", "text": f"Unknown tool: {name}"}],
                    "isError": True,
                }
        else:
            resp = {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32601, "message": f"Method not found: {method}"},
            }
            sys.stdout.write(json.dumps(resp) + "\\n")
            sys.stdout.flush()
            continue

        if req_id is not None:
            resp = {"jsonrpc": "2.0", "id": req_id, "result": result}
            sys.stdout.write(json.dumps(resp) + "\\n")
            sys.stdout.flush()

if __name__ == "__main__":
    main()
'''


def _pcm16_tone(duration_ms: int, amplitude: int, sample_rate: int = 16000, freq_hz: float = 440.0) -> bytes:
    num_samples = int(sample_rate * duration_ms / 1000)
    frames = bytearray()
    for i in range(num_samples):
        if amplitude == 0:
            val = 0
        else:
            val = int(amplitude * math.sin(2.0 * math.pi * freq_hz * i / sample_rate))
        frames.extend(struct.pack("<h", max(-32768, min(32767, val))))
    return bytes(frames)


class _MockLLMRouter:
    """Deterministic async mock for LLMRouter used in live daemon integration tests."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.tool_trigger_phrase = "use_echo_tool"
        self.tool_name_override: str | None = None
        self.tool_arguments: dict[str, Any] | None = None
        self.next_raw_reply: str | None = None

    async def detect_tool_support(
        self,
        provider: str,
        model: str,
        override: bool | None = None,
        base_url: str = "",
        api_key: str | None = None,
    ) -> bool:
        if override is not None:
            return bool(override)
        return (provider or "").lower() in ("gemini", "openai", "anthropic", "custom")

    async def discover_models(
        self,
        provider: str,
        base_url: str = "",
        api_key: str | None = None,
    ) -> dict[str, Any]:
        return {
            "models": [{"id": "gemini-2.5-flash", "name": "Gemini 2.5 Flash", "supports_tools": True}],
            "error": None,
        }

    async def generate_chat(
        self,
        messages: list[dict[str, Any]],
        system_prompt: str,
        provider: str,
        model: str,
        base_url: str = "",
        api_key: str | None = None,
        temperature: float = 0.7,
        tools: list[dict[str, Any]] | None = None,
        on_text_chunk: Any = None,
        on_emotion: Any = None,
        context_size: int | None = None,
    ) -> dict[str, Any]:
        self.calls.append(
            {
                "messages": messages,
                "system_prompt": system_prompt,
                "provider": provider,
                "model": model,
                "tools": tools,
                "context_size": context_size,
            }
        )
        last_msg = messages[-1] if messages else {}

        if last_msg.get("role") == "tool":
            tool_out = str(last_msg.get("content") or "")
            if on_emotion is not None:
                await on_emotion("curious")
            chunks = ["Tool ", f"confirmed: {tool_out}"]
            for ch in chunks:
                if on_text_chunk is not None:
                    await on_text_chunk(ch)
            content = "".join(chunks)
            return {
                "content": content,
                "emotion": "curious",
                "tool_calls": [],
                "raw_assistant_message": {"role": "assistant", "content": content},
            }

        user_text = str(last_msg.get("content") or "")
        if self.tool_trigger_phrase in user_text and tools:
            if on_emotion is not None:
                await on_emotion("curious")
            tool_name = self.tool_name_override or tools[0]["function"]["name"]
            tc = {
                "id": "call_mock_1",
                "name": tool_name,
                "arguments": dict(self.tool_arguments) if self.tool_arguments is not None else {"text": "daemon-ok"},
            }
            return {
                "content": "",
                "emotion": "curious",
                "tool_calls": [tc],
                "raw_assistant_message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": tc["id"],
                            "type": "function",
                            "function": {
                                "name": tc["name"],
                                "arguments": json.dumps(tc["arguments"]),
                            },
                        }
                    ],
                },
            }

        if self.next_raw_reply is not None:
            raw_reply = self.next_raw_reply
            self.next_raw_reply = None
            parser = EmotionStreamParser(on_emotion=on_emotion)
            out_parts: list[str] = []
            for idx in range(0, len(raw_reply), 5):
                clean_delta = parser.feed(raw_reply[idx : idx + 5])
                if clean_delta:
                    out_parts.append(clean_delta)
                    if on_text_chunk is not None:
                        await on_text_chunk(clean_delta)
            tail = parser.flush()
            if tail:
                out_parts.append(tail)
                if on_text_chunk is not None:
                    await on_text_chunk(tail)
            await parser.wait_callbacks()
            content = "".join(out_parts)
            return {
                "content": content,
                "emotion": parser.emotion or "neutral",
                "title": parser.captured_title,
                "tool_calls": [],
                "raw_assistant_message": {"role": "assistant", "content": content},
            }

        if on_emotion is not None:
            await on_emotion("happy")
        reply_chunks = ["Hello ", "from mocked ", "assistant!"]
        for ch in reply_chunks:
            if on_text_chunk is not None:
                await on_text_chunk(ch)
        content = "".join(reply_chunks)
        return {
            "content": content,
            "emotion": "happy",
            "tool_calls": [],
            "raw_assistant_message": {"role": "assistant", "content": content},
        }

    async def close(self) -> None:
        return None


class _CapabilitiesMockLLMRouter(_MockLLMRouter):
    """Mock LLMRouter exposing detect_model_capabilities for context clamping tests."""

    def __init__(self, *, supports_tools: bool = True, max_context_size: int | None = 8192) -> None:
        super().__init__()
        self.supports_tools = supports_tools
        self.max_context_size = max_context_size

    async def detect_model_capabilities(
        self,
        provider: str,
        model: str,
        override: bool | None = None,
        base_url: str = "",
        api_key: str | None = None,
    ) -> tuple[bool, int | None]:
        del provider, model, base_url, api_key
        tools = bool(override) if override is not None else self.supports_tools
        return tools, self.max_context_size


class TestConfigAndSecrets(unittest.TestCase):
    """Test ConfigManager and SecretStore behavior."""

    def setUp(self) -> None:
        os.environ["BYTEY_DISABLE_KEYRING"] = "1"
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.cfg_dir = Path(self.tmp_dir.name)

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()

    def test_secret_store_keyring_redaction_and_expansion(self) -> None:
        fake_keyring: dict[tuple[str, str], str] = {}
        store = SecretStore(config_dir=self.cfg_dir)

        with patch.dict(os.environ, {"BYTEY_DISABLE_KEYRING": "1"}):
            with self.assertRaises(RuntimeError):
                store.set_secret("OPENAI_API_KEY", "sk-should-fail")

        with patch.dict(os.environ, {"BYTEY_DISABLE_KEYRING": ""}), patch.object(
            store, "_keyring_enabled", return_value=True
        ), patch.object(
            store,
            "_keyring_set",
            side_effect=lambda k, v: fake_keyring.__setitem__((str(store.config_dir), k), v),
        ), patch.object(
            store,
            "_keyring_get",
            side_effect=lambda k: fake_keyring.get((str(store.config_dir), k)),
        ), patch.object(
            store,
            "_keyring_delete",
            side_effect=lambda k: fake_keyring.pop((str(store.config_dir), k), None),
        ):
            secret_val = "sk-super-secret-value-9988"
            store.set_secret("OPENAI_API_KEY", secret_val)

            self.assertEqual(store.get_secret("OPENAI_API_KEY"), secret_val)

            os.environ["TEMP_TEST_HOST_VAR"] = "env-fallback-xyz"
            try:
                expanded = store.resolve_env_value("key=${OPENAI_API_KEY}&host=${TEMP_TEST_HOST_VAR}")
                self.assertEqual(expanded, f"key={secret_val}&host=env-fallback-xyz")
            finally:
                os.environ.pop("TEMP_TEST_HOST_VAR", None)

            cfg_mgr = ConfigManager(config_dir=self.cfg_dir, secret_store=store)
            redacted = cfg_mgr.get_redacted_dict()
            self.assertTrue(redacted["secrets_configured"]["OPENAI_API_KEY"])
            self.assertFalse(redacted["secrets_configured"]["GEMINI_API_KEY"])
            self.assertNotIn(secret_val, json.dumps(redacted))

            store.delete_secret("OPENAI_API_KEY")
            self.assertIsNone(store.get_secret("OPENAI_API_KEY"))

    def test_config_patch_merging_and_persistence(self) -> None:
        store = SecretStore(config_dir=self.cfg_dir)
        mgr = ConfigManager(config_dir=self.cfg_dir, secret_store=store)

        updated = mgr.update_config(
            {
                "provider": {"active_provider": "gemini", "model": "gemini-2.5-flash"},
                "visuals": {"position_mode": "floating", "float_x": 222},
            }
        )
        self.assertEqual(updated.provider.active_provider, "gemini")
        self.assertEqual(updated.provider.model, "gemini-2.5-flash")
        self.assertEqual(updated.provider.temperature, 0.7)
        self.assertEqual(updated.visuals.position_mode, "floating")
        self.assertEqual(updated.visuals.float_x, 222)
        self.assertEqual(updated.visuals.float_y, 40)

        self.assertEqual(stat.S_IMODE(mgr.config_path.stat().st_mode), 0o600)
        reloaded = ConfigManager(config_dir=self.cfg_dir, secret_store=store)
        self.assertEqual(reloaded.config.provider.model, "gemini-2.5-flash")
        self.assertEqual(reloaded.config.visuals.float_x, 222)

    def test_legacy_config_dir_and_env_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as home_tmp:
            fake_home = Path(home_tmp)
            primary_dir = fake_home / ".config" / "bytey-ai-companion"
            legacy_dir = fake_home / ".config" / "ai-pet"
            clean_env = {
                k: v
                for k, v in os.environ.items()
                if k
                not in (
                    "BYTEY_CONFIG_DIR",
                    "AI_PET_CONFIG_DIR",
                    "BYTEY_DISABLE_KEYRING",
                    "AI_PET_DISABLE_KEYRING",
                )
            }

            with patch.dict(os.environ, clean_env, clear=True), patch(
                "pathlib.Path.home", return_value=fake_home
            ):
                # Fresh home resolves to primary bytey-ai-companion config dir
                self.assertEqual(resolve_config_dir(), primary_dir)

                # Legacy ~/.config/ai-pet/config.json falls back when primary is absent
                legacy_dir.mkdir(parents=True, exist_ok=True)
                (legacy_dir / "config.json").write_text("{}", encoding="utf-8")
                self.assertEqual(resolve_config_dir(), legacy_dir)

                # Primary ~/.config/bytey-ai-companion/config.json wins when present
                primary_dir.mkdir(parents=True, exist_ok=True)
                (primary_dir / "config.json").write_text("{}", encoding="utf-8")
                self.assertEqual(resolve_config_dir(), primary_dir)

                # AI_PET_CONFIG_DIR fallback and BYTEY_CONFIG_DIR override
                env_legacy = fake_home / "env-legacy-cfg"
                env_primary = fake_home / "env-primary-cfg"
                with patch.dict(os.environ, {"AI_PET_CONFIG_DIR": str(env_legacy)}):
                    self.assertEqual(resolve_config_dir(), env_legacy)
                    with patch.dict(os.environ, {"BYTEY_CONFIG_DIR": str(env_primary)}):
                        self.assertEqual(resolve_config_dir(), env_primary)

                # Keyring disable flag checks (primary + legacy)
                with patch("bytey_ai_companion.config._LibSecret", object()), patch.object(
                    SecretStore, "_build_schema", return_value=object()
                ):
                    self.assertTrue(SecretStore(config_dir=self.cfg_dir)._keyring_enabled())
                    with patch.dict(os.environ, {"BYTEY_DISABLE_KEYRING": "1"}):
                        self.assertFalse(SecretStore(config_dir=self.cfg_dir)._keyring_enabled())
                    with patch.dict(os.environ, {"AI_PET_DISABLE_KEYRING": "1"}):
                        self.assertFalse(SecretStore(config_dir=self.cfg_dir)._keyring_enabled())

    def test_visuals_color_validation_and_patch_roundtrip(self) -> None:
        store = SecretStore(config_dir=self.cfg_dir)
        mgr = ConfigManager(config_dir=self.cfg_dir, secret_store=store)

        self.assertEqual(mgr.config.visuals.eye_color, "#F7FAFF")
        self.assertEqual(mgr.config.visuals.background_color, "#0A0A0C")
        self.assertEqual(mgr.config.visuals.border_color, "#2A2C34")

        normalized = VisualsConfig(
            eye_color="#0f8",
            background_color=" 112233cc ",
            border_color="invalid",
        )
        self.assertEqual(normalized.eye_color, "#00FF88")
        self.assertEqual(normalized.background_color, "#112233CC")
        self.assertEqual(normalized.border_color, "#2A2C34")

        invalid_all = VisualsConfig(
            eye_color="invalid",
            background_color="not-a-hex",
            border_color="",
        )
        self.assertEqual(invalid_all.eye_color, "#F7FAFF")
        self.assertEqual(invalid_all.background_color, "#0A0A0C")
        self.assertEqual(invalid_all.border_color, "#2A2C34")

        updated = mgr.update_config(
            {
                "visuals": {
                    "eye_color": "#00ff88",
                    "background_color": "#101218",
                    "border_color": "#ff00aa80",
                }
            }
        )
        self.assertEqual(updated.visuals.eye_color, "#00FF88")
        self.assertEqual(updated.visuals.background_color, "#101218")
        self.assertEqual(updated.visuals.border_color, "#FF00AA80")

        reloaded = ConfigManager(config_dir=self.cfg_dir, secret_store=store)
        self.assertEqual(reloaded.config.visuals.eye_color, "#00FF88")
        self.assertEqual(reloaded.config.visuals.background_color, "#101218")
        self.assertEqual(reloaded.config.visuals.border_color, "#FF00AA80")

    def test_visuals_position_mode_validation_and_patch_roundtrip(self) -> None:
        self.assertEqual(VisualsConfig().position_mode, "fixed")
        for raw, expected in (
            ("fixed", "fixed"),
            ("floating", "floating"),
            ("notch", "notch"),
            ("Notch", "notch"),
            ("  NOTCH  ", "notch"),
            ("invalid", "fixed"),
            (None, "fixed"),
        ):
            with self.subTest(raw=raw):
                self.assertEqual(VisualsConfig(position_mode=raw).position_mode, expected)

        store = SecretStore(config_dir=self.cfg_dir)
        mgr = ConfigManager(config_dir=self.cfg_dir, secret_store=store)
        updated = mgr.update_config({"visuals": {"position_mode": "notch"}})
        self.assertEqual(updated.visuals.position_mode, "notch")

        reloaded = ConfigManager(config_dir=self.cfg_dir, secret_store=store)
        self.assertEqual(reloaded.config.visuals.position_mode, "notch")

    def test_provider_context_size_validation_and_patch_roundtrip(self) -> None:
        self.assertIsNone(ProviderConfig().context_size)
        for raw in (None, "", "   ", 0, "0", "auto", "Auto", "default", "none", True, False, -1, "-1"):
            with self.subTest(raw=raw):
                self.assertIsNone(ProviderConfig(context_size=raw).context_size)

        self.assertEqual(ProviderConfig(context_size="16384").context_size, 16384)
        self.assertEqual(ProviderConfig(context_size=16384).context_size, 16384)
        self.assertEqual(ProviderConfig(context_size=" 32768 ").context_size, 32768)
        with self.assertRaises(ValidationError):
            ProviderConfig(context_size="not-an-int")

        store = SecretStore(config_dir=self.cfg_dir)
        mgr = ConfigManager(config_dir=self.cfg_dir, secret_store=store)
        updated = mgr.update_config({"provider": {"context_size": 16384}})
        self.assertEqual(updated.provider.context_size, 16384)

        reloaded = ConfigManager(config_dir=self.cfg_dir, secret_store=store)
        self.assertEqual(reloaded.config.provider.context_size, 16384)

        cleared = mgr.update_config({"provider": {"context_size": None}})
        self.assertIsNone(cleared.provider.context_size)
        reloaded_cleared = ConfigManager(config_dir=self.cfg_dir, secret_store=store)
        self.assertIsNone(reloaded_cleared.config.provider.context_size)

    def test_voice_out_piper_validation_and_roundtrip(self) -> None:
        defaults = VoiceOutConfig()
        self.assertEqual(defaults.tts_provider, "kokoro")
        self.assertEqual(defaults.piper_model, "en_US-lessac-medium")
        self.assertEqual(defaults.piper_models_dir, "")
        self.assertEqual(defaults.piper_speed, 1.0)
        self.assertEqual(defaults.piper_pitch, 0.0)
        self.assertEqual(defaults.piper_noise_scale, 0.667)
        self.assertIsNone(defaults.piper_speaker_id)

        # Range clamping
        self.assertEqual(VoiceOutConfig(piper_speed=0.1).piper_speed, 0.4)
        self.assertEqual(VoiceOutConfig(piper_speed=-5).piper_speed, 0.4)
        self.assertEqual(VoiceOutConfig(piper_speed=9.0).piper_speed, 2.5)
        self.assertEqual(VoiceOutConfig(piper_speed="1.35").piper_speed, 1.35)

        self.assertEqual(VoiceOutConfig(piper_pitch=-20).piper_pitch, -12.0)
        self.assertEqual(VoiceOutConfig(piper_pitch=99).piper_pitch, 12.0)
        self.assertEqual(VoiceOutConfig(piper_pitch="-4.5").piper_pitch, -4.5)

        self.assertEqual(VoiceOutConfig(piper_noise_scale=-1).piper_noise_scale, 0.0)
        self.assertEqual(VoiceOutConfig(piper_noise_scale=3.0).piper_noise_scale, 1.5)
        self.assertEqual(VoiceOutConfig(piper_noise_scale="0.85").piper_noise_scale, 0.85)

        # Invalid / bool / empty fallbacks
        for bad in (None, True, False, "", "   ", "not-a-num", float("nan"), float("inf"), float("-inf")):
            with self.subTest(bad=bad):
                cfg = VoiceOutConfig(
                    piper_speed=bad,
                    piper_pitch=bad,
                    piper_noise_scale=bad,
                )
                self.assertEqual(cfg.piper_speed, 1.0)
                self.assertEqual(cfg.piper_pitch, 0.0)
                self.assertEqual(cfg.piper_noise_scale, 0.667)

        for bad_str in (None, True, False, "", "   "):
            with self.subTest(bad_str=bad_str):
                cfg = VoiceOutConfig(
                    tts_provider=bad_str,
                    piper_model=bad_str,
                    piper_models_dir=bad_str,
                )
                self.assertEqual(cfg.tts_provider, "kokoro")
                self.assertEqual(cfg.piper_model, "en_US-lessac-medium")
                self.assertEqual(cfg.piper_models_dir, "")

        self.assertEqual(VoiceOutConfig(tts_provider="  PIPER  ").tts_provider, "piper")
        self.assertEqual(
            VoiceOutConfig(piper_model="  pt_PT-tugão-medium  ").piper_model,
            "pt_PT-tugão-medium",
        )
        self.assertEqual(
            VoiceOutConfig(piper_models_dir="  /tmp/piper-models  ").piper_models_dir,
            "/tmp/piper-models",
        )

        # piper_speaker_id validation & clamping
        for bad_sid in (None, True, False, "", "   ", "bad"):
            with self.subTest(bad_sid=bad_sid):
                self.assertIsNone(VoiceOutConfig(piper_speaker_id=bad_sid).piper_speaker_id)
        for neg_sid in (-5, -1, "-3", 0, "0"):
            with self.subTest(neg_sid=neg_sid):
                self.assertEqual(VoiceOutConfig(piper_speaker_id=neg_sid).piper_speaker_id, 0)
        self.assertEqual(VoiceOutConfig(piper_speaker_id=4).piper_speaker_id, 4)
        self.assertEqual(VoiceOutConfig(piper_speaker_id=" 7 ").piper_speaker_id, 7)

        # ConfigManager / ByteyConfig persistence
        store = SecretStore(config_dir=self.cfg_dir)
        mgr = ConfigManager(config_dir=self.cfg_dir, secret_store=store)
        updated = mgr.update_config(
            {
                "voice_out": {
                    "tts_provider": "piper",
                    "piper_model": "pt_PT-tugão-medium",
                    "piper_models_dir": "/opt/piper",
                    "piper_speed": 1.25,
                    "piper_pitch": 8.0,
                    "piper_noise_scale": 0.8,
                    "piper_speaker_id": 2,
                }
            }
        )
        self.assertEqual(updated.voice_out.tts_provider, "piper")
        self.assertEqual(updated.voice_out.piper_model, "pt_PT-tugão-medium")
        self.assertEqual(updated.voice_out.piper_models_dir, "/opt/piper")
        self.assertEqual(updated.voice_out.piper_speed, 1.25)
        self.assertEqual(updated.voice_out.piper_pitch, 8.0)
        self.assertEqual(updated.voice_out.piper_noise_scale, 0.8)
        self.assertEqual(updated.voice_out.piper_speaker_id, 2)

        reloaded = ConfigManager(config_dir=self.cfg_dir, secret_store=store)
        self.assertEqual(reloaded.config.voice_out.piper_model, "pt_PT-tugão-medium")
        self.assertEqual(reloaded.config.voice_out.piper_pitch, 8.0)
        self.assertEqual(reloaded.config.voice_out.piper_speaker_id, 2)

        partial = mgr.update_config({"voice_out": {"piper_pitch": -20, "piper_speaker_id": None}})
        self.assertEqual(partial.voice_out.piper_pitch, -12.0)
        self.assertIsNone(partial.voice_out.piper_speaker_id)
        self.assertEqual(partial.voice_out.piper_model, "pt_PT-tugão-medium")

        # ByteyDaemon _normalize_patch & _serialize_config via voice_out and voice_output
        daemon = ByteyDaemon(
            config_dir=self.cfg_dir,
            data_dir=self.cfg_dir / "data",
            enable_voice_capture=False,
        )
        canon_out, _ = daemon._normalize_patch(
            {
                "voice_out": {
                    "tts_provider": "piper",
                    "piper_model": "de_DE-thorsten-medium",
                    "piper_models_dir": "/var/piper",
                    "piper_speed": 1.4,
                    "piper_pitch": -3.5,
                    "piper_noise_scale": 0.5,
                    "piper_speaker_id": 1,
                }
            }
        )
        self.assertEqual(canon_out["voice_out"]["tts_provider"], "piper")
        self.assertEqual(canon_out["voice_out"]["piper_model"], "de_DE-thorsten-medium")
        self.assertEqual(canon_out["voice_out"]["piper_speaker_id"], 1)

        canon_output, _ = daemon._normalize_patch(
            {
                "voice_output": {
                    "provider": "piper",
                    "piper_model": "fr_FR-siwis-medium",
                    "piper_models_dir": "/tmp/piper",
                    "piper_speed": 0.75,
                    "piper_pitch": 6.0,
                    "piper_noise_scale": 0.9,
                    "piper_speaker_id": 3,
                }
            }
        )
        self.assertEqual(canon_output["voice_out"]["tts_provider"], "piper")
        self.assertEqual(canon_output["voice_out"]["piper_model"], "fr_FR-siwis-medium")
        self.assertEqual(canon_output["voice_out"]["piper_models_dir"], "/tmp/piper")
        self.assertEqual(canon_output["voice_out"]["piper_speed"], 0.75)
        self.assertEqual(canon_output["voice_out"]["piper_pitch"], 6.0)
        self.assertEqual(canon_output["voice_out"]["piper_noise_scale"], 0.9)
        self.assertEqual(canon_output["voice_out"]["piper_speaker_id"], 3)

        daemon.config_manager.update_config(canon_output)
        serialized = daemon._serialize_config()
        self.assertEqual(serialized["voice_out"]["tts_provider"], "piper")
        self.assertEqual(serialized["voice_out"]["piper_model"], "fr_FR-siwis-medium")
        self.assertEqual(serialized["voice_output"]["provider"], "piper")
        self.assertEqual(serialized["voice_output"]["tts_provider"], "piper")
        self.assertEqual(serialized["voice_output"]["piper_model"], "fr_FR-siwis-medium")
        self.assertEqual(serialized["voice_output"]["piper_models_dir"], "/tmp/piper")
        self.assertEqual(serialized["voice_output"]["piper_speed"], 0.75)
        self.assertEqual(serialized["voice_output"]["piper_pitch"], 6.0)
        self.assertEqual(serialized["voice_output"]["piper_noise_scale"], 0.9)
        self.assertEqual(serialized["voice_output"]["piper_speaker_id"], 3)


class TestStorageManager(unittest.IsolatedAsyncioTestCase):
    """Test SQLite StorageManager CRUD, tool_call attachment, and FK cascades."""

    async def asyncSetUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp_dir.name) / "bytey.db"
        self.storage = StorageManager(db_path=self.db_path)
        await self.storage.init_db()

    async def asyncTearDown(self) -> None:
        await self.storage.close()
        self.tmp_dir.cleanup()

    async def test_crud_tool_attachment_and_fk_cascade(self) -> None:
        s1 = await self.storage.create_session("First Session")
        s2 = await self.storage.create_session("Second Session")
        sessions = await self.storage.list_sessions()
        self.assertEqual(len(sessions), 2)
        active = await self.storage.get_or_create_active_session()
        self.assertEqual(active["id"], s2["id"])

        u_msg = await self.storage.add_message(s1["id"], role="user", content="Run tool")
        a_msg = await self.storage.add_message(s1["id"], role="assistant", content="")

        tc = await self.storage.add_tool_call(
            message_id=a_msg["id"],
            server_name="fs",
            tool_name="read_file",
            arguments_json={"path": "/tmp/x"},
            status="running",
        )
        tc_updated = await self.storage.update_tool_call(tc["id"], "ok", "read 10 bytes")
        self.assertIsNotNone(tc_updated)
        self.assertEqual(tc_updated["status"], "ok")
        self.assertEqual(tc_updated["result_summary"], "read 10 bytes")

        a_updated = await self.storage.update_message(a_msg["id"], "Done reading.", emotion="happy")
        self.assertIsNotNone(a_updated)
        self.assertEqual(a_updated["emotion"], "happy")
        self.assertEqual(len(a_updated["tool_calls"]), 1)

        listed = await self.storage.list_messages(s1["id"])
        self.assertEqual([m["id"] for m in listed], [u_msg["id"], a_msg["id"]])
        self.assertEqual(listed[0]["tool_calls"], [])
        self.assertEqual(len(listed[1]["tool_calls"]), 1)
        self.assertEqual(listed[1]["tool_calls"][0]["tool_name"], "read_file")

        deleted = await self.storage.delete_session(s1["id"])
        self.assertTrue(deleted)
        self.assertEqual(await self.storage.list_messages(s1["id"]), [])

        with contextlib.closing(sqlite3.connect(str(self.db_path))) as conn:
            msg_count = conn.execute(
                "SELECT COUNT(*) FROM messages WHERE session_id = ?", (s1["id"],)
            ).fetchone()[0]
            tc_count = conn.execute(
                "SELECT COUNT(*) FROM tool_calls WHERE message_id = ?", (a_msg["id"],)
            ).fetchone()[0]
        self.assertEqual(msg_count, 0)
        self.assertEqual(tc_count, 0)

    async def test_list_sessions_enrichment_and_rename_session(self) -> None:
        s1 = await self.storage.create_session("Older Session")
        await asyncio.sleep(0.01)
        s2 = await self.storage.create_session("Newer Session")

        # Initial ordering: s2 (more recently created) is first, message_count=0, preview=""
        listed = await self.storage.list_sessions()
        self.assertEqual([s["id"] for s in listed], [s2["id"], s1["id"]])
        self.assertEqual(listed[0]["message_count"], 0)
        self.assertEqual(listed[0]["preview"], "")
        self.assertIn("updated_at", listed[0])

        # Adding a message to s1 bumps its updated_at and moves it to the top
        await asyncio.sleep(0.01)
        long_prompt = "   Hello   multichat\nworld! " + ("x" * 90)
        await self.storage.add_message(s1["id"], role="user", content=long_prompt)
        await self.storage.add_message(s1["id"], role="assistant", content="   ")  # blank ignored for preview
        listed_after = await self.storage.list_sessions()
        self.assertEqual([s["id"] for s in listed_after], [s1["id"], s2["id"]])
        self.assertEqual(listed_after[0]["message_count"], 2)
        self.assertEqual(len(listed_after[0]["preview"]), 80)
        self.assertTrue(listed_after[0]["preview"].endswith("..."))
        self.assertNotIn("\n", listed_after[0]["preview"])

        # rename_session: whitespace strip, 80-char truncation, empty fallback, missing -> None
        long_title = "   " + ("T" * 95) + "   "
        renamed = await self.storage.rename_session(s2["id"], long_title)
        self.assertIsNotNone(renamed)
        self.assertEqual(renamed["title"], "T" * 80)

        fallback = await self.storage.rename_session(s2["id"], "   \t\n  ")
        self.assertIsNotNone(fallback)
        self.assertEqual(fallback["title"], "New Chat")

        missing = await self.storage.rename_session("nonexistent-session-id", "Valid Title")
        self.assertIsNone(missing)

    async def test_delete_message_cascades_and_missing(self) -> None:
        s1 = await self.storage.create_session("Session One")
        s2 = await self.storage.create_session("Session Two")

        m1 = await self.storage.add_message(s1["id"], role="assistant", content="With tool")
        m2 = await self.storage.add_message(s1["id"], role="user", content="Keep me")
        await self.storage.add_tool_call(
            message_id=m1["id"],
            server_name="fs",
            tool_name="read_file",
            arguments_json={"path": "/tmp/y"},
            status="ok",
        )

        # Wrong session or nonexistent message -> False
        self.assertFalse(await self.storage.delete_message(s2["id"], m1["id"]))
        self.assertFalse(await self.storage.delete_message(s1["id"], "nonexistent-msg-id"))

        # Valid delete -> True, removes message + cascaded tool_calls
        self.assertTrue(await self.storage.delete_message(s1["id"], m1["id"]))
        remaining = await self.storage.list_messages(s1["id"])
        self.assertEqual([m["id"] for m in remaining], [m2["id"]])

        with contextlib.closing(sqlite3.connect(str(self.db_path))) as conn:
            msg_count = conn.execute(
                "SELECT COUNT(*) FROM messages WHERE id = ?", (m1["id"],)
            ).fetchone()[0]
            tc_count = conn.execute(
                "SELECT COUNT(*) FROM tool_calls WHERE message_id = ?", (m1["id"],)
            ).fetchone()[0]
        self.assertEqual(msg_count, 0)
        self.assertEqual(tc_count, 0)


class TestStoragePathFallback(unittest.TestCase):
    """Test legacy data directory and SQLite path fallback resolution."""

    def test_legacy_data_dir_and_db_path_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_home = root / "home"
            fake_home.mkdir()
            primary_dir = fake_home / ".local" / "share" / "bytey-ai-companion"
            legacy_dir = fake_home / ".local" / "share" / "ai-pet"
            clean_env = {
                k: v
                for k, v in os.environ.items()
                if k not in ("BYTEY_DATA_DIR", "AI_PET_DATA_DIR")
            }

            with patch.dict(os.environ, clean_env, clear=True), patch(
                "pathlib.Path.home", return_value=fake_home
            ):
                # Fresh home resolves to bytey-ai-companion and bytey.db
                self.assertEqual(resolve_data_dir(), primary_dir)
                self.assertEqual(resolve_db_path(), primary_dir / DEFAULT_DB_FILENAME)

                # Legacy ~/.local/share/ai-pet/pet.db falls back when primary is absent
                legacy_dir.mkdir(parents=True, exist_ok=True)
                (legacy_dir / LEGACY_DB_FILENAME).write_text("", encoding="utf-8")
                self.assertEqual(resolve_data_dir(), legacy_dir)
                self.assertEqual(resolve_db_path(), legacy_dir / LEGACY_DB_FILENAME)

                # Primary ~/.local/share/bytey-ai-companion/bytey.db wins when present
                primary_dir.mkdir(parents=True, exist_ok=True)
                (primary_dir / DEFAULT_DB_FILENAME).write_text("", encoding="utf-8")
                self.assertEqual(resolve_data_dir(), primary_dir)
                self.assertEqual(resolve_db_path(), primary_dir / DEFAULT_DB_FILENAME)

                # resolve_db_path(explicit_dir) picks pet.db if only pet.db exists, prefers bytey.db if both exist
                explicit_dir = root / "explicit"
                explicit_dir.mkdir()
                self.assertEqual(resolve_db_path(explicit_dir), explicit_dir / DEFAULT_DB_FILENAME)
                (explicit_dir / LEGACY_DB_FILENAME).write_text("", encoding="utf-8")
                self.assertEqual(resolve_db_path(explicit_dir), explicit_dir / LEGACY_DB_FILENAME)
                (explicit_dir / DEFAULT_DB_FILENAME).write_text("", encoding="utf-8")
                self.assertEqual(resolve_db_path(explicit_dir), explicit_dir / DEFAULT_DB_FILENAME)

                # AI_PET_DATA_DIR fallback and BYTEY_DATA_DIR override
                env_legacy = root / "env-legacy-data"
                env_primary = root / "env-primary-data"
                with patch.dict(os.environ, {"AI_PET_DATA_DIR": str(env_legacy)}):
                    self.assertEqual(resolve_data_dir(), env_legacy)
                    with patch.dict(os.environ, {"BYTEY_DATA_DIR": str(env_primary)}):
                        self.assertEqual(resolve_data_dir(), env_primary)


class TestWakeWordAndVAD(unittest.TestCase):
    """Test WakeWordMatcher and VADProcessor."""

    def test_wake_word_matches_phonetic_and_rejects_negative(self) -> None:
        matched, rem = WakeWordMatcher.match_wake_phrase("Hey Bytey", "Hey Bytey")
        self.assertTrue(matched)
        self.assertEqual(rem, "")

        matched, rem = WakeWordMatcher.match_wake_phrase(
            "hey bytey what is the status", "Hey Bytey"
        )
        self.assertTrue(matched)
        self.assertEqual(rem, "what is the status")

        matched, rem = WakeWordMatcher.match_wake_phrase(
            "Hey Bitey check calendar", "Hey Bytey"
        )
        self.assertTrue(matched)
        self.assertEqual(rem, "check calendar")

        matched, rem = WakeWordMatcher.match_wake_phrase("hello world", "Hey Bytey")
        self.assertFalse(matched)
        self.assertEqual(rem, "")

    def test_vad_rms_and_utterance_onset_end_cycle(self) -> None:
        silent_frame = _pcm16_tone(30, amplitude=0)
        loud_frame = _pcm16_tone(30, amplitude=12000)

        self.assertEqual(VADProcessor.compute_rms(silent_frame), 0.0)
        self.assertGreater(VADProcessor.compute_rms(loud_frame), 0.2)

        vad = VADProcessor(
            sample_rate=16000,
            frame_ms=30,
            vad_threshold=0.015,
            silence_timeout_ms=120,
            pre_roll_ms=60,
            onset_window_ms=90,
        )
        vad._silero_model = None  # Force deterministic RMS path for unit testing.

        res_idle = vad.process_frame(silent_frame)
        self.assertEqual(res_idle["state"], "idle")
        self.assertFalse(res_idle["is_speech"])

        states: list[str] = []
        onset_audio: bytes | None = None
        for _ in range(4):
            res = vad.process_frame(loud_frame)
            states.append(res["state"])
            if res["state"] == "onset":
                onset_audio = res["onset_audio"]

        self.assertIn("onset", states)
        self.assertIsNotNone(onset_audio)
        self.assertGreater(len(onset_audio), len(loud_frame))

        utterance_audio: bytes | None = None
        end_state = None
        for _ in range(6):
            res = vad.process_frame(silent_frame)
            if res["state"] == "utterance_end":
                end_state = res["state"]
                utterance_audio = res["utterance_audio"]
                break

        self.assertEqual(end_state, "utterance_end")
        self.assertIsNotNone(utterance_audio)
        self.assertGreater(len(utterance_audio), len(onset_audio))


class TestChunkerAndEmotions(unittest.TestCase):
    """Test SentenceChunker and EmotionStreamParser."""

    def test_sentence_chunker_avoids_splitting_abbreviations_decimals_and_urls(self) -> None:
        chunker = SentenceChunker(min_chars=10)
        text = (
            "Dr. Smith checked https://example.com/a.b and found 3.14 today. "
            "Then v1.2. was released! Final words"
        )
        emitted: list[str] = []
        for i in range(0, len(text), 4):
            emitted.extend(chunker.feed(text[i : i + 4]))
        tail = chunker.flush()
        if tail:
            emitted.append(tail)

        self.assertEqual(
            emitted,
            [
                "Dr. Smith checked https://example.com/a.b and found 3.14 today.",
                "Then v1.2. was released!",
                "Final words",
            ],
        )

    def test_sentence_chunker_ignores_fenced_code_blocks_and_keeps_inline_code(self) -> None:
        text = (
            "Here is the snippet:\n"
            "```python\n"
            "def greet(name):\n"
            "    return f'Hello, {name}!'\n"
            "```\n"
            "Use 'greet(\"Bytey\")' or `greet('world')` to run it. "
            "Another example:\n"
            "'''js\n"
            "console.log('ignored.');\n"
            "'''\n"
            "All done!"
        )
        # Test streaming across small 5-char chunks (splits fences across boundaries)
        chunker = SentenceChunker(min_chars=8)
        emitted: list[str] = []
        for i in range(0, len(text), 5):
            emitted.extend(chunker.feed(text[i : i + 5]))
        tail = chunker.flush()
        if tail:
            emitted.append(tail)

        joined = " ".join(emitted)
        self.assertIn("Here is the snippet:", joined)
        self.assertIn("Use 'greet(\"Bytey\")' or `greet('world')` to run it.", joined)
        self.assertIn("Another example:", joined)
        self.assertIn("All done!", joined)
        self.assertNotIn("def greet", joined)
        self.assertNotIn("console.log", joined)

        # Unclosed trailing code block should also be ignored on flush
        chunker.reset()
        out2 = chunker.feed("Intro sentence here.\n```bash\necho hi\n")
        tail2 = chunker.flush()
        if tail2:
            out2.append(tail2)
        self.assertEqual(out2, ["Intro sentence here."])

    def test_emotion_stream_parser_arbitrary_boundaries(self) -> None:
        observed_emotions: list[str] = []
        parser = EmotionStreamParser(on_emotion=observed_emotions.append)

        chunks = ["  [em", "otion:", "curi", "ous]  ", "How ", "does ", "this work?"]
        out = "".join(parser.feed(c) for c in chunks) + parser.flush()

        self.assertEqual(parser.emotion, "curious")
        self.assertEqual(observed_emotions, ["curious"])
        self.assertEqual(out, "How does this work?")
        self.assertIn("curious", VALID_EMOTIONS)

        # Character-by-character boundary split
        char_emotions: list[str] = []
        char_parser = EmotionStreamParser(on_emotion=char_emotions.append)
        raw = "[emotion:curious]\nReady now."
        char_out = "".join(char_parser.feed(ch) for ch in raw) + char_parser.flush()
        self.assertEqual(char_parser.emotion, "curious")
        self.assertEqual(char_emotions, ["curious"])
        self.assertEqual(char_out, "Ready now.")

        # Plain text without emotion prefix passes through untouched
        plain_parser = EmotionStreamParser()
        plain_out = "".join(plain_parser.feed(c) for c in ["Hello ", "world!"]) + plain_parser.flush()
        self.assertIsNone(plain_parser.emotion)
        self.assertEqual(plain_out, "Hello world!")

    def test_parse_reply_tags_and_emotion_prefix_with_title(self) -> None:
        for raw in (
            "[emotion:happy][title: Async Tips] Use gather.",
            "[title: Async Tips][emotion:happy] Use gather.",
            "  [emotion:happy]  [title:   Async   Tips  ]  Use gather.",
            "  [title: Async Tips]\n[emotion:happy] Use gather.",
        ):
            with self.subTest(raw=raw):
                emotion, title, clean = parse_reply_tags(raw)
                self.assertEqual(emotion, "happy")
                self.assertEqual(title, "Async Tips")
                self.assertEqual(clean, "Use gather.")

                p_emotion, p_clean = parse_emotion_prefix(raw)
                self.assertEqual(p_emotion, "happy")
                self.assertEqual(p_clean, "Use gather.")

        em_only, ti_only, cl_only = parse_reply_tags("[title: Async Tips] Plain reply.")
        self.assertIsNone(em_only)
        self.assertEqual(ti_only, "Async Tips")
        self.assertEqual(cl_only, "Plain reply.")

    def test_emotion_stream_parser_with_title_both_orders_and_chunked(self) -> None:
        cases = (
            ("emotion_then_title", ["[em", "otion:", "hap", "py][ti", "tle: As", "ync Tips]  ", "Use ", "gather."]),
            ("title_then_emotion", ["[ti", "tle: As", "ync Tips]", "[em", "otion:", "hap", "py] ", "Use ", "gather."]),
        )
        for label, chunks in cases:
            with self.subTest(case=label, mode="chunked"):
                emotions: list[str] = []
                titles: list[str] = []
                parser = EmotionStreamParser(on_emotion=emotions.append, on_title=titles.append)
                out = "".join(parser.feed(c) for c in chunks) + parser.flush()
                self.assertEqual(parser.emotion, "happy")
                self.assertEqual(parser.captured_title, "Async Tips")
                self.assertEqual(emotions, ["happy"])
                self.assertEqual(titles, ["Async Tips"])
                self.assertEqual(out, "Use gather.")

            with self.subTest(case=label, mode="char_by_char"):
                raw = "".join(chunks)
                emotions = []
                titles = []
                parser = EmotionStreamParser(on_emotion=emotions.append, on_title=titles.append)
                out = "".join(parser.feed(ch) for ch in raw) + parser.flush()
                self.assertEqual(parser.emotion, "happy")
                self.assertEqual(parser.captured_title, "Async Tips")
                self.assertEqual(emotions, ["happy"])
                self.assertEqual(titles, ["Async Tips"])
                self.assertEqual(out, "Use gather.")

    def test_emotion_stream_parser_mid_stream_aliases_and_brackets(self) -> None:
        # Mid-stream emotion tag after prose (chunked & char-by-char)
        for mode in ("chunked", "char"):
            with self.subTest(mode=mode):
                seen: list[str] = []
                parser = EmotionStreamParser(on_emotion=seen.append)
                chunks = (
                    ["Let me check that. ", "[em", "otion:", "foc", "us] ", "Here are the specs."]
                    if mode == "chunked"
                    else list("Let me check that. [emotion:focus] Here are the specs.")
                )
                out = "".join(parser.feed(c) for c in chunks) + parser.flush()
                self.assertEqual(parser.emotion, "focus")
                self.assertEqual(seen, ["focus"])
                self.assertEqual(out, "Let me check that. Here are the specs.")

        # Alias tag [emotion:sorry] maps to neutral and is stripped
        alias_seen: list[str] = []
        alias_parser = EmotionStreamParser(on_emotion=alias_seen.append)
        alias_out = (
            "".join(alias_parser.feed(c) for c in ["[emotion:sorry]\n", "I am so sorry."])
            + alias_parser.flush()
        )
        self.assertEqual(alias_parser.emotion, "neutral")
        self.assertEqual(alias_seen, ["neutral"])
        self.assertEqual(alias_out, "I am so sorry.")

        # Unknown tag is still stripped without leaking raw [emotion:...]
        unk_seen: list[str] = []
        unk_parser = EmotionStreamParser(on_emotion=unk_seen.append)
        unk_out = "".join(unk_parser.feed(c) for c in ["Hi [emotion:xyzzy] there!"]) + unk_parser.flush()
        self.assertIsNone(unk_parser.emotion)
        self.assertEqual(unk_seen, [])
        self.assertEqual(unk_out, "Hi there!")

        # Non-tag brackets remain untouched
        bracket_parser = EmotionStreamParser()
        bracket_out = (
            "".join(bracket_parser.feed(c) for c in ["  [not a tag] and [link](url)."])
            + bracket_parser.flush()
        )
        self.assertIsNone(bracket_parser.emotion)
        self.assertEqual(bracket_out, "  [not a tag] and [link](url).")

        # parse_reply_tags handles inline tags and aliases
        em, ti, clean = parse_reply_tags("Checked! [emotion:sorry] My bad. [title: Laptop Specs]")
        self.assertEqual(em, "neutral")
        self.assertEqual(ti, "Laptop Specs")
        self.assertEqual(clean, "Checked! My bad.")


class TestSchemaSyncAndMCP(unittest.IsolatedAsyncioTestCase):
    """Test schema_sync round-trips and MCPClientManager against a real stdio server."""

    async def asyncSetUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.server_script = Path(self.tmp_dir.name) / "mock_mcp.py"
        self.server_script.write_text(MOCK_MCP_SERVER_SCRIPT, encoding="utf-8")
        self.secret_store = SecretStore(config_dir=Path(self.tmp_dir.name) / "cfg")
        self._fake_secrets = {"MCP_SECRET_TOKEN": "tok-abc-123"}
        self._secret_patch = patch.object(
            self.secret_store,
            "get_secret",
            side_effect=lambda k: self._fake_secrets.get(k),
        )
        self._secret_patch.start()
        self.manager = MCPClientManager(secret_resolver=self.secret_store)

    async def asyncTearDown(self) -> None:
        self._secret_patch.stop()
        await self.manager.close_all()
        self.tmp_dir.cleanup()

    def test_schema_sync_bidirectional_roundtrip(self) -> None:
        internal = {
            "local_echo": {
                "transport": "stdio",
                "command": sys.executable,
                "args": [str(self.server_script)],
                "env": {"MOCK_MCP_TOKEN": "${MCP_SECRET_TOKEN}"},
                "enabled": True,
                "require_confirmation": True,
                "disabled_tools": ["secret_ping"],
            },
            "remote_api": {
                "transport": "http",
                "url": "https://example.com/mcp",
                "headers": {"Authorization": "Bearer ${MCP_SECRET_TOKEN}"},
                "enabled": False,
                "require_confirmation": False,
                "disabled_tools": [],
            },
        }
        raw_json = to_mcp_servers_json(internal)
        parsed = from_mcp_servers_json(raw_json)

        self.assertEqual(parsed["local_echo"]["transport"], "stdio")
        self.assertEqual(parsed["local_echo"]["command"], sys.executable)
        self.assertEqual(parsed["local_echo"]["args"], [str(self.server_script)])
        self.assertEqual(parsed["local_echo"]["env"], {"MOCK_MCP_TOKEN": "${MCP_SECRET_TOKEN}"})
        self.assertTrue(parsed["local_echo"]["require_confirmation"])
        self.assertEqual(parsed["local_echo"]["disabled_tools"], ["secret_ping"])

        self.assertEqual(parsed["remote_api"]["transport"], "http")
        self.assertEqual(parsed["remote_api"]["url"], "https://example.com/mcp")
        self.assertFalse(parsed["remote_api"]["enabled"])

    def test_from_mcp_servers_json_rejects_duplicate_urls(self) -> None:
        dup_payload = json.dumps({
            "mcpServers": {
                "Fetch": {"url": "http://100.67.225.57:8081/sse", "transport": "sse"},
                "Web Search": {"url": "HTTP://100.67.225.57:8081/sse/", "transport": "sse"},
            }
        })
        with self.assertRaisesRegex(ValueError, "duplicates existing server"):
            from_mcp_servers_json(dup_payload)

        # Updating/editing the same server with its own URL succeeds
        unique_payload = json.dumps({
            "mcpServers": {
                "Fetch": {"url": "http://100.67.225.57:8081/sse", "transport": "sse"},
                "Web Search": {"url": "http://100.67.225.57:8082/sse", "transport": "sse"},
            }
        })
        parsed = from_mcp_servers_json(unique_payload)
        self.assertEqual(len(parsed), 2)

        # ConfigManager.update_config also rejects duplicate URLs
        with tempfile.TemporaryDirectory() as tmpdir:
            cm = ConfigManager(config_dir=Path(tmpdir))
            with self.assertRaisesRegex(ValueError, "duplicates existing server"):
                cm.update_config({
                    "mcp_servers": {
                        "Fetch": {"transport": "sse", "url": "http://100.67.225.57:8081/sse"},
                        "Web Search": {"transport": "sse", "url": "http://100.67.225.57:8081/sse"},
                    }
                })

    async def test_stdio_mcp_server_tools_and_confirmation_gate(self) -> None:
        servers_cfg = {
            "mock": {
                "enabled": True,
                "transport": "stdio",
                "command": sys.executable,
                "args": [str(self.server_script)],
                "env": {"MOCK_MCP_TOKEN": "${MCP_SECRET_TOKEN}"},
                "require_confirmation": False,
                "disabled_tools": ["secret_ping"],
            }
        }

        probe = await self.manager.test_server("mock", servers_cfg["mock"])
        self.assertTrue(probe["ok"], probe.get("error"))
        self.assertEqual([t["name"] for t in probe["tools"]], ["echo", "secret_ping"])

        tools = await self.manager.list_active_tools(servers_cfg)
        self.assertEqual(len(tools), 1)
        self.assertEqual(tools[0]["function"]["name"], "mock__echo")

        exec_res = await self.manager.execute_tool_call(
            "mock__echo",
            {"text": "hello", "include_env": True},
            servers_cfg,
        )
        self.assertTrue(exec_res["ok"])
        self.assertEqual(exec_res["content"], "echo:hello|env=tok-abc-123")

        # Confirmation gate: denied then approved
        servers_cfg["mock"]["require_confirmation"] = True

        async def deny_cb(_srv: str, _tool: str, _args: dict[str, Any]) -> bool:
            return False

        denied_res = await self.manager.execute_tool_call(
            "mock__echo",
            {"text": "blocked"},
            servers_cfg,
            confirm_callback=deny_cb,
        )
        self.assertFalse(denied_res["ok"])
        self.assertTrue(denied_res["denied"])

        async def approve_cb(_srv: str, _tool: str, _args: dict[str, Any]) -> bool:
            return True

        approved_res = await self.manager.execute_tool_call(
            "mock__echo",
            {"text": "allowed"},
            servers_cfg,
            confirm_callback=approve_cb,
        )
        self.assertTrue(approved_res["ok"])
        self.assertFalse(approved_res["denied"])
        self.assertEqual(approved_res["content"], "echo:allowed")


class TestGeminiToolThoughtSignature(unittest.IsolatedAsyncioTestCase):
    """Test Gemini multi-round tool calls preserve thought signatures and filter thoughts."""

    async def test_gemini_multi_round_tool_call_preserves_thought_signature(self) -> None:
        recorded_calls: list[tuple[str, list[Any], Any]] = []
        round_chunks: list[list[Any]] = [
            [
                SimpleNamespace(
                    candidates=[
                        SimpleNamespace(
                            content=SimpleNamespace(
                                parts=[
                                    genai_types.Part(thought=True, text="checking weather"),
                                    genai_types.Part(
                                        function_call=genai_types.FunctionCall(
                                            id="fc_weather_1",
                                            name="Weather__get_current_weather",
                                            args={"city": "Ovar"},
                                        ),
                                        thought_signature=b"sig-weather-xyz",
                                    ),
                                ]
                            )
                        )
                    ]
                )
            ],
            [
                SimpleNamespace(
                    candidates=[
                        SimpleNamespace(
                            content=SimpleNamespace(
                                parts=[
                                    genai_types.Part(
                                        text="[emotion:happy] It is 18C and sunny in Ovar!"
                                    )
                                ]
                            )
                        )
                    ]
                )
            ],
        ]

        class _FakeModels:
            async def generate_content_stream(
                self, *, model: str, contents: list[Any], config: Any
            ) -> Any:
                idx = len(recorded_calls)
                recorded_calls.append((model, list(contents), config))
                chunks = round_chunks[idx]

                async def _iter_chunks() -> Any:
                    for chunk in chunks:
                        yield chunk

                return _iter_chunks()

        class _FakeAio:
            def __init__(self) -> None:
                self.models = _FakeModels()

            async def aclose(self) -> None:
                return None

        class _FakeClient:
            def __init__(self, api_key: str | None = None) -> None:
                self.api_key = api_key
                self.aio = _FakeAio()

        fake_genai = SimpleNamespace(Client=_FakeClient)
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "Weather__get_current_weather",
                    "description": "Get current weather for a city.",
                    "parameters": {
                        "type": "object",
                        "properties": {"city": {"type": "string"}},
                        "required": ["city"],
                    },
                },
            }
        ]

        router = LLMRouter()
        r1_streamed: list[str] = []
        r2_streamed: list[str] = []
        try:
            with patch("bytey_ai_companion.llm.router._google_genai", fake_genai):
                res1 = await router.generate_chat(
                    messages=[{"role": "user", "content": "How is the weather in Ovar?"}],
                    system_prompt="You are Bytey.",
                    provider="gemini",
                    model="gemini-2.5-flash",
                    api_key="test-key",
                    tools=tools,
                    on_text_chunk=r1_streamed.append,
                )

                self.assertEqual(len(res1["tool_calls"]), 1)
                tc1 = res1["tool_calls"][0]
                self.assertEqual(tc1["name"], "Weather__get_current_weather")
                self.assertEqual(tc1["thought_signature"], b"sig-weather-xyz")
                self.assertTrue(tc1["has_provider_id"])
                self.assertEqual(r1_streamed, [])

                messages = [
                    {"role": "user", "content": "How is the weather in Ovar?"},
                    res1["raw_assistant_message"],
                    {
                        "role": "tool",
                        "tool_call_id": "fc_weather_1",
                        "name": "Weather__get_current_weather",
                        "content": "18C sunny",
                    },
                ]
                res2 = await router.generate_chat(
                    messages=messages,
                    system_prompt="You are Bytey.",
                    provider="gemini",
                    model="gemini-2.5-flash",
                    api_key="test-key",
                    tools=tools,
                    on_text_chunk=r2_streamed.append,
                )
        finally:
            await router.close()

        self.assertEqual(len(recorded_calls), 2)
        _, round2_contents, _ = recorded_calls[1]

        model_contents = [c for c in round2_contents if getattr(c, "role", None) == "model"]
        self.assertEqual(len(model_contents), 1)
        model_parts = list(model_contents[0].parts or [])
        thought_parts = [p for p in model_parts if getattr(p, "thought", False)]
        fc_parts = [p for p in model_parts if getattr(p, "function_call", None) is not None]
        self.assertEqual(len(thought_parts), 1)
        self.assertEqual(thought_parts[0].text, "checking weather")
        self.assertEqual(len(fc_parts), 1)
        self.assertEqual(fc_parts[0].thought_signature, b"sig-weather-xyz")
        self.assertEqual(fc_parts[0].function_call.name, "Weather__get_current_weather")
        self.assertEqual(fc_parts[0].function_call.id, "fc_weather_1")

        user_contents = [c for c in round2_contents if getattr(c, "role", None) == "user"]
        fn_resp_parts = [
            p
            for c in user_contents
            for p in (c.parts or [])
            if getattr(p, "function_response", None) is not None
        ]
        self.assertEqual(len(fn_resp_parts), 1)
        self.assertEqual(fn_resp_parts[0].function_response.name, "Weather__get_current_weather")
        self.assertEqual(fn_resp_parts[0].function_response.id, "fc_weather_1")

        self.assertEqual(res2["content"], "It is 18C and sunny in Ovar!")
        self.assertEqual(res2["emotion"], "happy")
        self.assertEqual("".join(r2_streamed), "It is 18C and sunny in Ovar!")
        self.assertNotIn("checking weather", "".join(r2_streamed))
        self.assertNotIn("[emotion:", "".join(r2_streamed))


class TestLLMRouterContextSize(unittest.IsolatedAsyncioTestCase):
    """Test Ollama context_length extraction, capability detection, and options.num_ctx."""

    async def test_ollama_discovery_capabilities_and_num_ctx(self) -> None:
        chat_payloads: list[dict[str, Any]] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if request.method == "GET" and path == "/api/tags":
                return httpx.Response(
                    200,
                    json={
                        "models": [
                            {"name": "gemma4:latest"},
                            {"name": "llama3.2:latest"},
                        ]
                    },
                )
            if request.method == "POST" and path == "/api/show":
                body = json.loads(request.content.decode("utf-8"))
                model_name = body.get("model")
                if model_name == "gemma4:latest":
                    return httpx.Response(
                        200,
                        json={
                            "capabilities": ["completion", "tools"],
                            "model_info": {
                                "general.architecture": "gemma4",
                                "gemma4.context_length": 262144,
                            },
                        },
                    )
                if model_name == "llama3.2:latest":
                    return httpx.Response(
                        200,
                        json={
                            "capabilities": ["completion"],
                            "model_info": {
                                "general.architecture": "llama",
                                "llama.context_length": 8192,
                            },
                        },
                    )
                return httpx.Response(404, json={"error": "not found"})
            if request.method == "POST" and path == "/api/chat":
                chat_payloads.append(json.loads(request.content.decode("utf-8")))
                ndjson = (
                    '{"message":{"content":"[emotion:happy] Ready!"},"done":false}\n'
                    '{"done":true,"prompt_eval_count":42,"eval_count":5,"done_reason":"stop"}\n'
                ).encode("utf-8")
                return httpx.Response(200, content=ndjson)
            return httpx.Response(404, json={"error": f"Unhandled route {request.method} {path}"})

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            router = LLMRouter(http_client=client)

            discovered = await router._discover_ollama_models("http://localhost:11434")
            self.assertIsNone(discovered["error"])
            by_id = {m["id"]: m for m in discovered["models"]}
            self.assertEqual(by_id["gemma4:latest"]["context_length"], 262144)
            self.assertTrue(by_id["gemma4:latest"]["supports_tools"])
            self.assertEqual(by_id["llama3.2:latest"]["context_length"], 8192)
            self.assertFalse(by_id["llama3.2:latest"]["supports_tools"])

            self.assertEqual(
                await router.detect_model_capabilities(
                    "ollama", "gemma4:latest", base_url="http://localhost:11434"
                ),
                (True, 262144),
            )
            self.assertEqual(
                await router.detect_model_capabilities(
                    "ollama", "llama3.2:latest", base_url="http://localhost:11434"
                ),
                (False, 8192),
            )

            res_with_ctx = await router.generate_chat(
                messages=[{"role": "user", "content": "Hello"}],
                system_prompt="You are Bytey.",
                provider="ollama",
                model="gemma4:latest",
                base_url="http://localhost:11434",
                context_size=16384,
            )
            self.assertEqual(res_with_ctx["content"], "Ready!")
            self.assertEqual(res_with_ctx["emotion"], "happy")

            res_auto_ctx = await router.generate_chat(
                messages=[{"role": "user", "content": "Hello again"}],
                system_prompt="You are Bytey.",
                provider="ollama",
                model="gemma4:latest",
                base_url="http://localhost:11434",
                context_size=None,
            )
            self.assertEqual(res_auto_ctx["content"], "Ready!")

        self.assertEqual(len(chat_payloads), 2)
        self.assertEqual(chat_payloads[0]["options"].get("num_ctx"), 16384)
        self.assertNotIn("num_ctx", chat_payloads[1]["options"])


class TestLiveDaemonIntegration(unittest.IsolatedAsyncioTestCase):
    """End-to-end tests against a live ByteyDaemon on ephemeral localhost port."""

    async def asyncSetUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp_dir.name)
        self.cfg_dir = self.root / "config"
        self.data_dir = self.root / "data"
        self.server_script = self.root / "mock_mcp.py"
        self.server_script.write_text(MOCK_MCP_SERVER_SCRIPT, encoding="utf-8")

        self.mock_llm = _MockLLMRouter()
        self.daemon = ByteyDaemon(
            host="127.0.0.1",
            port=0,
            config_dir=self.cfg_dir,
            data_dir=self.data_dir,
            enable_voice_capture=False,
            llm_router=self.mock_llm,  # type: ignore[arg-type]
        )
        # Mute TTS in config so tests never touch local PipeWire or Kokoro HTTP endpoints
        self.daemon.config_manager.update_config(
            {"voice_out": {"tts_muted": True, "tts_provider": "disabled"}}
        )
        await self.daemon.start()
        self.base_url = f"http://127.0.0.1:{self.daemon.ipc.port}"
        self.ws_url = f"ws://127.0.0.1:{self.daemon.ipc.port}/ws"

    async def asyncTearDown(self) -> None:
        await self.daemon.stop()
        self.tmp_dir.cleanup()

    async def test_full_rest_and_websocket_lifecycle(self) -> None:
        async with httpx.AsyncClient(base_url=self.base_url, timeout=10.0) as client:
            # 1. Health
            r = await client.get("/api/v1/health")
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.json()["status"], "ok")

            # 2. Config GET + PATCH (settings-ui schema + gnome-extension schema)
            r = await client.get("/api/v1/config")
            self.assertEqual(r.status_code, 200)

            r = await client.patch(
                "/api/v1/config",
                json={
                    "llm": {
                        "provider": "gemini",
                        "model": "gemini-2.5-flash",
                        "force_enable_tools": True,
                    }
                },
            )
            self.assertEqual(r.status_code, 200)
            cfg_after_ui = r.json()
            self.assertEqual(cfg_after_ui["llm"]["provider"], "gemini")
            self.assertEqual(cfg_after_ui["llm"]["model"], "gemini-2.5-flash")
            self.assertTrue(cfg_after_ui["llm"]["force_enable_tools"])
            self.assertTrue(cfg_after_ui["llm"]["supports_tools"])
            self.assertIn("context_size", cfg_after_ui["llm"])
            self.assertIn("max_context_size", cfg_after_ui["llm"])

            r = await client.patch(
                "/api/v1/config",
                json={
                    "visuals": {
                        "position_mode": "floating",
                        "float_x": 320,
                        "float_y": 140,
                    }
                },
            )
            self.assertEqual(r.status_code, 200)
            cfg_after_ext = r.json()
            self.assertEqual(cfg_after_ext["visuals"]["position_mode"], "floating")
            self.assertEqual(cfg_after_ext["visuals"]["float_x"], 320)
            self.assertEqual(cfg_after_ext["visuals"]["float_y"], 140)

            r = await client.patch(
                "/api/v1/config",
                json={
                    "visuals": {
                        "eye_color": "#00ff88",
                        "background_color": "#11131a",
                        "border_color": "#ffaa0080",
                    }
                },
            )
            self.assertEqual(r.status_code, 200)
            cfg_colors = r.json()
            self.assertEqual(cfg_colors["visuals"]["eye_color"], "#00FF88")
            self.assertEqual(cfg_colors["visuals"]["background_color"], "#11131A")
            self.assertEqual(cfg_colors["visuals"]["border_color"], "#FFAA0080")

            # 3. Secrets PUT + verify secret absent from GET /api/v1/config
            gem_secret = "AIzaSyTestSecretValue12345"
            fake_keyring: dict[str, str] = {}
            with patch.object(
                self.daemon.secret_store, "_keyring_enabled", return_value=True
            ), patch.object(
                self.daemon.secret_store,
                "_keyring_set",
                side_effect=lambda k, v: fake_keyring.__setitem__(k, v),
            ), patch.object(
                self.daemon.secret_store,
                "_keyring_get",
                side_effect=lambda k: fake_keyring.get(k),
            ), patch.object(
                self.daemon.secret_store,
                "_keyring_delete",
                side_effect=lambda k: fake_keyring.pop(k, None),
            ):
                r = await client.put("/api/v1/secrets", json={"secrets": {"GEMINI_API_KEY": gem_secret}})
                self.assertEqual(r.status_code, 200)
                self.assertTrue(r.json()["ok"])
                self.assertTrue(r.json()["secrets_configured"]["GEMINI_API_KEY"])

                r = await client.get("/api/v1/config")
                self.assertEqual(r.status_code, 200)
                self.assertTrue(r.json()["secrets_configured"]["GEMINI_API_KEY"])
                self.assertNotIn(gem_secret, r.text)

            # 4. MCP JSON GET & PUT
            r = await client.get("/api/v1/mcp/json")
            self.assertEqual(r.status_code, 200)
            self.assertIn("mcpServers", r.json())

            mcp_payload = {
                "mcpServers": {
                    "mock": {
                        "command": sys.executable,
                        "args": [str(self.server_script)],
                        "require_confirmation": True,
                    }
                }
            }
            r = await client.put(
                "/api/v1/mcp/json",
                json={"raw_json": json.dumps(mcp_payload)},
            )
            self.assertEqual(r.status_code, 200)
            self.assertIn("mock", r.json()["mcpServers"])
            self.assertTrue(r.json()["mcpServers"]["mock"]["require_confirmation"])

            # 5. WebSocket + Chat + Tool confirmation round-trip + Shutdown
            events: list[dict[str, Any]] = []
            confirm_calls: list[str] = []

            async with websockets.connect(self.ws_url) as ws:
                async def _ws_reader() -> None:
                    try:
                        async for raw in ws:
                            ev = json.loads(raw)
                            events.append(ev)
                            if (
                                ev.get("type") == "tool_call_status"
                                and ev.get("status") == "awaiting_confirm"
                            ):
                                call_id = ev["call_id"]
                                confirm_calls.append(call_id)
                                resp = await client.post(
                                    "/api/v1/mcp/confirm",
                                    json={"call_id": call_id, "approved": True},
                                )
                                self.assertEqual(resp.status_code, 200)
                    except websockets.ConnectionClosed:
                        pass

                reader_task = asyncio.create_task(_ws_reader())
                try:
                    # 5a. Standard chat turn via POST /api/v1/chat
                    r_chat = await client.post(
                        "/api/v1/chat",
                        json={"content": "Hello Bytey", "input_mode": "text"},
                    )
                    self.assertEqual(r_chat.status_code, 200)
                    self.assertEqual(
                        r_chat.json()["assistant_message"]["content"],
                        "Hello from mocked assistant!",
                    )

                    # 5b. Tool-calling turn via POST /api/v1/sessions/active/messages
                    r_tool = await client.post(
                        "/api/v1/sessions/active/messages",
                        json={"content": "Please use_echo_tool now", "input_mode": "text"},
                    )
                    self.assertEqual(r_tool.status_code, 200)
                    self.assertIn(
                        "echo:daemon-ok",
                        r_tool.json()["assistant_message"]["content"],
                    )

                    # Wait briefly for any trailing WS events
                    await asyncio.sleep(0.05)

                    # Verify WS events
                    ev_types = {e.get("type") for e in events}
                    self.assertIn("state_changed", ev_types)
                    self.assertIn("transcript_delta", ev_types)
                    self.assertIn("tool_call_status", ev_types)

                    tool_statuses = [
                        e.get("status")
                        for e in events
                        if e.get("type") == "tool_call_status"
                    ]
                    self.assertEqual(len(confirm_calls), 1)
                    self.assertIn("awaiting_confirm", tool_statuses)
                    self.assertIn("running", tool_statuses)
                    self.assertIn("ok", tool_statuses)
                finally:
                    reader_task.cancel()
                    await asyncio.gather(reader_task, return_exceptions=True)

    async def test_system_exit_broadcasts_shutdown_and_stops_cleanly(self) -> None:
        """Verify POST /api/v1/system/exit broadcasts shutdown and stops cleanly without loop errors."""
        driver = """
import asyncio, json, os, sys, tempfile
from pathlib import Path
import httpx, websockets

os.environ["BYTEY_DISABLE_KEYRING"] = "1"
sys.path.insert(0, %r)
from bytey_ai_companion.main import ByteyDaemon

async def main():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        daemon = ByteyDaemon(
            host="127.0.0.1",
            port=0,
            config_dir=root / "cfg",
            data_dir=root / "data",
            enable_voice_capture=False,
        )
        daemon.config_manager.update_config({"voice_out": {"tts_muted": True, "tts_provider": "disabled"}})
        await daemon.start()
        base = f"http://127.0.0.1:{daemon.ipc.port}"
        ws_url = f"ws://127.0.0.1:{daemon.ipc.port}/ws"
        got_shutdown = False
        async with websockets.connect(ws_url) as ws:
            async with httpx.AsyncClient(base_url=base, timeout=5.0) as client:
                r = await client.post("/api/v1/system/exit")
                assert r.status_code == 200 and r.json().get("status") == "shutting_down", r.text
            try:
                async for raw in ws:
                    if json.loads(raw).get("type") == "shutdown":
                        got_shutdown = True
                        break
            except websockets.ConnectionClosed:
                pass
        await asyncio.wait_for(daemon.wait_until_stopped(), timeout=5.0)
        await asyncio.sleep(0.15)
        assert got_shutdown, "Did not receive shutdown WS event"
        assert daemon.ipc._tcp_server is None, "IPCServer was not stopped cleanly by /api/v1/system/exit"

asyncio.run(main())
""" % str(CORE_DAEMON_DIR)

        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            driver,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "BYTEY_DISABLE_KEYRING": "1"},
        )
        try:
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=15.0)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            self.fail("Daemon subprocess hung during POST /api/v1/system/exit")

        stderr = stderr_b.decode("utf-8", errors="replace")
        stdout = stdout_b.decode("utf-8", errors="replace")
        self.assertEqual(
            proc.returncode,
            0,
            f"POST /api/v1/system/exit failed (rc={proc.returncode}):\nSTDOUT:\n{stdout}\nSTDERR:\n{stderr}",
        )
        self.assertNotIn("RecursionError", stderr)

    async def test_context_size_config_patch_and_clamping(self) -> None:
        """Verify ByteyDaemon exposes max_context_size, resets context_size, and clamps on prompt."""
        cap_router = _CapabilitiesMockLLMRouter(supports_tools=True, max_context_size=8192)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            daemon = ByteyDaemon(
                host="127.0.0.1",
                port=0,
                config_dir=root / "cfg",
                data_dir=root / "data",
                enable_voice_capture=False,
                llm_router=cap_router,  # type: ignore[arg-type]
            )
            daemon.config_manager.update_config(
                {"voice_out": {"tts_muted": True, "tts_provider": "disabled"}}
            )
            await daemon.start()
            try:
                base_url = f"http://127.0.0.1:{daemon.ipc.port}"
                async with httpx.AsyncClient(base_url=base_url, timeout=10.0) as client:
                    cfg0 = (await client.get("/api/v1/config")).json()
                    self.assertIsNone(cfg0["llm"]["context_size"])
                    self.assertEqual(cfg0["llm"]["max_context_size"], 8192)

                    r1 = await client.patch("/api/v1/config", json={"llm": {"context_size": 16384}})
                    self.assertEqual(r1.status_code, 200)
                    self.assertEqual(r1.json()["llm"]["context_size"], 16384)
                    self.assertEqual(r1.json()["llm"]["max_context_size"], 8192)

                    r2 = await client.patch("/api/v1/config", json={"llm": {"context_size": None}})
                    self.assertEqual(r2.status_code, 200)
                    self.assertIsNone(r2.json()["llm"]["context_size"])
                    self.assertEqual(r2.json()["llm"]["max_context_size"], 8192)

                    r3 = await client.patch("/api/v1/config", json={"llm": {"context_size": 65536}})
                    self.assertEqual(r3.status_code, 200)
                    self.assertEqual(r3.json()["llm"]["context_size"], 65536)

                await daemon.handle_user_prompt("Clamp context please", input_mode="text")
                self.assertEqual(cap_router.calls[-1]["context_size"], 8192)

                daemon.config_manager.update_config({"provider": {"context_size": 4096}})
                await daemon.handle_user_prompt("Keep smaller context", input_mode="text")
                self.assertEqual(cap_router.calls[-1]["context_size"], 4096)

                daemon.config_manager.update_config({"provider": {"context_size": None}})
                await daemon.handle_user_prompt("Auto context", input_mode="text")
                self.assertIsNone(cap_router.calls[-1]["context_size"])
            finally:
                await daemon.stop()

    async def test_tool_call_status_broadcast_untruncated_summary(self) -> None:
        """Verify tool_call_status WS broadcast emits full >400 char MCP summary without truncation."""
        long_text = "A" * 1000
        expected_summary = f"echo:{long_text}"
        self.mock_llm.tool_arguments = {"text": long_text}

        mcp_payload = {
            "mcpServers": {
                "mock": {
                    "command": sys.executable,
                    "args": [str(self.server_script)],
                    "require_confirmation": False,
                }
            }
        }
        events: list[dict[str, Any]] = []

        async with httpx.AsyncClient(base_url=self.base_url, timeout=10.0) as client:
            r_cfg = await client.patch(
                "/api/v1/config",
                json={
                    "llm": {
                        "provider": "gemini",
                        "model": "gemini-2.5-flash",
                        "force_enable_tools": True,
                    }
                },
            )
            self.assertEqual(r_cfg.status_code, 200)

            r_mcp = await client.put(
                "/api/v1/mcp/json",
                json={"raw_json": json.dumps(mcp_payload)},
            )
            self.assertEqual(r_mcp.status_code, 200)

            async with websockets.connect(self.ws_url) as ws:
                async def _ws_reader() -> None:
                    try:
                        async for raw in ws:
                            events.append(json.loads(raw))
                    except websockets.ConnectionClosed:
                        pass

                reader_task = asyncio.create_task(_ws_reader())
                try:
                    r_chat = await client.post(
                        "/api/v1/chat",
                        json={"content": "Please use_echo_tool now", "input_mode": "text"},
                    )
                    self.assertEqual(r_chat.status_code, 200)
                    await asyncio.sleep(0.05)
                finally:
                    reader_task.cancel()
                    await asyncio.gather(reader_task, return_exceptions=True)

        ok_events = [
            e
            for e in events
            if e.get("type") == "tool_call_status" and e.get("status") == "ok"
        ]
        self.assertEqual(len(ok_events), 1)
        self.assertEqual(ok_events[0]["summary"], expected_summary)
        self.assertGreater(len(ok_events[0]["summary"]), 400)

    async def test_multichat_activate_rename_autotitle_and_delete(self) -> None:
        """Verify multichat activate, PATCH rename, auto-titling, voice turn, and active delete."""
        events: list[dict[str, Any]] = []

        async with httpx.AsyncClient(base_url=self.base_url, timeout=10.0) as client:
            async with websockets.connect(self.ws_url) as ws:
                async def _ws_reader() -> None:
                    try:
                        async for raw in ws:
                            events.append(json.loads(raw))
                    except websockets.ConnectionClosed:
                        pass

                reader_task = asyncio.create_task(_ws_reader())
                try:
                    # 1. Create first session ("New Chat") and send prompt -> auto-titles
                    r_s1 = await client.post("/api/v1/sessions", json={})
                    self.assertEqual(r_s1.status_code, 200)
                    s1_id = r_s1.json()["id"]

                    r_msg1 = await client.post(
                        f"/api/v1/sessions/{s1_id}/messages",
                        json={"content": "Explain quantum tunneling simply", "input_mode": "text"},
                    )
                    self.assertEqual(r_msg1.status_code, 200)

                    # 2. Create second session with custom title -> prompt must NOT overwrite custom title
                    r_s2 = await client.post("/api/v1/sessions", json={"title": "Custom Topic"})
                    self.assertEqual(r_s2.status_code, 200)
                    s2_id = r_s2.json()["id"]

                    r_msg2 = await client.post(
                        "/api/v1/sessions/active/messages",
                        json={"content": "First prompt in custom chat", "input_mode": "text"},
                    )
                    self.assertEqual(r_msg2.status_code, 200)

                    r_list = await client.get("/api/v1/sessions")
                    self.assertEqual(r_list.status_code, 200)
                    sessions_by_id = {s["id"]: s for s in r_list.json()["sessions"]}
                    self.assertEqual(sessions_by_id[s1_id]["title"], "Explain quantum tunneling simply")
                    self.assertEqual(sessions_by_id[s2_id]["title"], "Custom Topic")

                    # 3. Activate unknown session -> 404
                    r_404 = await client.post("/api/v1/sessions/does-not-exist/activate")
                    self.assertEqual(r_404.status_code, 404)

                    # 4. Activate older session s1 -> updates active_session_id, returns messages, broadcasts session_activated
                    events.clear()
                    r_act = await client.post(f"/api/v1/sessions/{s1_id}/activate")
                    self.assertEqual(r_act.status_code, 200)
                    act_data = r_act.json()
                    self.assertEqual(act_data["active_session_id"], s1_id)
                    self.assertEqual(len(act_data["messages"]), 2)

                    r_active = await client.get("/api/v1/sessions/active")
                    self.assertEqual(r_active.status_code, 200)
                    self.assertEqual(r_active.json()["active_session_id"], s1_id)

                    # Subsequent text prompt on "active" and voice turn persist into resumed s1
                    r_msg1b = await client.post(
                        "/api/v1/sessions/active/messages",
                        json={"content": "Follow up in resumed session", "input_mode": "text"},
                    )
                    self.assertEqual(r_msg1b.status_code, 200)
                    self.assertEqual(r_msg1b.json()["session_id"], s1_id)

                    await self.daemon._on_voice_prompt("Voice follow up in resumed session")

                    r_s1_msgs = await client.get(f"/api/v1/sessions/{s1_id}/messages")
                    self.assertEqual(r_s1_msgs.status_code, 200)
                    s1_contents = [m["content"] for m in r_s1_msgs.json()["messages"]]
                    self.assertIn("Follow up in resumed session", s1_contents)
                    self.assertIn("Voice follow up in resumed session", s1_contents)

                    # s2 should still only have its original 2 messages
                    r_s2_msgs = await client.get(f"/api/v1/sessions/{s2_id}/messages")
                    self.assertEqual(len(r_s2_msgs.json()["messages"]), 2)

                    # 5. PATCH /api/v1/sessions/{id} validation & rename
                    r_bad1 = await client.patch(f"/api/v1/sessions/{s1_id}", json={})
                    self.assertEqual(r_bad1.status_code, 400)
                    r_bad2 = await client.patch(f"/api/v1/sessions/{s1_id}", json={"title": "   "})
                    self.assertEqual(r_bad2.status_code, 400)
                    r_bad3 = await client.patch(f"/api/v1/sessions/{s1_id}", json={"title": 123})
                    self.assertEqual(r_bad3.status_code, 400)
                    r_patch_404 = await client.patch(
                        "/api/v1/sessions/missing-id", json={"title": "Valid"}
                    )
                    self.assertEqual(r_patch_404.status_code, 404)

                    r_patch = await client.patch(
                        f"/api/v1/sessions/{s1_id}", json={"title": "  Renamed Quantum Chat  "}
                    )
                    self.assertEqual(r_patch.status_code, 200)
                    self.assertEqual(r_patch.json()["session"]["title"], "Renamed Quantum Chat")

                    # 6. Delete currently active session s1 -> falls back to valid active session s2
                    r_del = await client.delete(f"/api/v1/sessions/{s1_id}")
                    self.assertEqual(r_del.status_code, 200)
                    del_data = r_del.json()
                    self.assertTrue(del_data["ok"])
                    self.assertEqual(del_data["active_session_id"], s2_id)

                    await asyncio.sleep(0.05)
                finally:
                    reader_task.cancel()
                    await asyncio.gather(reader_task, return_exceptions=True)

        activated_ev = [e for e in events if e.get("type") == "session_activated"]
        self.assertEqual(len(activated_ev), 1)
        self.assertEqual(activated_ev[0]["session_id"], s1_id)
        self.assertEqual(len(activated_ev[0]["messages"]), 2)

        updated_ev = [e for e in events if e.get("type") == "sessions_updated"]
        self.assertGreaterEqual(len(updated_ev), 2)
        self.assertEqual(updated_ev[-1]["active_session_id"], s2_id)
        self.assertTrue(all(s["id"] != s1_id for s in updated_ev[-1]["sessions"]))

    async def test_auto_title_from_llm_and_prompt_fallback(self) -> None:
        events: list[dict[str, Any]] = []

        async with websockets.connect(self.ws_url) as ws:
            async def _reader() -> None:
                while True:
                    raw = await ws.recv()
                    events.append(json.loads(raw))

            reader_task = asyncio.create_task(_reader())
            async with httpx.AsyncClient(base_url=self.base_url, timeout=10.0) as client:
                try:
                    # 1. First turn on "New Chat" where LLM returns [emotion:happy][title: Python Async Tips]
                    r_create1 = await client.post("/api/v1/sessions", json={})
                    self.assertEqual(r_create1.status_code, 200)
                    sid1 = r_create1.json()["session"]["id"]
                    self.assertEqual(r_create1.json()["session"]["title"], "New Chat")

                    events.clear()
                    self.mock_llm.next_raw_reply = "[emotion:happy][title: Python Async Tips] Use asyncio.gather."
                    r_msg1 = await client.post(
                        f"/api/v1/sessions/{sid1}/messages",
                        json={"content": "How do I run tasks concurrently in Python?", "input_mode": "text"},
                    )
                    self.assertEqual(r_msg1.status_code, 200)

                    s1_db = await self.daemon.storage.get_session(sid1)
                    self.assertIsNotNone(s1_db)
                    self.assertEqual(s1_db["title"], "Python Async Tips")

                    msgs1 = await self.daemon.storage.list_messages(sid1)
                    assistant_msgs1 = [m for m in msgs1 if m["role"] == "assistant"]
                    self.assertEqual(len(assistant_msgs1), 1)
                    self.assertEqual(assistant_msgs1[0]["content"], "Use asyncio.gather.")
                    self.assertNotIn("[title:", assistant_msgs1[0]["content"])
                    self.assertNotIn("[emotion:", assistant_msgs1[0]["content"])
                    self.assertEqual(assistant_msgs1[0]["emotion"], "happy")

                    await asyncio.sleep(0.05)
                    updated1 = [e for e in events if e.get("type") == "sessions_updated"]
                    self.assertGreaterEqual(len(updated1), 1)
                    titles1 = {s["id"]: s["title"] for s in updated1[-1]["sessions"]}
                    self.assertEqual(titles1.get(sid1), "Python Async Tips")

                    # 2. First turn on "New Chat" where LLM omits [title:...] -> falls back to _auto_title_from_prompt
                    r_create2 = await client.post("/api/v1/sessions", json={})
                    self.assertEqual(r_create2.status_code, 200)
                    sid2 = r_create2.json()["session"]["id"]
                    self.assertEqual(r_create2.json()["session"]["title"], "New Chat")

                    events.clear()
                    prompt2 = "Explain Python asyncio event loop internals in detail please"
                    self.mock_llm.next_raw_reply = "[emotion:curious] Check the event loop."
                    r_msg2 = await client.post(
                        f"/api/v1/sessions/{sid2}/messages",
                        json={"content": prompt2, "input_mode": "text"},
                    )
                    self.assertEqual(r_msg2.status_code, 200)

                    expected_fallback = _auto_title_from_prompt(prompt2)
                    s2_db = await self.daemon.storage.get_session(sid2)
                    self.assertIsNotNone(s2_db)
                    self.assertEqual(s2_db["title"], expected_fallback)

                    msgs2 = await self.daemon.storage.list_messages(sid2)
                    assistant_msgs2 = [m for m in msgs2 if m["role"] == "assistant"]
                    self.assertEqual(len(assistant_msgs2), 1)
                    self.assertEqual(assistant_msgs2[0]["content"], "Check the event loop.")

                    await asyncio.sleep(0.05)
                    updated2 = [e for e in events if e.get("type") == "sessions_updated"]
                    self.assertGreaterEqual(len(updated2), 1)
                    titles2 = {s["id"]: s["title"] for s in updated2[-1]["sessions"]}
                    self.assertEqual(titles2.get(sid2), expected_fallback)
                finally:
                    reader_task.cancel()
                    await asyncio.gather(reader_task, return_exceptions=True)

    async def test_skills_crud_ws_event_slash_command_and_load_tool(self) -> None:
        """Verify /api/v1/skills CRUD, skills_updated WS events, /<skill> fast path, and bytey_skill__load."""
        user_skills_dir = self.root / "user_skills"
        events: list[dict[str, Any]] = []

        async with httpx.AsyncClient(base_url=self.base_url, timeout=10.0) as client:
            r_cfg = await client.patch(
                "/api/v1/config",
                json={
                    "llm": {
                        "provider": "gemini",
                        "model": "gemini-2.5-flash",
                        "force_enable_tools": True,
                    },
                    "skills": {
                        "enabled": True,
                        "skills_dir": str(user_skills_dir),
                    },
                },
            )
            self.assertEqual(r_cfg.status_code, 200)

            async with websockets.connect(self.ws_url) as ws:
                async def _ws_reader() -> None:
                    try:
                        async for raw in ws:
                            events.append(json.loads(raw))
                    except websockets.ConnectionClosed:
                        pass

                reader_task = asyncio.create_task(_ws_reader())
                try:
                    # 1. GET /api/v1/skills
                    r_list = await client.get("/api/v1/skills")
                    self.assertEqual(r_list.status_code, 200)
                    self.assertIn("skills", r_list.json())

                    # 2. POST /api/v1/skills
                    r_create = await client.post(
                        "/api/v1/skills",
                        json={
                            "name": "live-skill",
                            "description": "Live test skill",
                            "body": "Follow live instructions.",
                            "slash_command": "live",
                        },
                    )
                    self.assertEqual(r_create.status_code, 201)
                    self.assertEqual(r_create.json()["skill"]["name"], "live-skill")
                    self.assertEqual(r_create.json()["skill"]["source"], "user")

                    # 3. PUT /api/v1/skills/{name}
                    r_put = await client.put(
                        "/api/v1/skills/live-skill",
                        json={
                            "description": "Updated live skill",
                            "body": "Updated live instructions.",
                        },
                    )
                    self.assertEqual(r_put.status_code, 200)
                    self.assertEqual(r_put.json()["skill"]["body"], "Updated live instructions.")

                    # 4. PATCH /api/v1/skills/{name} (disable then re-enable)
                    r_patch_off = await client.patch(
                        "/api/v1/skills/live-skill",
                        json={"enabled": False},
                    )
                    self.assertEqual(r_patch_off.status_code, 200)
                    self.assertFalse(r_patch_off.json()["skill"]["enabled"])

                    r_patch_on = await client.patch(
                        "/api/v1/skills/live-skill",
                        json={"enabled": True},
                    )
                    self.assertEqual(r_patch_on.status_code, 200)
                    self.assertTrue(r_patch_on.json()["skill"]["enabled"])

                    # 5. POST /api/v1/skills/rescan
                    r_rescan = await client.post("/api/v1/skills/rescan")
                    self.assertEqual(r_rescan.status_code, 200)
                    self.assertTrue(
                        any(s["name"] == "live-skill" for s in r_rescan.json()["skills"])
                    )

                    # 6. Slash-command fast path injects <active_skill> into system prompt
                    self.mock_llm.calls.clear()
                    r_slash = await client.post(
                        "/api/v1/chat",
                        json={"content": "/live check status", "input_mode": "text"},
                    )
                    self.assertEqual(r_slash.status_code, 200)
                    self.assertGreaterEqual(len(self.mock_llm.calls), 1)
                    sys_prompt = self.mock_llm.calls[-1]["system_prompt"]
                    self.assertIn('<active_skill name="live-skill">', sys_prompt)
                    self.assertIn("Updated live instructions.", sys_prompt)
                    self.assertIn("</active_skill>", sys_prompt)

                    # 7. bytey_skill__load tool call execution
                    self.mock_llm.calls.clear()
                    self.mock_llm.tool_trigger_phrase = "trigger_skill_load"
                    self.mock_llm.tool_name_override = "bytey_skill__load"
                    self.mock_llm.tool_arguments = {"name": "live-skill"}
                    try:
                        r_tool = await client.post(
                            "/api/v1/chat",
                            json={"content": "Please trigger_skill_load now", "input_mode": "text"},
                        )
                        self.assertEqual(r_tool.status_code, 200)
                        self.assertGreaterEqual(len(self.mock_llm.calls), 2)
                        round2_msgs = self.mock_llm.calls[1]["messages"]
                        tool_msgs = [m for m in round2_msgs if m.get("role") == "tool"]
                        self.assertEqual(len(tool_msgs), 1)
                        self.assertIn("Updated live instructions.", tool_msgs[0]["content"])
                    finally:
                        self.mock_llm.tool_trigger_phrase = "use_echo_tool"
                        self.mock_llm.tool_name_override = None
                        self.mock_llm.tool_arguments = None

                    # 8. DELETE /api/v1/skills/{name}
                    r_del = await client.delete("/api/v1/skills/live-skill")
                    self.assertEqual(r_del.status_code, 200)
                    self.assertEqual(r_del.json()["deleted"], "live-skill")
                    r_get_gone = await client.get("/api/v1/skills/live-skill")
                    self.assertEqual(r_get_gone.status_code, 404)

                    await asyncio.sleep(0.05)
                finally:
                    reader_task.cancel()
                    await asyncio.gather(reader_task, return_exceptions=True)

        skills_ws_events = [e for e in events if e.get("type") == "skills_updated"]
        self.assertGreaterEqual(len(skills_ws_events), 5)

    async def test_workspace_rest_slash_commands_and_session_restore(self) -> None:
        ws1 = (self.root / "ws_one").resolve()
        ws2 = (self.root / "ws_two").resolve()
        (ws1 / ".agents" / "skills" / "ws-only-skill").mkdir(parents=True)
        (ws1 / ".agents" / "skills" / "ws-only-skill" / "SKILL.md").write_text(
            "---\nname: ws-only-skill\ndescription: Workspace skill\n---\n\nWorkspace body\n",
            encoding="utf-8",
        )
        ws2.mkdir(parents=True)

        async with httpx.AsyncClient(base_url=self.base_url, timeout=10.0) as client:
            r_init = await client.get("/api/v1/workspace")
            self.assertEqual(r_init.status_code, 200)
            self.assertEqual(r_init.json()["active_path"], "")

            r_bad = await client.put(
                "/api/v1/workspace",
                json={"path": str(self.root / "does_not_exist")},
            )
            self.assertEqual(r_bad.status_code, 400)

            r_sess_a = await client.post("/api/v1/sessions", json={"title": "Session A"})
            self.assertEqual(r_sess_a.status_code, 200)
            sess_a_id = r_sess_a.json()["id"]
            await client.post(f"/api/v1/sessions/{sess_a_id}/activate")

            events: list[dict[str, Any]] = []
            async with websockets.connect(self.ws_url) as ws:
                async def _ws_reader() -> None:
                    try:
                        async for raw in ws:
                            events.append(json.loads(raw))
                    except websockets.ConnectionClosed:
                        pass

                reader_task = asyncio.create_task(_ws_reader())
                try:
                    r_put1 = await client.put("/api/v1/workspace", json={"path": str(ws1)})
                    self.assertEqual(r_put1.status_code, 200)
                    p1 = r_put1.json()
                    self.assertEqual(p1["active_path"], str(ws1))
                    self.assertIn(str(ws1), p1["recent_paths"])
                    self.assertEqual(p1["workspace_skills_count"], 1)

                    r_sess_b = await client.post("/api/v1/sessions", json={"title": "Session B"})
                    self.assertEqual(r_sess_b.status_code, 200)
                    sess_b_id = r_sess_b.json()["id"]
                    await client.post(f"/api/v1/sessions/{sess_b_id}/activate")

                    r_cmd_set = await client.post(
                        "/api/v1/chat",
                        json={"content": f"/workspace {ws2}", "input_mode": "text"},
                    )
                    self.assertEqual(r_cmd_set.status_code, 200)
                    self.assertEqual(
                        (await client.get("/api/v1/workspace")).json()["active_path"],
                        str(ws2),
                    )

                    r_cmd_clear = await client.post(
                        "/api/v1/chat",
                        json={"content": "/workspace clear", "input_mode": "text"},
                    )
                    self.assertEqual(r_cmd_clear.status_code, 200)
                    self.assertEqual(
                        (await client.get("/api/v1/workspace")).json()["active_path"],
                        "",
                    )

                    # Activating Session A restores ws1 as the active workspace
                    r_act_a = await client.post(f"/api/v1/sessions/{sess_a_id}/activate")
                    self.assertEqual(r_act_a.status_code, 200)
                    restored = (await client.get("/api/v1/workspace")).json()
                    self.assertEqual(restored["active_path"], str(ws1))
                    self.assertEqual(restored["workspace_skills_count"], 1)

                    await asyncio.sleep(0.05)
                finally:
                    reader_task.cancel()
                    await asyncio.gather(reader_task, return_exceptions=True)

            ws_updated = [e for e in events if e.get("type") == "workspace_updated"]
            skills_updated = [e for e in events if e.get("type") == "skills_updated"]
            self.assertGreaterEqual(len(ws_updated), 4)
            self.assertGreaterEqual(len(skills_updated), 4)

    async def test_workspace_propose_edit_approval_and_rejection(self) -> None:
        ws = (self.root / "ws_edit").resolve()
        ws.mkdir(parents=True)
        target_file = ws / "app.py"
        target_file.write_text("x = 1\n", encoding="utf-8")

        async with httpx.AsyncClient(base_url=self.base_url, timeout=10.0) as client:
            r_ws = await client.put("/api/v1/workspace", json={"path": str(ws)})
            self.assertEqual(r_ws.status_code, 200)
            r_cfg = await client.patch(
                "/api/v1/config",
                json={
                    "llm": {
                        "provider": "gemini",
                        "model": "gemini-2.5-flash",
                        "force_enable_tools": True,
                    }
                },
            )
            self.assertEqual(r_cfg.status_code, 200)

            self.mock_llm.tool_trigger_phrase = "trigger_propose_edit"
            self.mock_llm.tool_name_override = "bytey_workspace__propose_edit"
            self.mock_llm.tool_arguments = {
                "rel_path": "app.py",
                "target": "x = 1",
                "replacement": "x = 2",
            }
            approve_next = {"value": False}
            awaiting_events: list[dict[str, Any]] = []

            async with websockets.connect(self.ws_url) as ws_conn:
                async def _ws_reader() -> None:
                    try:
                        async for raw in ws_conn:
                            ev = json.loads(raw)
                            if (
                                ev.get("type") == "tool_call_status"
                                and ev.get("status") == "awaiting_confirm"
                            ):
                                awaiting_events.append(ev)
                                call_id = ev["call_id"]
                                resp = await client.post(
                                    f"/api/v1/tools/{call_id}/confirm",
                                    json={"approved": approve_next["value"]},
                                )
                                self.assertEqual(resp.status_code, 200)
                    except websockets.ConnectionClosed:
                        pass

                reader_task = asyncio.create_task(_ws_reader())
                try:
                    approve_next["value"] = False
                    r_reject = await client.post(
                        "/api/v1/chat",
                        json={"content": "Please trigger_propose_edit", "input_mode": "text"},
                    )
                    self.assertEqual(r_reject.status_code, 200)
                    self.assertEqual(target_file.read_text(encoding="utf-8"), "x = 1\n")

                    approve_next["value"] = True
                    r_approve = await client.post(
                        "/api/v1/chat",
                        json={"content": "Please trigger_propose_edit", "input_mode": "text"},
                    )
                    self.assertEqual(r_approve.status_code, 200)
                    self.assertEqual(target_file.read_text(encoding="utf-8"), "x = 2\n")
                finally:
                    self.mock_llm.tool_trigger_phrase = "use_echo_tool"
                    self.mock_llm.tool_name_override = None
                    self.mock_llm.tool_arguments = None
                    reader_task.cancel()
                    await asyncio.gather(reader_task, return_exceptions=True)

            self.assertEqual(len(awaiting_events), 2)
            for ev in awaiting_events:
                self.assertEqual(ev.get("server"), "bytey_workspace")
                self.assertEqual(ev.get("tool"), "propose_edit")
                self.assertIn("-x = 1", ev.get("diff", ""))
                self.assertIn("+x = 2", ev.get("diff", ""))

    async def test_workspace_delete_and_rename_approval_and_rejection(self) -> None:
        ws = (self.root / "ws_del_ren").resolve()
        ws.mkdir(parents=True)
        src_file = ws / "old_name.py"
        src_file.write_text("val = 42\n", encoding="utf-8")

        async with httpx.AsyncClient(base_url=self.base_url, timeout=10.0) as client:
            r_ws = await client.put("/api/v1/workspace", json={"path": str(ws)})
            self.assertEqual(r_ws.status_code, 200)
            r_cfg = await client.patch(
                "/api/v1/config",
                json={
                    "llm": {
                        "provider": "gemini",
                        "model": "gemini-2.5-flash",
                        "force_enable_tools": True,
                    }
                },
            )
            self.assertEqual(r_cfg.status_code, 200)

            approve_next = {"value": False}
            awaiting_events: list[dict[str, Any]] = []

            async with websockets.connect(self.ws_url) as ws_conn:
                async def _ws_reader() -> None:
                    try:
                        async for raw in ws_conn:
                            ev = json.loads(raw)
                            if (
                                ev.get("type") == "tool_call_status"
                                and ev.get("status") == "awaiting_confirm"
                            ):
                                awaiting_events.append(ev)
                                call_id = ev["call_id"]
                                resp = await client.post(
                                    f"/api/v1/tools/{call_id}/confirm",
                                    json={"approved": approve_next["value"]},
                                )
                                self.assertEqual(resp.status_code, 200)
                    except websockets.ConnectionClosed:
                        pass

                reader_task = asyncio.create_task(_ws_reader())
                try:
                    # 1) Reject then approve rename_file
                    self.mock_llm.tool_trigger_phrase = "trigger_rename"
                    self.mock_llm.tool_name_override = "bytey_workspace__rename_file"
                    self.mock_llm.tool_arguments = {
                        "src_rel_path": "old_name.py",
                        "dst_rel_path": "pkg/new_name.py",
                    }
                    approve_next["value"] = False
                    r_ren_rej = await client.post(
                        "/api/v1/chat",
                        json={"content": "Please trigger_rename", "input_mode": "text"},
                    )
                    self.assertEqual(r_ren_rej.status_code, 200)
                    self.assertTrue(src_file.exists())
                    self.assertFalse((ws / "pkg" / "new_name.py").exists())

                    approve_next["value"] = True
                    r_ren_ok = await client.post(
                        "/api/v1/chat",
                        json={"content": "Please trigger_rename", "input_mode": "text"},
                    )
                    self.assertEqual(r_ren_ok.status_code, 200)
                    self.assertFalse(src_file.exists())
                    self.assertTrue((ws / "pkg" / "new_name.py").exists())

                    # 2) Reject then approve delete_file
                    self.mock_llm.tool_trigger_phrase = "trigger_delete"
                    self.mock_llm.tool_name_override = "bytey_workspace__delete_file"
                    self.mock_llm.tool_arguments = {"rel_path": "pkg/new_name.py"}
                    approve_next["value"] = False
                    r_del_rej = await client.post(
                        "/api/v1/chat",
                        json={"content": "Please trigger_delete", "input_mode": "text"},
                    )
                    self.assertEqual(r_del_rej.status_code, 200)
                    self.assertTrue((ws / "pkg" / "new_name.py").exists())

                    approve_next["value"] = True
                    r_del_ok = await client.post(
                        "/api/v1/chat",
                        json={"content": "Please trigger_delete", "input_mode": "text"},
                    )
                    self.assertEqual(r_del_ok.status_code, 200)
                    self.assertFalse((ws / "pkg" / "new_name.py").exists())
                finally:
                    self.mock_llm.tool_trigger_phrase = "use_echo_tool"
                    self.mock_llm.tool_name_override = None
                    self.mock_llm.tool_arguments = None
                    reader_task.cancel()
                    await asyncio.gather(reader_task, return_exceptions=True)

            self.assertEqual(len(awaiting_events), 4)
            self.assertEqual(awaiting_events[0].get("tool"), "rename_file")
            self.assertEqual(awaiting_events[2].get("tool"), "delete_file")
            self.assertIn("-val = 42", awaiting_events[2].get("diff", ""))

    async def test_delete_message_and_tts_pause_resume(self) -> None:
        """Verify DELETE message endpoint (404/200 + WS) and POST /api/v1/tts/pause."""
        events: list[dict[str, Any]] = []

        async with httpx.AsyncClient(base_url=self.base_url, timeout=10.0) as client:
            async with websockets.connect(self.ws_url) as ws:
                async def _ws_reader() -> None:
                    try:
                        async for raw in ws:
                            events.append(json.loads(raw))
                    except websockets.ConnectionClosed:
                        pass

                reader_task = asyncio.create_task(_ws_reader())
                try:
                    r_s = await client.post("/api/v1/sessions", json={"title": "Delete & Pause"})
                    self.assertEqual(r_s.status_code, 200)
                    sid = r_s.json()["id"]

                    r_post = await client.post(
                        f"/api/v1/sessions/{sid}/messages",
                        json={"content": "Message to delete", "input_mode": "text"},
                    )
                    self.assertEqual(r_post.status_code, 200)

                    r_msgs = await client.get(f"/api/v1/sessions/{sid}/messages")
                    self.assertEqual(r_msgs.status_code, 200)
                    msgs = r_msgs.json()["messages"]
                    self.assertGreaterEqual(len(msgs), 1)
                    mid = msgs[0]["id"]

                    # Missing message -> 404
                    r_404 = await client.delete(
                        f"/api/v1/sessions/{sid}/messages/nonexistent-msg-id"
                    )
                    self.assertEqual(r_404.status_code, 404)

                    # Existing message -> 200 + message_deleted & sessions_updated
                    events.clear()
                    r_del = await client.delete(f"/api/v1/sessions/{sid}/messages/{mid}")
                    self.assertEqual(r_del.status_code, 200)
                    self.assertEqual(r_del.json(), {"deleted": mid, "session_id": sid})

                    r_after = await client.get(f"/api/v1/sessions/{sid}/messages")
                    self.assertEqual(r_after.status_code, 200)
                    self.assertNotIn(mid, [m["id"] for m in r_after.json()["messages"]])

                    # TTS pause/resume toggle + explicit paused flag
                    self.daemon.tts_player._speaking = True
                    self.daemon._speaking_message_id = "msg-tts-1"
                    try:
                        r_p1 = await client.post("/api/v1/tts/pause", json={})
                        self.assertEqual(r_p1.status_code, 200)
                        self.assertEqual(
                            r_p1.json(),
                            {"ok": True, "paused": True, "message_id": "msg-tts-1"},
                        )

                        r_p2 = await client.post("/api/v1/tts/pause", json={})
                        self.assertEqual(r_p2.status_code, 200)
                        self.assertEqual(
                            r_p2.json(),
                            {"ok": True, "paused": False, "message_id": "msg-tts-1"},
                        )

                        r_p3 = await client.post("/api/v1/tts/pause", json={"paused": True})
                        self.assertEqual(r_p3.status_code, 200)
                        self.assertTrue(r_p3.json()["paused"])

                        r_p4 = await client.post("/api/v1/tts/pause", json={"paused": False})
                        self.assertEqual(r_p4.status_code, 200)
                        self.assertFalse(r_p4.json()["paused"])
                    finally:
                        await self.daemon.tts_player.barge_in()
                        self.daemon._speaking_message_id = None

                    await asyncio.sleep(0.05)
                finally:
                    reader_task.cancel()
                    await asyncio.gather(reader_task, return_exceptions=True)

        deleted_evs = [e for e in events if e.get("type") == "message_deleted"]
        self.assertEqual(len(deleted_evs), 1)
        self.assertEqual(deleted_evs[0]["session_id"], sid)
        self.assertEqual(deleted_evs[0]["message_id"], mid)

        updated_evs = [e for e in events if e.get("type") == "sessions_updated"]
        self.assertGreaterEqual(len(updated_evs), 1)

        tts_evs = [e for e in events if e.get("type") == "tts_playback"]
        self.assertEqual(
            [(e.get("status"), e.get("message_id")) for e in tts_evs],
            [
                ("paused", "msg-tts-1"),
                ("playing", "msg-tts-1"),
                ("paused", "msg-tts-1"),
                ("playing", "msg-tts-1"),
            ],
        )

    async def test_get_mcp_host_builtin_tools(self) -> None:
        async with httpx.AsyncClient(base_url=self.base_url, timeout=5.0) as client:
            resp = await client.get("/api/v1/mcp/host")
            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            self.assertTrue(data["ok"])
            self.assertEqual(data["title"], "Bytey MCP Host")
            self.assertTrue(bool(data["description"]))
            tools_by_name = {t["name"]: t["description"] for t in data["tools"]}
            self.assertIn("bytey_workspace__list_files", tools_by_name)
            self.assertIn("bytey_skill__load", tools_by_name)
            self.assertTrue(all(bool(desc) for desc in tools_by_name.values()))

    async def test_multi_round_tool_loop_wrapup_and_cancel(self) -> None:
        from bytey_ai_companion.config import ProviderConfig

        self.assertEqual(ProviderConfig().max_tool_rounds, 25)
        self.assertEqual(ProviderConfig(max_tool_rounds=0).max_tool_rounds, 1)
        self.assertEqual(ProviderConfig(max_tool_rounds=999).max_tool_rounds, 100)

        with tempfile.TemporaryDirectory() as ws_tmp:
            Path(ws_tmp, "a.txt").write_text("hi\n", encoding="utf-8")
            orig_generate = self.daemon.llm_router.generate_chat
            try:
                async with httpx.AsyncClient(base_url=self.base_url, timeout=10.0) as client:
                    await client.put("/api/v1/workspace", json={"active_path": ws_tmp})
                    r_cfg = await client.patch(
                        "/api/v1/config",
                        json={
                            "llm": {"max_tool_rounds": 8, "supports_tools": True, "force_enable_tools": True},
                        },
                    )
                    self.assertEqual(r_cfg.status_code, 200)
                    self.assertEqual(r_cfg.json()["llm"]["max_tool_rounds"], 8)

                    calls_8 = {"n": 0}

                    async def mock_eight_rounds(**kwargs):
                        calls_8["n"] += 1
                        if calls_8["n"] <= 7:
                            return {
                                "content": "",
                                "emotion": "focus",
                                "tool_calls": [
                                    {
                                        "id": f"c_{calls_8['n']}",
                                        "name": "bytey_workspace__list_files",
                                        "arguments": {},
                                    }
                                ],
                                "raw_assistant_message": {"role": "assistant", "content": ""},
                            }
                        if kwargs.get("on_text_chunk"):
                            await kwargs["on_text_chunk"]("Finished 8 rounds!")
                        return {"content": "Finished 8 rounds!", "emotion": "happy", "tool_calls": []}

                    self.daemon.llm_router.generate_chat = mock_eight_rounds
                    r_chat = await client.post(
                        "/api/v1/chat",
                        json={"content": "Do 8 rounds", "input_mode": "text"},
                    )
                    self.assertEqual(r_chat.status_code, 200)
                    self.assertEqual(calls_8["n"], 8)
                    self.assertIn("Finished 8 rounds!", r_chat.json()["assistant_message"]["content"])

                    # Now test round cap wrap-up (max_tool_rounds=2)
                    await client.patch("/api/v1/config", json={"llm": {"max_tool_rounds": 2}})
                    wrap_calls = {"n": 0, "wrap_tools_none": False}

                    async def mock_cap_hit(**kwargs):
                        wrap_calls["n"] += 1
                        if kwargs.get("tools") is None:
                            wrap_calls["wrap_tools_none"] = True
                            if kwargs.get("on_text_chunk"):
                                await kwargs["on_text_chunk"]("Wrap-up summary.")
                            return {"content": "Wrap-up summary.", "emotion": "neutral", "tool_calls": []}
                        return {
                            "content": "",
                            "emotion": "focus",
                            "tool_calls": [
                                {
                                    "id": f"cap_{wrap_calls['n']}",
                                    "name": "bytey_workspace__list_files",
                                    "arguments": {},
                                }
                            ],
                            "raw_assistant_message": {"role": "assistant", "content": ""},
                        }

                    self.daemon.llm_router.generate_chat = mock_cap_hit
                    r_cap = await client.post(
                        "/api/v1/chat",
                        json={"content": "Loop forever", "input_mode": "text"},
                    )
                    self.assertEqual(r_cap.status_code, 200)
                    self.assertEqual(wrap_calls["n"], 3)
                    self.assertTrue(wrap_calls["wrap_tools_none"])
                    self.assertIn("Wrap-up summary.", r_cap.json()["assistant_message"]["content"])

                    r_cancel = await client.post("/api/v1/prompt/cancel")
                    self.assertEqual(r_cancel.status_code, 200)
                    self.assertTrue(r_cancel.json()["ok"])
            finally:
                self.daemon.llm_router.generate_chat = orig_generate


class TestSettingsClientAndSyntax(unittest.TestCase):
    """Test DaemonClient offline fallback + secret stripping and static syntax checks."""

    def test_daemon_client_offline_fallback_and_secret_stripping(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            closed_port = s.getsockname()[1]

        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = Path(tmp) / "config.json"
            with DaemonClient(
                base_url=f"http://127.0.0.1:{closed_port}",
                config_path=cfg_path,
                timeout=1.0,
            ) as client:
                get_res = client.get_config()
                self.assertTrue(get_res.ok)
                self.assertTrue(get_res.offline)
                self.assertEqual(get_res.data["general"]["pet_name"], "Bytey")

                patch_res = client.patch_config(
                    {
                        "general": {"pet_name": "Pixel"},
                        "OPENAI_API_KEY": "sk-must-be-stripped",
                        "llm": {"model": "gpt-4o-mini", "api_key": "must-also-be-stripped"},
                    }
                )
                self.assertTrue(patch_res.ok)
                self.assertTrue(patch_res.offline)
                self.assertEqual(patch_res.data["general"]["pet_name"], "Pixel")
                self.assertEqual(patch_res.data["llm"]["model"], "gpt-4o-mini")
                self.assertNotIn("OPENAI_API_KEY", patch_res.data)
                self.assertNotIn("api_key", patch_res.data["llm"])

                raw_written = cfg_path.read_text(encoding="utf-8")
                self.assertNotIn("sk-must-be-stripped", raw_written)
                self.assertNotIn("must-also-be-stripped", raw_written)

                sec_res = client.put_secrets({"OPENAI_API_KEY": "sk-123"})
                self.assertFalse(sec_res.ok)
                self.assertTrue(sec_res.offline)

    def test_settings_client_offline_legacy_fallback(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            closed_port = s.getsockname()[1]

        with tempfile.TemporaryDirectory() as tmp:
            fake_home = Path(tmp)
            primary_cfg = fake_home / ".config" / "bytey-ai-companion" / "config.json"
            legacy_cfg = fake_home / ".config" / "ai-pet" / "config.json"
            clean_env = {
                k: v
                for k, v in os.environ.items()
                if k not in ("BYTEY_CONFIG_DIR", "AI_PET_CONFIG_DIR")
            }

            with patch.dict(os.environ, clean_env, clear=True), patch(
                "pathlib.Path.home", return_value=fake_home
            ):
                # Returns primary path when neither exists
                self.assertEqual(resolve_local_config_path(), primary_cfg)

                # Falls back to legacy ~/.config/ai-pet/config.json when only legacy exists
                legacy_cfg.parent.mkdir(parents=True, exist_ok=True)
                legacy_cfg.write_text(
                    json.dumps({"general": {"pet_name": "LegacyBuddy"}}),
                    encoding="utf-8",
                )
                self.assertEqual(resolve_local_config_path(), legacy_cfg)

                # DaemonClient without explicit config_path reads custom value from legacy config
                with DaemonClient(
                    base_url=f"http://127.0.0.1:{closed_port}",
                    timeout=1.0,
                ) as client:
                    get_res = client.get_config()
                    self.assertTrue(get_res.ok)
                    self.assertTrue(get_res.offline)
                    self.assertEqual(get_res.data["general"]["pet_name"], "LegacyBuddy")

                # Prefers primary when both exist
                primary_cfg.parent.mkdir(parents=True, exist_ok=True)
                primary_cfg.write_text(
                    json.dumps({"general": {"pet_name": "PrimaryBytey"}}),
                    encoding="utf-8",
                )
                self.assertEqual(resolve_local_config_path(), primary_cfg)

    def test_py_compile_all_python_files(self) -> None:
        py_files = [
            p
            for p in REPO_ROOT.rglob("*.py")
            if "__pycache__" not in p.parts and ".venv" not in p.parts
        ]
        self.assertGreater(len(py_files), 0)
        for py_file in py_files:
            with self.subTest(file=str(py_file.relative_to(REPO_ROOT))):
                py_compile.compile(str(py_file), doraise=True)

    def test_js_syntax_and_metadata_json(self) -> None:
        js_files = sorted(GNOME_EXT_DIR.glob("*.js"))
        self.assertGreater(len(js_files), 0)
        for js_file in js_files:
            with self.subTest(file=js_file.name):
                proc = subprocess.run(
                    ["node", "--check", str(js_file)],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(
                    proc.returncode,
                    0,
                    f"node --check failed for {js_file}: {proc.stderr}",
                )
                js_text = js_file.read_text(encoding="utf-8")
                self.assertNotIn("ai-pet-", js_text)
                self.assertNotRegex(js_text, r"^\s*vertical\s*:", msg=f"Removed GNOME 51 St.BoxLayout 'vertical' property in {js_file}")
                self.assertNotRegex(js_text, r"\basync\s+disable\s*\(", msg=f"Async disable() throws in GNOME 51 in {js_file}")

        css_path = GNOME_EXT_DIR / "stylesheet.css"
        self.assertTrue(css_path.is_file())
        css_text = css_path.read_text(encoding="utf-8")
        self.assertNotIn("ai-pet-", css_text)
        self.assertIn(".popup-menu-item", css_text)

        meta_path = GNOME_EXT_DIR / "metadata.json"
        self.assertTrue(meta_path.is_file())
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        self.assertEqual(meta.get("uuid"), "bytey-ai-companion@c42759.gitlab.com")
        self.assertEqual(meta.get("name"), "Bytey AI Companion")
        self.assertIsInstance(meta.get("description"), str)
        self.assertEqual(meta.get("shell-version"), ["48", "49", "50", "51"])
        self.assertEqual(meta.get("url"), "https://gitlab.com/c42759/bytey")
        self.assertEqual(
            meta.get("settings-schema"),
            "org.gnome.shell.extensions.bytey-ai-companion",
        )
        self.assertNotIn("version", meta)
        self.assertIsInstance(meta.get("version-name"), str)

    def test_ego_submission_readiness(self) -> None:
        """Verify EGO packaging, GSettings schema, license, and GJS rules."""
        license_path = GNOME_EXT_DIR / "LICENSE"
        self.assertTrue(license_path.is_file())
        license_text = license_path.read_text(encoding="utf-8")
        self.assertIn("GNU GENERAL PUBLIC LICENSE", license_text)
        self.assertIn("Version 3", license_text)

        schemas_dir = GNOME_EXT_DIR / "schemas"
        schema_path = (
            schemas_dir / "org.gnome.shell.extensions.bytey-ai-companion.gschema.xml"
        )
        self.assertTrue(schema_path.is_file())
        schema_text = schema_path.read_text(encoding="utf-8")
        self.assertIn('id="org.gnome.shell.extensions.bytey-ai-companion"', schema_text)
        self.assertIn(
            'path="/org/gnome/shell/extensions/bytey-ai-companion/"', schema_text
        )
        tracked_compiled = subprocess.run(
            [
                "git",
                "ls-files",
                str((schemas_dir / "gschemas.compiled").relative_to(REPO_ROOT)),
            ],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(tracked_compiled.returncode, 0)
        self.assertEqual(
            tracked_compiled.stdout.strip(),
            "",
            "gschemas.compiled must never be tracked in git",
        )

        schema_proc = subprocess.run(
            ["glib-compile-schemas", "--strict", "--dry-run", str(schemas_dir)],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(
            schema_proc.returncode,
            0,
            f"glib-compile-schemas failed: {schema_proc.stderr}",
        )

        icons_dir = GNOME_EXT_DIR / "icons"
        for icon_name, expected_viewbox in (
            ("bytey-ai-companion.svg", "0 0 128 128"),
            ("bytey-ai-companion-symbolic.svg", "0 0 16 16"),
        ):
            with self.subTest(icon=icon_name):
                svg_path = icons_dir / icon_name
                self.assertTrue(svg_path.is_file(), f"Missing SVG icon: {svg_path}")
                root = ET.parse(svg_path).getroot()
                self.assertTrue(root.tag.endswith("svg"))
                self.assertEqual(root.attrib.get("viewBox"), expected_viewbox)

        png_icon_path = REPO_ROOT / "gnome-extension" / "bytey-ai-companion-icon-128.png"
        self.assertTrue(png_icon_path.is_file(), f"Missing PNG icon: {png_icon_path}")
        png_bytes = png_icon_path.read_bytes()
        self.assertTrue(png_bytes.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertGreaterEqual(len(png_bytes), 24)
        self.assertEqual(struct.unpack(">II", png_bytes[16:24]), (128, 128))

        prefs_text = (GNOME_EXT_DIR / "prefs.js").read_text(encoding="utf-8")
        self.assertIn("ExtensionPreferences", prefs_text)
        self.assertIn("gi://Soup?version=3.0", prefs_text)
        self.assertIn("bytey-ai-companion.svg", prefs_text)
        for forbidden_import in (
            "gi://St",
            "gi://Clutter",
            "gi://Meta",
            "gi://Shell",
            "resource:///org/gnome/shell/",
        ):
            self.assertNotIn(forbidden_import, prefs_text)

        ext_text = (GNOME_EXT_DIR / "extension.js").read_text(encoding="utf-8")
        self.assertIn("gi://Soup?version=3.0", ext_text)
        self.assertNotIn("'gi://Soup'", ext_text)
        self.assertNotIn('"gi://Soup"', ext_text)
        self.assertIn("this.getSettings()", ext_text)
        self.assertIn("this.openPreferences()", ext_text)
        self.assertIn("    disable() {", ext_text)
        disable_body = ext_text.split("    disable() {", 1)[1].split("\n    }\n", 1)[0]
        self.assertIn("this._settings.disconnect(", disable_body)
        self.assertIn("this._settings = null;", disable_body)

        js_files = sorted(GNOME_EXT_DIR.glob("*.js"))
        self.assertGreater(len(js_files), 0)
        forbidden_js_tokens = (
            "/home/",
            "Gio.Subprocess",
            "GLib.spawn_",
            "GLib.source_remove(",
        )
        for js_file in js_files:
            with self.subTest(file=js_file.name):
                js_text = js_file.read_text(encoding="utf-8")
                self.assertTrue(
                    js_text.startswith("// SPDX-License-Identifier: GPL-3.0-or-later"),
                    f"{js_file.name} missing SPDX header",
                )
                for token in forbidden_js_tokens:
                    self.assertNotIn(token, js_text)

        expected_zip_entries = sorted(
            [
                "LICENSE",
                "contextMenu.js",
                "extension.js",
                "eyeRenderer.js",
                "icons/bytey-ai-companion-symbolic.svg",
                "icons/bytey-ai-companion.svg",
                "islandWidget.js",
                "metadata.json",
                "positionManager.js",
                "prefs.js",
                "schemas/org.gnome.shell.extensions.bytey-ai-companion.gschema.xml",
                "stylesheet.css",
                "transcriptDrawer.js",
            ]
        )
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            pack_proc = subprocess.run(
                [
                    "bash",
                    str(REPO_ROOT / "gnome-extension" / "pack-ego.sh"),
                    str(tmp_path),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(
                pack_proc.returncode,
                0,
                f"pack-ego.sh failed: {pack_proc.stdout}\n{pack_proc.stderr}",
            )
            zip_path = (
                tmp_path / "bytey-ai-companion@c42759.gitlab.com.shell-extension.zip"
            )
            self.assertTrue(zip_path.is_file())
            with zipfile.ZipFile(zip_path, "r") as zf:
                zip_entries = sorted(zf.namelist())
            self.assertEqual(zip_entries, expected_zip_entries)
            for entry in zip_entries:
                self.assertNotIn("gschemas.compiled", entry)
                self.assertNotIn("__pycache__", entry)
                self.assertNotIn(".git", entry)

    @staticmethod
    def _extract_js_method(source: str, signature: str) -> str:
        """Extract a balanced-brace JS block starting at signature."""
        start = source.find(signature)
        if start == -1:
            raise AssertionError(f"Missing JS signature: {signature}")
        open_idx = source.find("{", start)
        if open_idx == -1:
            raise AssertionError(f"Missing opening brace for {signature}")
        depth = 0
        for idx in range(open_idx, len(source)):
            ch = source[idx]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return source[start : idx + 1]
        raise AssertionError(f"Unbalanced braces for {signature}")

    def _run_node_script(
        self,
        script: str,
        *args: str,
        failure_context: str,
    ) -> None:
        """Execute a Node verification script with positional arguments."""
        proc = subprocess.run(
            ["node", "-e", script, *args],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(
            proc.returncode,
            0,
            f"{failure_context}: {proc.stderr or proc.stdout}",
        )

    def test_transcript_drawer_tool_placement_and_thinking_css(self) -> None:
        css_text = (GNOME_EXT_DIR / "stylesheet.css").read_text(encoding="utf-8")
        thinking_match = re.search(r"\.bytey-island-thinking\s*\{([^}]*)\}", css_text)
        self.assertIsNotNone(thinking_match)
        thinking_body = thinking_match.group(1)  # type: ignore[union-attr]
        for forbidden_rgb in ("168, 85, 247", "139, 92, 246"):
            self.assertNotIn(forbidden_rgb, thinking_body)

        island_rules = re.findall(
            r"^\.bytey-island[^{]*\{([^}]*)\}",
            css_text,
            flags=re.MULTILINE,
        )
        self.assertGreater(len(island_rules), 0)
        for rule_body in island_rules:
            for forbidden_rgb in ("168, 85, 247", "139, 92, 246"):
                self.assertNotIn(forbidden_rgb, rule_body)

        island_js = (GNOME_EXT_DIR / "islandWidget.js").read_text(encoding="utf-8")
        for symbol in ("_capsulePoint", "_hslToRgb", "_strokeRainbowCapsule"):
            self.assertIn(symbol, island_js)

        drawer_js = (GNOME_EXT_DIR / "transcriptDrawer.js").read_text(encoding="utf-8")
        update_method = self._extract_js_method(drawer_js, "updateToolCall(event) {")
        normalize_method = self._extract_js_method(drawer_js, "_normalizeTool(raw) {")
        node_script = """
const assert = require('assert');
const [updateMethod, normalizeMethod] = process.argv.slice(1);
const methods = new Function(`return ({ ${updateMethod}, ${normalizeMethod} });`)();

function makeMockDrawer() {
    const msg0 = {id: 'msg0', role: 'assistant', tool_calls: []};
    const msg1 = {id: 'msg1', role: 'user', tool_calls: []};
    return {
        _destroyed: false,
        _messages: [msg0, msg1],
        _messageWidgets: new Map([
            ['msg0', {msg: msg0, toolBox: {children: []}}],
            ['msg1', {msg: msg1, toolBox: {children: []}}],
        ]),
        _toolWidgets: new Map(),
        _normalizeTool: methods._normalizeTool,
        updateToolCall: methods.updateToolCall,
        upsertMessageDelta(event) {
            const msg = {
                id: event.message_id,
                role: event.role,
                content: event.text ?? '',
                tool_calls: [],
            };
            this._messages.push(msg);
            this._messageWidgets.set(msg.id, {msg, toolBox: {children: []}});
        },
        _appendToolWidget(toolBox, tool) {
            const rec = {tool};
            toolBox.children.push(rec);
            this._toolWidgets.set(tool.id, rec);
        },
        _refreshToolWidget() {},
        _scheduleScrollToBottom() {},
    };
}

// Case A: message_id "msg2" arrives before any transcript delta for msg2
const drawerA = makeMockDrawer();
drawerA.updateToolCall({
    call_id: 'call_a',
    message_id: 'msg2',
    server: 'weather',
    tool: 'get_current_weather',
    status: 'running',
});
assert.strictEqual(drawerA._messages[0].tool_calls.length, 0);
assert.deepStrictEqual(drawerA._messages.map(m => m.id), ['msg0', 'msg1', 'msg2']);
assert.strictEqual(drawerA._messages[2].role, 'assistant');
assert.strictEqual(drawerA._messages[2].tool_calls.length, 1);

// Case B: no message_id after user msg1 creates new assistant after msg1 instead of attaching to msg0
const drawerB = makeMockDrawer();
drawerB.updateToolCall({
    call_id: 'call_b',
    server: 'search',
    tool: 'web_search',
    status: 'running',
});
assert.strictEqual(drawerB._messages[0].tool_calls.length, 0);
assert.deepStrictEqual(drawerB._messages.map(m => m.id), ['msg0', 'msg1', 'tool-msg-call_b']);
assert.strictEqual(drawerB._messages[2].role, 'assistant');
assert.strictEqual(drawerB._messages[2].tool_calls.length, 1);
"""
        self._run_node_script(
            node_script,
            update_method,
            normalize_method,
            failure_context="node transcriptDrawer updateToolCall test failed",
        )

    def test_markdown_rendering_blocks_and_inline_markup(self) -> None:
        ext_dir = REPO_ROOT / "gnome-extension" / "bytey-ai-companion@c42759.github.com"
        drawer_js = (ext_dir / "transcriptDrawer.js").read_text(encoding="utf-8")
        css_text = (ext_dir / "stylesheet.css").read_text(encoding="utf-8")

        for selector in (
            ".bytey-md-heading",
            ".bytey-md-h1",
            ".bytey-md-h2",
            ".bytey-md-h3",
            ".bytey-md-list-row",
            ".bytey-md-list-marker",
            ".bytey-md-blockquote",
            ".bytey-md-hr",
        ):
            self.assertIn(selector, css_text)

        fmt_method = self._extract_js_method(drawer_js, "_formatInlineMarkup(text) {")
        blocks_method = self._extract_js_method(drawer_js, "_parseMarkdownBlocks(text) {")
        self._run_node_script(
            r"""
const assert = require('assert');
const [fmtMethod, blocksMethod] = process.argv.slice(1);
const GLib = {
    markup_escape_text(s) {
        return String(s)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;');
    },
};
const methods = new Function('GLib', `return ({ ${fmtMethod}, ${blocksMethod} });`)(GLib);

const inline = methods._formatInlineMarkup('***both*** **bold** *italic* ~~old~~ `x < y` [site](https://example.com) **unclosed');
assert.ok(inline.includes('<b><i>both</i></b>'));
assert.ok(inline.includes('<b>bold</b>'));
assert.ok(inline.includes('<i>italic</i>'));
assert.ok(inline.includes('<s>old</s>'));
assert.ok(inline.includes('<tt><span background="#1e293b" foreground="#e2e8f0"> x &lt; y </span></tt>'));
assert.ok(inline.includes('<span foreground="#38bdf8" underline="single">site</span>'));
assert.ok(inline.endsWith('**unclosed'));

const blocks = methods._parseMarkdownBlocks('# Title\n\n* First\n  1. Nested\n> Quote\n---\nTail');
assert.deepStrictEqual(
    blocks.map(b => b.type),
    ['heading', 'list_item', 'list_item', 'blockquote', 'hr', 'paragraph']
);
assert.strictEqual(blocks[0].level, 1);
assert.strictEqual(blocks[1].marker, '•');
assert.strictEqual(blocks[2].indent, 1);
assert.strictEqual(blocks[2].marker, '1.');
""",
            fmt_method,
            blocks_method,
            failure_context="node transcriptDrawer markdown rendering test failed",
        )

    def test_drawer_header_alignment_and_connection_status_indicator(self) -> None:
        css_text = (GNOME_EXT_DIR / "stylesheet.css").read_text(encoding="utf-8")
        self.assertNotIn(".bytey-status-dot-online", css_text)

        expected_css_rules = {
            ".bytey-status-connected": ("#22c55e",),
            ".bytey-status-error": ("#ef4444",),
            ".bytey-status-disconnected": ("#000000", "#ffffff"),
            ".bytey-status-tooltip": (),
            ".bytey-provider-pill-label": (),
        }
        for selector, expected_tokens in expected_css_rules.items():
            match = re.search(rf"{re.escape(selector)}\s*\{{([^}}]*)\}}", css_text)
            self.assertIsNotNone(match, f"Missing CSS rule for {selector}")
            rule_body = match.group(1)  # type: ignore[union-attr]
            for token in expected_tokens:
                self.assertIn(token, rule_body, f"{selector} missing {token}")

        drawer_js = (GNOME_EXT_DIR / "transcriptDrawer.js").read_text(encoding="utf-8")
        build_header = self._extract_js_method(drawer_js, "_buildHeader() {")
        self.assertRegex(
            build_header,
            r"this\._providerPill\s*=\s*new St\.Bin\(\{[^}]*child:\s*this\._providerLabel",
        )
        self.assertRegex(
            build_header,
            r"this\._connect\(\s*this\._statusIndicator\s*,\s*'notify::hover'\s*,\s*\(\)\s*=>\s*\{[^}]*"
            r"this\._showStatusTooltip\(\)[^}]*this\._hideStatusTooltip\(\)[^}]*\}\s*\)",
        )

        states_match = re.search(
            r"const\s+CONNECTION_STATES\s*=\s*\[[^\]]*\];",
            drawer_js,
        )
        self.assertIsNotNone(states_match, "Missing CONNECTION_STATES in transcriptDrawer.js")
        states_decl = states_match.group(0)  # type: ignore[union-attr]
        labels_decl = (
            self._extract_js_method(drawer_js, "const CONNECTION_STATUS_LABELS =") + ";"
        )
        set_status_method = self._extract_js_method(
            drawer_js, "setConnectionStatus(status) {"
        )

        node_script = """
const assert = require('assert');
const [statesDecl, labelsDecl, setStatusMethod] = process.argv.slice(1);
const methods = new Function(
    `${statesDecl}\n${labelsDecl}\nreturn ({ ${setStatusMethod} });`
)();

const classes = new Set(['bytey-status-dot', 'bytey-status-disconnected']);
const drawer = {
    _destroyed: false,
    _connectionState: 'disconnected',
    _online: false,
    _statusDot: {
        classes,
        add_style_class_name(cls) { classes.add(cls); },
        remove_style_class_name(cls) { classes.delete(cls); },
    },
    _statusIndicator: {accessible_name: 'Disconnected'},
    _statusTooltip: {visible: false},
    tooltipRefreshCount: 0,
    _showStatusTooltip() {
        this.tooltipRefreshCount += 1;
    },
    setConnectionStatus: methods.setConnectionStatus,
};

function assertState(expectedState, expectedOnline, expectedLabel) {
    assert.strictEqual(drawer._connectionState, expectedState);
    assert.strictEqual(drawer._online, expectedOnline);
    assert.strictEqual(drawer._statusIndicator.accessible_name, expectedLabel);
    for (const state of ['connected', 'error', 'disconnected']) {
        assert.strictEqual(
            classes.has(`bytey-status-${state}`),
            state === expectedState,
            `Unexpected class state for bytey-status-${state} when state=${expectedState}`
        );
    }
}

drawer.setConnectionStatus('connected');
assertState('connected', true, 'Connected');
assert.strictEqual(drawer.tooltipRefreshCount, 0);

drawer._statusTooltip.visible = true;
drawer.setConnectionStatus('connected');
assertState('connected', true, 'Connected');
assert.strictEqual(drawer.tooltipRefreshCount, 1);
drawer._statusTooltip.visible = false;

drawer.setConnectionStatus('error');
assertState('error', false, 'Connection error');
assert.strictEqual(drawer.tooltipRefreshCount, 1);

drawer.setConnectionStatus('disconnected');
assertState('disconnected', false, 'Disconnected');

drawer.setConnectionStatus(true);
assertState('connected', true, 'Connected');

drawer.setConnectionStatus(false);
assertState('disconnected', false, 'Disconnected');
"""
        self._run_node_script(
            node_script,
            states_decl,
            labels_decl,
            set_status_method,
            failure_context="node transcriptDrawer setConnectionStatus test failed",
        )

        extension_js = (GNOME_EXT_DIR / "extension.js").read_text(encoding="utf-8")
        for state in ("connected", "error", "disconnected"):
            self.assertIn(f"setConnectionStatus('{state}')", extension_js)

    def test_prefs_stt_tts_split_and_provider_model_selector(self) -> None:
        prefs_js = (GNOME_EXT_DIR / "prefs.js").read_text(encoding="utf-8")

        self.assertIn("title: 'STT Controls'", prefs_js)
        self.assertIn("title: 'TTS Controls'", prefs_js)
        self.assertNotIn("title: 'Voice Controls'", prefs_js)

        self.assertIn("Adw.PasswordEntryRow", prefs_js)
        self.assertIn("'/api/v1/secrets'", prefs_js)
        self.assertIn("/api/v1/providers/", prefs_js)
        for secret_key in (
            "STT_API_KEY",
            "KOKORO_API_KEY",
            "ELEVENLABS_API_KEY",
            "GEMINI_API_KEY",
            "OPENAI_API_KEY",
            "ANTHROPIC_API_KEY",
            "CUSTOM_LLM_API_KEY",
        ):
            self.assertIn(secret_key, prefs_js)

        self.assertIn("sttApiUrlRow.set_visible(backend === 'custom_whisper')", prefs_js)
        self.assertIn("sttApiKeyRow.set_visible(backend !== 'local_whisper')", prefs_js)
        self.assertIn("kokoroBaseUrlRow.set_visible(isKokoro)", prefs_js)
        self.assertIn("kokoroApiKeyRow.set_visible(isKokoro)", prefs_js)
        self.assertIn("elevenlabsApiKeyRow.set_visible(ttsProvider === 'elevenlabs')", prefs_js)

        self.assertRegex(prefs_js, r"const\s+modelRow\s*=\s*new\s+Adw\.ComboRow\(")
        self.assertNotRegex(prefs_js, r"new\s+Adw\.EntryRow\(\{\s*title:\s*'Model'")
        self.assertIn("enable_search: true", prefs_js)
        self._assert_prefs_ego_constraints(prefs_js)

        mask_decl = re.search(r"const\s+SECRET_MASK_PLACEHOLDER\s*=\s*[^;]+;", prefs_js)
        self.assertIsNotNone(mask_decl)
        is_masked_fn = self._extract_js_method(prefs_js, "function isMaskedPlaceholder(value) {")
        node_script = """
const assert = require('assert');
const [maskDecl, isMaskedFn] = process.argv.slice(1);
const fn = new Function(`${maskDecl}\n${isMaskedFn}\nreturn isMaskedPlaceholder;`)();
assert.strictEqual(fn('••••••••••••••••'), true);
assert.strictEqual(fn('••••'), true);
assert.strictEqual(fn(''), false);
assert.strictEqual(fn('   '), false);
assert.strictEqual(fn('sk-live-secret'), false);
"""
        self._run_node_script(
            node_script,
            mask_decl.group(0),  # type: ignore[union-attr]
            is_masked_fn,
            failure_context="node prefs.js isMaskedPlaceholder test failed",
        )

    def _assert_prefs_ego_constraints(self, prefs_js: str) -> None:
        """Verify EGO preferences constraints (no shell imports, subprocesses, or GLib.source_remove)."""
        self.assertNotIn("GLib.source_remove(", prefs_js)
        self.assertNotIn("Gio.Subprocess", prefs_js)
        for forbidden_import in (
            "gi://St",
            "gi://Clutter",
            "gi://Meta",
            "gi://Shell",
            "resource:///org/gnome/shell/",
        ):
            self.assertNotIn(forbidden_import, prefs_js)

    def test_prefs_tools_support_and_mcp_configuration(self) -> None:
        prefs_js = (GNOME_EXT_DIR / "prefs.js").read_text(encoding="utf-8")

        for expected in (
            "title: 'Tools Support'",
            "title: 'Force Enable Tools'",
            "title: 'MCP Servers'",
            "title: 'Raw mcpServers JSON'",
            "'/api/v1/mcp/json'",
            "'/api/v1/mcp/test'",
            "Adw.Dialog",
            "Adw.AlertDialog",
            "supports_tools_override",
            "mcpGroup.set_sensitive(active)",
            "mcpJsonGroup.set_sensitive(active)",
        ):
            self.assertIn(expected, prefs_js)

        self._assert_prefs_ego_constraints(prefs_js)

    def test_notch_mode_position_manager_geometry_css_and_guards(self) -> None:
        pm_js = (GNOME_EXT_DIR / "positionManager.js").read_text(encoding="utf-8")
        pm_stripped = re.sub(r"^\s*import\b[^;]+;\s*$", "", pm_js, flags=re.MULTILINE)
        pm_stripped = re.sub(r"^\s*export\s+", "", pm_stripped, flags=re.MULTILINE)
        pm_node = """
const assert = require('assert');
const [pmSrc] = process.argv.slice(1);
const Main = {
    layoutManager: {
        monitors: [
            {x: 0, y: 0, width: 1920, height: 1080},
            {x: 1920, y: 0, width: 2560, height: 1440},
        ],
        primaryIndex: 0,
        getWorkAreaForMonitor: idx => idx === 1
            ? ({x: 1920, y: 28, width: 2560, height: 1412})
            : ({x: 0, y: 32, width: 1920, height: 1048}),
    },
};
const PositionManager = new Function('Main', `${pmSrc}\nreturn PositionManager;`)(Main);
const pm = new PositionManager();

assert.deepStrictEqual(
    pm.computePosition(
        {
            position_mode: 'notch',
            fixed_alignment: 'bottom-right',
            offset_x: 200,
            offset_y: 200,
            float_x: 100,
            float_y: 500,
        },
        140,
        36
    ),
    {x: 890, y: 32}
);
assert.deepStrictEqual(
    pm.computePosition({position_mode: 'Notch', monitor_index: 1, offset_x: 99, offset_y: 99}, 140, 36),
    {x: 3130, y: 28}
);
assert.deepStrictEqual(
    pm.computePosition({position_mode: 'fixed', fixed_alignment: 'top-center', offset_x: 0, offset_y: 12}, 140, 36),
    {x: 890, y: 44}
);
assert.deepStrictEqual(
    pm.computePosition({position_mode: 'floating', float_x: 100, float_y: 500}, 140, 36),
    {x: 100, y: 500}
);
"""
        self._run_node_script(
            pm_node,
            pm_stripped,
            failure_context="node positionManager notch test failed",
        )

        island_js = (GNOME_EXT_DIR / "islandWidget.js").read_text(encoding="utf-8")
        flare_decl = re.search(r"const\s+NOTCH_TOP_FLARE_PX\s*=\s*[^;]+;", island_js)
        bot_r_decl = re.search(r"const\s+NOTCH_BOTTOM_RADIUS_PX\s*=\s*[^;]+;", island_js)
        self.assertIsNotNone(flare_decl)
        self.assertIsNotNone(bot_r_decl)
        compute_geo_fn = self._extract_js_method(
            island_js, "function _computeNotchGeometry(width, height) {"
        )
        metrics_fn = self._extract_js_method(
            island_js, "function _notchBorderMetrics(width, height, inset) {"
        )
        point_fn = self._extract_js_method(
            island_js, "function _notchBorderPoint(s, width, height, inset) {"
        )
        notch_node = """
const assert = require('assert');
const [flareDecl, botRDecl, geoFn, metricsFn, pointFn] = process.argv.slice(1);
const {
    NOTCH_TOP_FLARE_PX,
    NOTCH_BOTTOM_RADIUS_PX,
    _computeNotchGeometry,
    _notchBorderMetrics,
    _notchBorderPoint,
} = new Function(
    `${flareDecl}\n${botRDecl}\n${geoFn}\n${metricsFn}\n${pointFn}\n` +
    `return {NOTCH_TOP_FLARE_PX, NOTCH_BOTTOM_RADIUS_PX, _computeNotchGeometry, _notchBorderMetrics, _notchBorderPoint};`
)();

assert.strictEqual(NOTCH_TOP_FLARE_PX, 12);
assert.strictEqual(NOTCH_BOTTOM_RADIUS_PX, 16);
assert.deepStrictEqual(_computeNotchGeometry(140, 36), {flare: 12, botR: 16, bodyWidth: 116});

const small60 = _computeNotchGeometry(60, 20);
assert.strictEqual(small60.flare, 7);
assert.strictEqual(small60.botR, 13);
assert.strictEqual(small60.bodyWidth, 46);

const tiny10 = _computeNotchGeometry(10, 10);
assert.ok(tiny10.flare >= 4 && tiny10.botR >= 4 && tiny10.bodyWidth >= 4);

const metrics = _notchBorderMetrics(140, 36, 0.75);
assert.ok(metrics.totalLength > 0);
const [startX, startY] = _notchBorderPoint(0, 140, 36, 0.75);
const [endX, endY] = _notchBorderPoint(metrics.totalLength, 140, 36, 0.75);
assert.ok(Math.abs(startY) < 1e-9, `Expected startY ≈ 0, got ${startY}`);
assert.ok(Math.abs(endY) < 1e-9, `Expected endY ≈ 0, got ${endY}`);
assert.ok(Math.abs(endX - (140 - startX)) < 1e-9, `Expected endX ≈ ${140 - startX}, got ${endX}`);
const [midX, midY] = _notchBorderPoint(metrics.totalLength / 2, 140, 36, 0.75);
assert.ok(Math.abs(midX - 70) < 1e-6);
assert.ok(Math.abs(midY - (36 - 0.75)) < 1e-6);
"""
        self._run_node_script(
            notch_node,
            flare_decl.group(0),  # type: ignore[union-attr]
            bot_r_decl.group(0),  # type: ignore[union-attr]
            compute_geo_fn,
            metrics_fn,
            point_fn,
            failure_context="node islandWidget notch geometry test failed",
        )

        self.assertIn("this._bgCanvas = new St.DrawingArea(", island_js)
        self.assertIn("this._bgCanvas.visible = isNotch;", island_js)
        self.assertIn("this._island.add_style_class_name('bytey-island-notch')", island_js)
        self.assertIn("this._island.remove_style_class_name('bytey-island-notch')", island_js)
        self.assertIn("cr.arcNegative(width, flare, flare, -Math.PI / 2, -Math.PI)", island_js)
        self.assertIn("cr.arcNegative(0, flare, flare, 0, -Math.PI / 2)", island_js)

        css_text = (GNOME_EXT_DIR / "stylesheet.css").read_text(encoding="utf-8")
        notch_rule = re.search(r"(?:^|\n)\.bytey-island-notch\s*\{([^}]*)\}", css_text)
        self.assertIsNotNone(notch_rule)
        notch_body = notch_rule.group(1)  # type: ignore[union-attr]
        self.assertIn("background-color: transparent;", notch_body)
        self.assertIn("border-radius: 0px;", notch_body)
        self.assertIn("box-shadow: none;", notch_body)

        notch_hover = re.search(r"\.bytey-island-notch:hover\s*\{([^}]*)\}", css_text)
        self.assertIsNotNone(notch_hover)
        self.assertIn("box-shadow: none;", notch_hover.group(1))  # type: ignore[union-attr]

        state_override_match = re.search(
            r"([^{}]*\.bytey-island-notch\.bytey-island-listening[^{}]*)\{([^}]*)\}",
            css_text,
        )
        self.assertIsNotNone(state_override_match)
        state_selectors = state_override_match.group(1)  # type: ignore[union-attr]
        state_body = state_override_match.group(2)  # type: ignore[union-attr]
        for cls_name in (
            "listening",
            "thinking",
            "tool",
            "speaking",
            "error",
            "dragging",
        ):
            self.assertIn(f".bytey-island-notch.bytey-island-{cls_name}", state_selectors)
        self.assertIn("box-shadow: none;", state_body)

        menu_js = (GNOME_EXT_DIR / "contextMenu.js").read_text(encoding="utf-8")
        self.assertIn("this._floatingItem.setSensitive(!isNotch);", menu_js)
        sync_cfg_method = self._extract_js_method(menu_js, "syncFromConfig(config) {")

        ext_js = (GNOME_EXT_DIR / "extension.js").read_text(encoding="utf-8")
        toggle_floating_method = self._extract_js_method(ext_js, "_toggleFloatingMode(floating) {")
        enable_body = self._extract_js_method(ext_js, "enable() {")
        disable_body = self._extract_js_method(ext_js, "disable() {")
        self.assertIn("global.display.connect('workareas-changed'", enable_body)
        self.assertIn("global.display.disconnect(this._workareasChangedId)", disable_body)

        guard_node = """
const assert = require('assert');
const [syncMethod, toggleMethod] = process.argv.slice(1);
const {syncFromConfig, _toggleFloatingMode} = new Function(
    `return ({ ${syncMethod}, ${toggleMethod} });`
)();

const makeItem = () => ({toggled: false, sensitive: true, setToggleState(v) { this.toggled = v; }, setSensitive(v) { this.sensitive = v; }});
const menu = {
    _destroyed: false,
    _syncing: false,
    _micMuteItem: makeItem(),
    _ttsMuteItem: makeItem(),
    _floatingItem: makeItem(),
    syncFromConfig,
};
menu.syncFromConfig({visuals: {position_mode: 'notch'}});
assert.strictEqual(menu._floatingItem.toggled, false);
assert.strictEqual(menu._floatingItem.sensitive, false);

menu.syncFromConfig({visuals: {position_mode: 'floating'}});
assert.strictEqual(menu._floatingItem.toggled, true);
assert.strictEqual(menu._floatingItem.sensitive, true);

menu.syncFromConfig({visuals: {position_mode: 'fixed'}});
assert.strictEqual(menu._floatingItem.toggled, false);
assert.strictEqual(menu._floatingItem.sensitive, true);

const patches = [];
const ext = {
    _config: {visuals: {position_mode: 'notch'}},
    _islandWidget: {getBounds: () => ({x: 100, y: 200})},
    _patchConfig(p) { patches.push(p); return Promise.resolve(); },
    _toggleFloatingMode,
};
ext._toggleFloatingMode(true);
assert.strictEqual(patches.length, 0);

ext._config.visuals.position_mode = 'fixed';
ext._toggleFloatingMode(true);
assert.deepStrictEqual(patches, [{visuals: {position_mode: 'floating', float_x: 100, float_y: 200}}]);
"""
        self._run_node_script(
            guard_node,
            sync_cfg_method,
            toggle_floating_method,
            failure_context="node contextMenu/extension notch guard test failed",
        )

    def test_prefs_and_window_placement_order_and_mode_controls(self) -> None:
        prefs_js = (GNOME_EXT_DIR / "prefs.js").read_text(encoding="utf-8")
        window_py = (SETTINGS_UI_DIR / "bytey_settings" / "window.py").read_text(encoding="utf-8")

        self._assert_prefs_ego_constraints(prefs_js)

        self.assertIn("makeComboRow('Mode',", prefs_js)
        self.assertNotIn("'Position Mode'", prefs_js)
        self.assertIn('"Mode"', window_py)
        self.assertNotIn('"Position Mode"', window_py)

        self.assertEqual(
            SETTINGS_POSITION_MODE_OPTIONS,
            (("Fixed", "fixed"), ("Floating", "floating"), ("Notch", "notch")),
        )

        # Island Placement appears before Eye Animations / Colors / Chat Drawer in prefs.js
        self.assertLess(
            prefs_js.index("title: 'Island Placement'"),
            prefs_js.index("title: 'Eye Animations'"),
        )
        self.assertLess(
            prefs_js.index("title: 'Eye Animations'"),
            prefs_js.index("title: 'Colors'"),
        )
        self.assertLess(
            prefs_js.index("title: 'Colors'"),
            prefs_js.index("title: 'Chat Drawer'"),
        )
        self.assertLess(
            prefs_js.index("visualsPage.add(placementGroup);"),
            prefs_js.index("visualsPage.add(eyesGroup);"),
        )
        self.assertLess(
            prefs_js.index("visualsPage.add(eyesGroup);"),
            prefs_js.index("visualsPage.add(colorsGroup);"),
        )
        self.assertLess(
            prefs_js.index("visualsPage.add(colorsGroup);"),
            prefs_js.index("visualsPage.add(drawerGroup);"),
        )

        # Island Placement appears before Eye Animations / Colors / Chat Window & Density in window.py
        self.assertLess(
            window_py.index('title="Island Placement"'),
            window_py.index('title="Eye Animations"'),
        )
        self.assertLess(
            window_py.index('title="Eye Animations"'),
            window_py.index('title="Colors"'),
        )
        self.assertLess(
            window_py.index('title="Colors"'),
            window_py.index('title="Chat Window & Density"'),
        )
        self.assertLess(
            window_py.index("page.add(pos_group)"),
            window_py.index("page.add(eye_group)"),
        )
        self.assertLess(
            window_py.index("page.add(eye_group)"),
            window_py.index("page.add(colors_group)"),
        )
        self.assertLess(
            window_py.index("page.add(colors_group)"),
            window_py.index("page.add(chat_group)"),
        )

        # Verify window.py _sync_position_groups disables both fixed_group and floating_group in notch mode
        class _DummyGroup:
            def __init__(self) -> None:
                self.sensitive = True

            def set_sensitive(self, val: bool) -> None:
                self.sensitive = val

        dummy_win = SimpleNamespace(fixed_group=_DummyGroup(), floating_group=_DummyGroup())
        ByteyPreferencesWindow._sync_position_groups(dummy_win, "fixed")  # type: ignore[arg-type]
        self.assertTrue(dummy_win.fixed_group.sensitive)
        self.assertFalse(dummy_win.floating_group.sensitive)

        ByteyPreferencesWindow._sync_position_groups(dummy_win, "floating")  # type: ignore[arg-type]
        self.assertFalse(dummy_win.fixed_group.sensitive)
        self.assertTrue(dummy_win.floating_group.sensitive)

        ByteyPreferencesWindow._sync_position_groups(dummy_win, "notch")  # type: ignore[arg-type]
        self.assertFalse(dummy_win.fixed_group.sensitive)
        self.assertFalse(dummy_win.floating_group.sensitive)

        # Extract prefs.js mode helpers and syncPlacementSensitivity
        opts_match = re.search(
            r"const\s+POSITION_MODE_OPTIONS\s*=\s*\[[\s\S]*?\];",
            prefs_js,
        )
        self.assertIsNotNone(opts_match)
        val_for_idx_fn = self._extract_js_method(
            prefs_js, "function positionModeValueForIndex(idx) {"
        )
        idx_for_val_fn = self._extract_js_method(
            prefs_js, "function positionModeIndexForValue(mode) {"
        )
        sync_placement_fn = self._extract_js_method(
            prefs_js, "const syncPlacementSensitivity = () => {"
        )

        prefs_mode_node = """
const assert = require('assert');
const [optsDecl, valFn, idxFn, syncDecl] = process.argv.slice(1);
const makeCtrl = () => ({sensitive: true, set_sensitive(v) { this.sensitive = v; }});
const posModeRow = {selected: 0, get_selected() { return this.selected; }};
const fixedAlignRow = makeCtrl();
const offsetXRow = makeCtrl();
const offsetYRow = makeCtrl();
const floatXRow = makeCtrl();
const floatYRow = makeCtrl();

const {
    POSITION_MODE_OPTIONS,
    positionModeValueForIndex,
    positionModeIndexForValue,
    syncPlacementSensitivity,
} = new Function(
    'posModeRow', 'fixedAlignRow', 'offsetXRow', 'offsetYRow', 'floatXRow', 'floatYRow',
    `${optsDecl}\n${valFn}\n${idxFn}\n${syncDecl}\n` +
    `return {POSITION_MODE_OPTIONS, positionModeValueForIndex, positionModeIndexForValue, syncPlacementSensitivity};`
)(posModeRow, fixedAlignRow, offsetXRow, offsetYRow, floatXRow, floatYRow);

assert.deepStrictEqual(POSITION_MODE_OPTIONS, [
    {label: 'Fixed', value: 'fixed'},
    {label: 'Floating', value: 'floating'},
    {label: 'Notch', value: 'notch'},
]);
assert.strictEqual(positionModeValueForIndex(0), 'fixed');
assert.strictEqual(positionModeValueForIndex(1), 'floating');
assert.strictEqual(positionModeValueForIndex(2), 'notch');
assert.strictEqual(positionModeValueForIndex(99), 'fixed');

assert.strictEqual(positionModeIndexForValue('fixed'), 0);
assert.strictEqual(positionModeIndexForValue('floating'), 1);
assert.strictEqual(positionModeIndexForValue('notch'), 2);
assert.strictEqual(positionModeIndexForValue('Notch'), 2);
assert.strictEqual(positionModeIndexForValue('invalid'), 0);

posModeRow.selected = 0;
syncPlacementSensitivity();
assert.strictEqual(fixedAlignRow.sensitive, true);
assert.strictEqual(offsetXRow.sensitive, true);
assert.strictEqual(offsetYRow.sensitive, true);
assert.strictEqual(floatXRow.sensitive, false);
assert.strictEqual(floatYRow.sensitive, false);

posModeRow.selected = 1;
syncPlacementSensitivity();
assert.strictEqual(fixedAlignRow.sensitive, false);
assert.strictEqual(offsetXRow.sensitive, false);
assert.strictEqual(offsetYRow.sensitive, false);
assert.strictEqual(floatXRow.sensitive, true);
assert.strictEqual(floatYRow.sensitive, true);

posModeRow.selected = 2;
syncPlacementSensitivity();
assert.strictEqual(fixedAlignRow.sensitive, false);
assert.strictEqual(offsetXRow.sensitive, false);
assert.strictEqual(offsetYRow.sensitive, false);
assert.strictEqual(floatXRow.sensitive, false);
assert.strictEqual(floatYRow.sensitive, false);
"""
        self._run_node_script(
            prefs_mode_node,
            opts_match.group(0),  # type: ignore[union-attr]
            val_for_idx_fn,
            idx_for_val_fn,
            sync_placement_fn,
            failure_context="node prefs.js mode options and sensitivity test failed",
        )

    def test_context_size_presets_clamping_and_ui_placement(self) -> None:
        # Python settings-ui presets & clamping
        self.assertEqual(
            [v for _, v in build_context_size_presets(262144)],
            [None, 4096, 8192, 16384, 32768, 65536, 131072, 262144],
        )
        presets_128k = build_context_size_presets(128000)
        vals_128k = [v for _, v in presets_128k]
        self.assertEqual(vals_128k, [None, 4096, 8192, 16384, 32768, 65536])
        self.assertLessEqual(max(v for v in vals_128k if v is not None), 128000)

        presets_8k = build_context_size_presets(8192)
        self.assertEqual([v for _, v in presets_8k], [None, 4096, 8192])
        self.assertEqual(
            [v for _, v in build_context_size_presets(2048)],
            [None, 512, 1024, 2048],
        )
        self.assertEqual(
            [v for _, v in build_context_size_presets(None)],
            [None, 4096, 8192, 16384, 32768, 65536, 131072],
        )
        self.assertEqual(clamp_context_size(65536, presets_8k), 8192)
        self.assertEqual(clamp_context_size(4096, presets_8k), 4096)
        self.assertIsNone(clamp_context_size(None, presets_8k))

        prefs_js = (GNOME_EXT_DIR / "prefs.js").read_text(encoding="utf-8")
        window_py = (SETTINGS_UI_DIR / "bytey_settings" / "window.py").read_text(encoding="utf-8")
        self._assert_prefs_ego_constraints(prefs_js)

        # Structural placement: Context Size strictly below Model and strictly above Tools Support
        self.assertLess(
            prefs_js.index("title: 'Model'"),
            prefs_js.index("title: 'Context Size'"),
        )
        self.assertLess(
            prefs_js.index("title: 'Context Size'"),
            prefs_js.index("title: 'Tools Support'"),
        )
        self.assertLess(
            prefs_js.index("aiGroup.add(modelRow);"),
            prefs_js.index("aiGroup.add(contextSizeRow);"),
        )
        self.assertLess(
            prefs_js.index("aiGroup.add(contextSizeRow);"),
            prefs_js.index("aiGroup.add(toolsStatusRow);"),
        )

        self.assertLess(
            window_py.index('title="Active Model"'),
            window_py.index('"Context Size"'),
        )
        self.assertLess(
            window_py.index('"Context Size"'),
            window_py.index('title="Tools Support"'),
        )
        self.assertLess(
            window_py.index("ai_group.add(self.model_entry_row)"),
            window_py.index("ai_group.add(self.context_size_row)"),
        )
        self.assertLess(
            window_py.index("ai_group.add(self.context_size_row)"),
            window_py.index("ai_group.add(self.tools_status_row)"),
        )

        fallback_decl = re.search(
            r"const\s+DEFAULT_CONTEXT_FALLBACK_MAX\s*=\s*[^;]+;",
            prefs_js,
        )
        self.assertIsNotNone(fallback_decl)
        fmt_commas_fn = self._extract_js_method(prefs_js, "function formatNumberCommas(n) {")
        fmt_tokens_fn = self._extract_js_method(
            prefs_js, "function formatContextTokens(tokens) {"
        )
        norm_int_fn = self._extract_js_method(prefs_js, "function normalizePositiveInt(value) {")
        build_ctx_fn = self._extract_js_method(
            prefs_js, "function buildContextPresets(maxContext, currentValue = null) {"
        )
        clamp_ctx_fn = self._extract_js_method(
            prefs_js, "function clampContextValue(value, presets) {"
        )

        prefs_ctx_node = """
const assert = require('assert');
const [fallbackDecl, fmtCommas, fmtTokens, normInt, buildFn, clampFn] = process.argv.slice(1);
const {buildContextPresets, clampContextValue} = new Function(
    `${fallbackDecl}\n${fmtCommas}\n${fmtTokens}\n${normInt}\n${buildFn}\n${clampFn}\n` +
    `return {buildContextPresets, clampContextValue};`
)();

assert.deepStrictEqual(
    buildContextPresets(262144).map(o => o.value),
    [null, 4096, 8192, 16384, 32768, 65536, 131072, 262144]
);
const presets128k = buildContextPresets(128000);
const vals128k = presets128k.map(o => o.value);
assert.deepStrictEqual(vals128k, [null, 4096, 8192, 16384, 32768, 65536]);
assert.ok(Math.max(...vals128k.filter(v => v !== null)) <= 128000);

const presets8k = buildContextPresets(8192);
assert.deepStrictEqual(presets8k.map(o => o.value), [null, 4096, 8192]);
assert.deepStrictEqual(buildContextPresets(2048).map(o => o.value), [null, 2048]);
assert.deepStrictEqual(
    buildContextPresets(null).map(o => o.value),
    [null, 4096, 8192, 16384, 32768, 65536, 131072]
);
assert.strictEqual(clampContextValue(65536, presets8k), 8192);
assert.strictEqual(clampContextValue(4096, presets8k), 4096);
assert.strictEqual(clampContextValue(null, presets8k), null);
"""
        self._run_node_script(
            prefs_ctx_node,
            fallback_decl.group(0),  # type: ignore[union-attr]
            fmt_commas_fn,
            fmt_tokens_fn,
            norm_int_fn,
            build_ctx_fn,
            clamp_ctx_fn,
            failure_context="node prefs.js buildContextPresets/clampContextValue test failed",
        )

    def test_piper_ui_parity_and_ego_compliance(self) -> None:
        prefs_js = (GNOME_EXT_DIR / "prefs.js").read_text(encoding="utf-8")
        window_py = (SETTINGS_UI_DIR / "bytey_settings" / "window.py").read_text(encoding="utf-8")

        self._assert_prefs_ego_constraints(prefs_js)

        for expected in (
            "PIPER_VOICE_PRESETS",
            "longHttpSession",
            "'/api/v1/tts/piper/download'",
            "'/api/v1/tts/replay'",
        ):
            self.assertIn(expected, prefs_js)

        tts_decl = re.search(r"const\s+TTS_PROVIDERS\s*=\s*\[[^\]]*\];", prefs_js)
        presets_decl = re.search(r"const\s+PIPER_VOICE_PRESETS\s*=\s*\[[\s\S]*?\];", prefs_js)
        self.assertIsNotNone(tts_decl)
        self.assertIsNotNone(presets_decl)
        combo_text_fn = self._extract_js_method(prefs_js, "function comboText(row) {")
        match_fn = self._extract_js_method(
            prefs_js, "function matchPiperPresetIndex(speed, pitch, noiseScale) {"
        )
        sample_fn = self._extract_js_method(prefs_js, "function sampleTextForLanguage(langCode) {")
        sync_tts_fn = self._extract_js_method(prefs_js, "const syncTtsVisibility = () => {")

        piper_node = """
const assert = require('assert');
const [backendPresetsJson, ttsDecl, presetsDecl, comboFn, matchFn, sampleFn, syncFn] = process.argv.slice(1);
const backendPresets = JSON.parse(backendPresetsJson);

const makeRow = () => ({visible: null, set_visible(v) { this.visible = v; }});
const ttsProviderRow = {selected: 'kokoro', get_selected_item() { return {get_string: () => this.selected}; }};
const kokoroBaseUrlRow = makeRow();
const kokoroApiKeyRow = makeRow();
const elevenlabsApiKeyRow = makeRow();
const piperLangRow = makeRow();
const piperModelRow = makeRow();
const piperCustomModelRow = makeRow();
const piperPresetRow = makeRow();
const piperSpeedRow = makeRow();
const piperPitchRow = makeRow();
const piperNoiseRow = makeRow();
const piperTestRow = makeRow();

const {
    TTS_PROVIDERS,
    PIPER_VOICE_PRESETS,
    matchPiperPresetIndex,
    sampleTextForLanguage,
    syncTtsVisibility,
} = new Function(
    'ttsProviderRow', 'kokoroBaseUrlRow', 'kokoroApiKeyRow', 'elevenlabsApiKeyRow',
    'piperLangRow', 'piperModelRow', 'piperCustomModelRow', 'piperPresetRow',
    'piperSpeedRow', 'piperPitchRow', 'piperNoiseRow', 'piperTestRow',
    `${ttsDecl}\n${presetsDecl}\n${comboFn}\n${matchFn}\n${sampleFn}\n${syncFn}\n` +
    `return {TTS_PROVIDERS, PIPER_VOICE_PRESETS, matchPiperPresetIndex, sampleTextForLanguage, syncTtsVisibility};`
)(
    ttsProviderRow, kokoroBaseUrlRow, kokoroApiKeyRow, elevenlabsApiKeyRow,
    piperLangRow, piperModelRow, piperCustomModelRow, piperPresetRow,
    piperSpeedRow, piperPitchRow, piperNoiseRow, piperTestRow
);

assert.deepStrictEqual(TTS_PROVIDERS, ['kokoro', 'piper', 'elevenlabs', 'disabled']);
assert.strictEqual(PIPER_VOICE_PRESETS[PIPER_VOICE_PRESETS.length - 1].id, 'custom');
for (const [key, spec] of Object.entries(backendPresets)) {
    const found = PIPER_VOICE_PRESETS.find(p => p.id === key);
    assert.ok(found, `Missing preset ${key}`);
    assert.strictEqual(found.label, spec.label);
    assert.strictEqual(found.speed, spec.speed);
    assert.strictEqual(found.pitch, spec.pitch);
    assert.strictEqual(found.noise_scale, spec.noise_scale);
}

assert.strictEqual(matchPiperPresetIndex(1.0, 0.0, 0.667), 0);
assert.strictEqual(matchPiperPresetIndex(1.0, 0.0, 0.67), 0);
assert.strictEqual(matchPiperPresetIndex(1.25, 8.0, 0.8), 1);
assert.strictEqual(matchPiperPresetIndex(1.15, 10.5, 0.9), 2);
assert.strictEqual(matchPiperPresetIndex(0.82, -8.0, 0.667), 3);
assert.strictEqual(matchPiperPresetIndex(0.68, -5.5, 0.55), 4);
assert.strictEqual(matchPiperPresetIndex(1.55, 5.5, 1.0), 5);
assert.strictEqual(matchPiperPresetIndex(1.05, -2.5, 0.1), 6);
assert.strictEqual(matchPiperPresetIndex(1.13, 3.0, 0.5), 7);
assert.strictEqual(matchPiperPresetIndex(NaN, 0.0, 0.667), 7);

assert.ok(sampleTextForLanguage('en_US').includes('Bytey'));
assert.ok(sampleTextForLanguage('pt-BR').includes('Olá'));
assert.ok(sampleTextForLanguage('fr_FR').includes('Bonjour'));
assert.strictEqual(sampleTextForLanguage('xx_YY'), sampleTextForLanguage('en'));

const piperRows = [
    piperLangRow, piperModelRow, piperCustomModelRow, piperPresetRow,
    piperSpeedRow, piperPitchRow, piperNoiseRow, piperTestRow,
];

ttsProviderRow.selected = 'piper';
syncTtsVisibility();
for (const r of piperRows) assert.strictEqual(r.visible, true);
assert.strictEqual(kokoroBaseUrlRow.visible, false);
assert.strictEqual(kokoroApiKeyRow.visible, false);
assert.strictEqual(elevenlabsApiKeyRow.visible, false);

ttsProviderRow.selected = 'kokoro';
syncTtsVisibility();
for (const r of piperRows) assert.strictEqual(r.visible, false);
assert.strictEqual(kokoroBaseUrlRow.visible, true);
assert.strictEqual(kokoroApiKeyRow.visible, true);
assert.strictEqual(elevenlabsApiKeyRow.visible, false);

ttsProviderRow.selected = 'elevenlabs';
syncTtsVisibility();
for (const r of piperRows) assert.strictEqual(r.visible, false);
assert.strictEqual(kokoroBaseUrlRow.visible, false);
assert.strictEqual(kokoroApiKeyRow.visible, false);
assert.strictEqual(elevenlabsApiKeyRow.visible, true);

ttsProviderRow.selected = 'disabled';
syncTtsVisibility();
for (const r of piperRows) assert.strictEqual(r.visible, false);
assert.strictEqual(kokoroBaseUrlRow.visible, false);
assert.strictEqual(kokoroApiKeyRow.visible, false);
assert.strictEqual(elevenlabsApiKeyRow.visible, false);
"""
        self._run_node_script(
            piper_node,
            json.dumps(PIPER_VOICE_PRESETS),
            tts_decl.group(0),  # type: ignore[union-attr]
            presets_decl.group(0),  # type: ignore[union-attr]
            combo_text_fn,
            match_fn,
            sample_fn,
            sync_tts_fn,
            failure_context="node prefs.js Piper presets/visibility test failed",
        )

        # settings-ui api_client.py & window.py parity
        self.assertIn("piper", SETTINGS_TTS_PROVIDERS)
        self.assertEqual(SETTINGS_TTS_PROVIDERS, ("kokoro", "piper", "elevenlabs", "disabled"))
        vo_defaults = SETTINGS_DEFAULT_CONFIG["voice_output"]
        self.assertEqual(vo_defaults["piper_model"], "en_US-lessac-medium")
        self.assertEqual(vo_defaults["piper_models_dir"], "")
        self.assertEqual(vo_defaults["piper_speed"], 1.0)
        self.assertEqual(vo_defaults["piper_pitch"], 0.0)
        self.assertEqual(vo_defaults["piper_noise_scale"], 0.667)
        self.assertIsNone(vo_defaults["piper_speaker_id"])

        self.assertEqual(SETTINGS_PIPER_VOICE_PRESETS[-1][0], "custom")
        for idx, (pid, spec) in enumerate(PIPER_VOICE_PRESETS.items()):
            w_pid, w_lbl, w_spd, w_pitch, w_noise = SETTINGS_PIPER_VOICE_PRESETS[idx]
            self.assertEqual(w_pid, pid)
            self.assertEqual(w_lbl, spec["label"])
            self.assertEqual(w_spd, spec["speed"])
            self.assertEqual(w_pitch, spec["pitch"])
            self.assertEqual(w_noise, spec["noise_scale"])

        self.assertEqual(_match_piper_preset_index(1.0, 0.0, 0.667), 0)
        self.assertEqual(_match_piper_preset_index(1.0, 0.0, 0.67), 0)
        self.assertEqual(_match_piper_preset_index(1.25, 8.0, 0.8), 1)
        self.assertEqual(_match_piper_preset_index(1.05, -2.5, 0.1), 6)
        self.assertEqual(_match_piper_preset_index(1.13, 3.0, 0.5), 7)
        self.assertIn("Olá", _piper_sample_text("pt-BR"))
        self.assertEqual(_piper_sample_text("xx_YY"), _piper_sample_text("en"))

        for token in (
            '"Piper Speed"',
            '"Piper Pitch"',
            '"Piper Expressiveness"',
            "self.piper_speed_scale",
            "self.piper_pitch_scale",
            "self.piper_noise_scale",
            "download_piper_model",
            "replay_tts",
        ):
            self.assertIn(token, window_py)

        # Verify DaemonClient.download_piper_model & replay_tts via MockTransport
        seen_api_calls: list[tuple[str, str, dict[str, Any]]] = []

        def _client_handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content.decode("utf-8")) if request.content else {}
            seen_api_calls.append((request.method, str(request.url), body))
            if request.url.path == "/api/v1/tts/piper/download":
                return httpx.Response(200, json={"ok": True, "installed": True, "model": body.get("model")})
            if request.url.path == "/api/v1/tts/replay":
                return httpx.Response(200, json={"ok": True})
            if request.url.path == "/api/v1/tts/voices":
                return httpx.Response(200, json={"voices": [{"id": "en_US-lessac-medium", "installed": False}]})
            return httpx.Response(404, json={"error": "Not found"})

        with tempfile.TemporaryDirectory() as tmp:
            dc = DaemonClient(base_url="http://127.0.0.1:8765", config_path=Path(tmp) / "cfg.json")
            dc._client.close()
            dc._client = httpx.Client(
                base_url="http://127.0.0.1:8765",
                transport=httpx.MockTransport(_client_handler),
            )
            try:
                empty_dl = dc.download_piper_model("   ")
                self.assertFalse(empty_dl.ok)

                voices_res = dc.list_tts_voices("piper", refresh=True, language="pt")
                self.assertTrue(voices_res.ok)
                self.assertIn("voices", voices_res.data)

                dl_res = dc.download_piper_model("pt_PT-tugão-medium")
                self.assertTrue(dl_res.ok)
                self.assertTrue(dl_res.data["installed"])

                rep_res = dc.replay_tts("Olá!")
                self.assertTrue(rep_res.ok)
            finally:
                dc.close()

        self.assertEqual(len(seen_api_calls), 3)
        self.assertIn("provider=piper", seen_api_calls[0][1])
        self.assertIn("refresh=1", seen_api_calls[0][1])
        self.assertIn("language=pt", seen_api_calls[0][1])
        self.assertEqual(seen_api_calls[1][2], {"model": "pt_PT-tugão-medium"})
        self.assertEqual(seen_api_calls[2][2], {"text": "Olá!"})

    def test_builtin_mcp_host_top_entry_in_settings(self) -> None:
        ext_dir = Path(__file__).resolve().parent.parent / "gnome-extension" / "bytey-ai-companion@c42759.github.com"
        prefs_js = (ext_dir / "prefs.js").read_text(encoding="utf-8")
        window_py = (SETTINGS_UI_DIR / "bytey_settings" / "window.py").read_text(encoding="utf-8")

        # prefs.js: Bytey MCP Host is added at the top of rebuildMcpList before empty-state return
        prefs_start = prefs_js.index("const rebuildMcpList = () => {")
        prefs_end = prefs_js.index("const pushMcpChanges =", prefs_start)
        prefs_block = prefs_js[prefs_start:prefs_end]
        self.assertIn("title: 'Bytey MCP Host'", prefs_block)
        self.assertIn("active: true", prefs_block)
        self.assertIn("sensitive: false", prefs_block)
        self.assertLess(
            prefs_block.index("title: 'Bytey MCP Host'"),
            prefs_block.index("No external MCP servers configured"),
        )

        # window.py: Bytey MCP Host is added at the top of _rebuild_mcp_list before empty-state return
        win_start = window_py.index("def _rebuild_mcp_list(self) -> None:")
        win_end = window_py.index("def _sync_mcp_json_text(self) -> None:", win_start)
        win_block = window_py[win_start:win_end]
        self.assertIn('title="Bytey MCP Host"', win_block)
        self.assertIn("active=True", win_block)
        self.assertIn("sensitive=False", win_block)
        self.assertLess(
            win_block.index('title="Bytey MCP Host"'),
            win_block.index("No external MCP servers configured"),
        )

    def test_max_tool_rounds_ui_parity(self) -> None:
        from bytey_settings.api_client import DEFAULT_CONFIG

        self.assertEqual(DEFAULT_CONFIG["llm"]["max_tool_rounds"], 25)
        prefs_js = (
            REPO_ROOT / "gnome-extension" / "bytey-ai-companion@c42759.github.com" / "prefs.js"
        ).read_text(encoding="utf-8")
        window_py = (REPO_ROOT / "settings-ui" / "bytey_settings" / "window.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("title: 'Max Tool Loops'", prefs_js)
        self.assertIn("max_tool_rounds", prefs_js)
        self.assertIn("self.max_tool_rounds_row", window_py)
        self.assertIn("llm_max_tool_rounds", window_py)

    def test_mcp_info_icon_and_dialog_parity(self) -> None:
        ext_dir = Path(__file__).resolve().parent.parent / "gnome-extension" / "bytey-ai-companion@c42759.github.com"
        prefs_js = (ext_dir / "prefs.js").read_text(encoding="utf-8")
        window_py = (SETTINGS_UI_DIR / "bytey_settings" / "window.py").read_text(encoding="utf-8")

        self.assertGreaterEqual(prefs_js.count("dialog-information-symbolic"), 2)
        self.assertIn("showMcpInfoDialog(null)", prefs_js)
        self.assertIn("showMcpInfoDialog(name)", prefs_js)
        self.assertIn("await httpJson('GET', '/api/v1/mcp/host')", prefs_js)

        self.assertGreaterEqual(window_py.count("dialog-information-symbolic"), 2)
        self.assertIn("self._open_mcp_info_dialog(None)", window_py)
        self.assertIn("self._open_mcp_info_dialog(n)", window_py)
        self.assertIn("self.client.get_mcp_host_info", window_py)

        def _handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/v1/mcp/host":
                return httpx.Response(
                    200,
                    json={
                        "ok": True,
                        "title": "Bytey MCP Host",
                        "description": "Built-in MCP host",
                        "tools": [{"name": "bytey_workspace__read_file", "description": "Read a file"}],
                    },
                )
            return httpx.Response(404, json={"error": "not found"})

        with tempfile.TemporaryDirectory() as tmp:
            dc = DaemonClient(base_url="http://127.0.0.1:8765", config_path=Path(tmp) / "cfg.json")
            dc._client.close()
            dc._client = httpx.Client(base_url="http://127.0.0.1:8765", transport=httpx.MockTransport(_handler))
            try:
                res = dc.get_mcp_host_info()
                self.assertTrue(res.ok)
                self.assertEqual(res.data["title"], "Bytey MCP Host")
                self.assertEqual(res.data["tools"][0]["name"], "bytey_workspace__read_file")
            finally:
                dc.close()


class TestSTTBackendPrecedence(unittest.IsolatedAsyncioTestCase):
    """Test openai_whisper fixed endpoint URL and STT_API_KEY precedence."""

    async def test_openai_whisper_fixed_url_and_stt_key_precedence(self) -> None:
        seen_requests: list[tuple[str, str]] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            seen_requests.append(
                (str(request.url), request.headers.get("authorization", ""))
            )
            text = (
                "hello openai"
                if str(request.url) == DEFAULT_OPENAI_STT_URL
                else "hello custom"
            )
            return httpx.Response(200, json={"text": text})

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            engine = STTEngine(http_client=client)
            pcm = b"\x01\x02" * 160

            openai_text = await engine.transcribe(
                pcm,
                backend="openai_whisper",
                model="whisper-1",
                api_url="https://stale-custom.example.com/v1/audio/transcriptions",
                api_key="stt-key-1",
            )
            custom_text = await engine.transcribe(
                pcm,
                backend="custom_whisper",
                model="whisper-1",
                api_url="https://custom.example.com/v1/audio/transcriptions",
                api_key="stt-key-2",
            )

        self.assertEqual(openai_text, "hello openai")
        self.assertEqual(custom_text, "hello custom")
        self.assertEqual(
            seen_requests,
            [
                (DEFAULT_OPENAI_STT_URL, "Bearer stt-key-1"),
                ("https://custom.example.com/v1/audio/transcriptions", "Bearer stt-key-2"),
            ],
        )

        secrets_both = {"OPENAI_API_KEY": "oa-key", "STT_API_KEY": "stt-key"}
        svc_both = VoiceInputService(
            get_config=lambda: {},
            get_secret=secrets_both.get,
            stt_engine=STTEngine(),
        )
        self.assertEqual(await svc_both._resolve_stt_secret("openai_whisper"), "stt-key")
        self.assertEqual(await svc_both._resolve_stt_secret("custom_whisper"), "stt-key")

        secrets_fallback = {"OPENAI_API_KEY": "oa-key"}
        svc_fallback = VoiceInputService(
            get_config=lambda: {},
            get_secret=secrets_fallback.get,
            stt_engine=STTEngine(),
        )
        self.assertEqual(await svc_fallback._resolve_stt_secret("openai_whisper"), "oa-key")


def _make_sine_wav_bytes(
    duration_s: float = 0.4,
    sample_rate: int = 22050,
    freq_hz: float = 440.0,
    amplitude: int = 12000,
) -> bytes:
    """Build deterministic mono 16-bit PCM WAV bytes containing a pure sine tone."""
    num_samples = int(round(duration_s * sample_rate))
    t = np.arange(num_samples, dtype=np.float64) / float(sample_rate)
    pcm = np.rint(amplitude * np.sin(2.0 * np.pi * freq_hz * t)).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm.tobytes())
    return buf.getvalue()


def _inspect_wav_bytes(wav_bytes: bytes) -> tuple[int, int, int, int, float]:
    """Return (channels, sampwidth, sample_rate, nframes, rms) for a 16-bit WAV payload."""
    with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
        channels = wf.getnchannels()
        sampwidth = wf.getsampwidth()
        sample_rate = wf.getframerate()
        nframes = wf.getnframes()
        raw = wf.readframes(nframes)
    samples = np.frombuffer(raw, dtype=np.int16).astype(np.float64) / 32768.0
    rms = float(np.sqrt(np.mean(samples * samples))) if samples.size > 0 else 0.0
    return channels, sampwidth, sample_rate, nframes, rms


class TestPiperTTS(unittest.IsolatedAsyncioTestCase):
    """Tests for Piper WAV pitch shifting, discovery, download, 3-tier synthesis, and player dispatch."""

    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp_dir.name)
        self.fake_home = self.root / "home"
        self.fake_home.mkdir(parents=True, exist_ok=True)
        self.models_dir = self.root / "piper_models"
        self.models_dir.mkdir(parents=True, exist_ok=True)
        self._home_patcher = patch("pathlib.Path.home", return_value=self.fake_home)
        self._home_patcher.start()

    def tearDown(self) -> None:
        self._home_patcher.stop()
        self.tmp_dir.cleanup()

    @staticmethod
    def _write_fake_model(
        target_dir: Path,
        model_id: str,
        *,
        language_code: str = "en_US",
        num_speakers: int = 1,
        sample_rate: int = 22050,
        noise_w: float = 0.8,
        phoneme_id_map: dict[str, list[int]] | None = None,
        espeak_voice: str = "en-us",
    ) -> tuple[Path, Path]:
        target_dir.mkdir(parents=True, exist_ok=True)
        onnx_path = target_dir / f"{model_id}.onnx"
        json_path = target_dir / f"{model_id}.onnx.json"
        onnx_path.write_bytes(b"FAKE_ONNX_WEIGHTS_" + model_id.encode("utf-8"))
        cfg = {
            "audio": {"sample_rate": sample_rate, "quality": "medium"},
            "espeak": {"voice": espeak_voice},
            "inference": {"noise_scale": 0.667, "length_scale": 1.0, "noise_w": noise_w},
            "language": {"code": language_code},
            "num_speakers": num_speakers,
            "phoneme_type": "espeak",
            "phoneme_id_map": phoneme_id_map
            or {
                "^": [1],
                "$": [2],
                "_": [0],
                " ": [3],
                "o": [10],
                "l": [11],
                "a": [12],
                ".": [13],
            },
        }
        json_path.write_text(json.dumps(cfg), encoding="utf-8")
        return onnx_path, json_path

    async def test_apply_pitch_shift_wav_fast_path_ffmpeg_and_numpy_fallback(self) -> None:
        wav_in = _make_sine_wav_bytes(duration_s=0.4, sample_rate=22050, freq_hz=440.0)
        _, _, _, n_in, rms_in = _inspect_wav_bytes(wav_in)
        self.assertGreater(rms_in, 0.1)

        # Fast path (< 0.05 semitones) returns exact identical bytes
        for zeroish in (0.0, 0.04, -0.04):
            out = await apply_pitch_shift_wav(wav_in, zeroish)
            self.assertIs(out, wav_in)

        # Verify atempo decomposition stays within ffmpeg's [0.5, 2.0] range
        for factor in (0.25, 0.5, 1.0, 2.0, 3.0):
            stages = _build_atempo_chain(factor)
            self.assertGreaterEqual(len(stages), 1)
            for stage in stages:
                val = float(stage.split("=", 1)[1])
                self.assertGreaterEqual(val, 0.5 - 1e-6)
                self.assertLessEqual(val, 2.0 + 1e-6)

        # Real /usr/bin/ffmpeg path (+7.0 and -7.0 semitones)
        self.assertTrue(os.path.isfile("/usr/bin/ffmpeg"))
        for semitones in (7.0, -7.0):
            with self.subTest(backend="ffmpeg", semitones=semitones):
                shifted = await apply_pitch_shift_wav(
                    wav_in, semitones, ffmpeg_bin="/usr/bin/ffmpeg"
                )
                self.assertTrue(shifted.startswith(b"RIFF"))
                self.assertNotEqual(shifted, wav_in)
                ch, sw, sr, nframes, rms = _inspect_wav_bytes(shifted)
                self.assertEqual(ch, 1)
                self.assertEqual(sw, 2)
                self.assertEqual(sr, 22050)
                self.assertGreater(nframes, 0)
                self.assertGreater(rms, 0.05)
                duration_s = nframes / float(sr)
                self.assertAlmostEqual(duration_s, 0.4, delta=0.08)

        # Pure-numpy fallback path
        up_np = await apply_pitch_shift_wav(wav_in, 7.0, ffmpeg_bin="/nonexistent/ffmpeg")
        down_np = await apply_pitch_shift_wav(wav_in, -7.0, ffmpeg_bin="/nonexistent/ffmpeg")

        for label, payload in (("up", up_np), ("down", down_np)):
            with self.subTest(backend="numpy", direction=label):
                self.assertTrue(payload.startswith(b"RIFF"))
                ch, sw, sr, nframes, rms = _inspect_wav_bytes(payload)
                self.assertEqual(ch, 1)
                self.assertEqual(sw, 2)
                self.assertEqual(sr, 22050)
                self.assertGreater(nframes, 0)
                self.assertGreater(rms, 0.05)

        _, _, _, n_up, _ = _inspect_wav_bytes(up_np)
        _, _, _, n_down, _ = _inspect_wav_bytes(down_np)
        self.assertEqual(n_up, int(round(n_in / (2.0 ** (7.0 / 12.0)))))
        self.assertEqual(n_down, int(round(n_in / (2.0 ** (-7.0 / 12.0)))))
        self.assertLess(n_up, n_in)
        self.assertGreater(n_down, n_in)

    async def test_piper_scan_local_models_catalog_and_language_filter(self) -> None:
        self._write_fake_model(self.models_dir, "en_US-lessac-medium", language_code="en_US")
        self._write_fake_model(
            self.models_dir / "nested",
            "fr_FR-custom-low",
            language_code="fr_FR",
            num_speakers=2,
            sample_rate=16000,
        )
        # Orphan .onnx without JSON and broken JSON should be ignored cleanly
        (self.models_dir / "orphan-broken.onnx").write_bytes(b"NO_JSON")
        (self.models_dir / "badjson.onnx").write_bytes(b"BAD_JSON")
        (self.models_dir / "badjson.onnx.json").write_text("{not valid json", encoding="utf-8")

        def _forbid_network(_request: httpx.Request) -> httpx.Response:
            raise AssertionError("Unexpected HTTP request during refresh=False catalog listing")

        async with httpx.AsyncClient(transport=httpx.MockTransport(_forbid_network)) as http:
            client = PiperTTSClient(models_dir=self.models_dir, http_client=http)
            local = client.scan_local_models()
            self.assertEqual(set(local.keys()), {"en_US-lessac-medium", "fr_FR-custom-low"})
            self.assertTrue(local["en_US-lessac-medium"]["installed"])
            self.assertEqual(local["fr_FR-custom-low"]["num_speakers"], 2)
            self.assertEqual(local["fr_FR-custom-low"]["sample_rate"], 16000)

            voices = await client.list_voices(refresh=False)
            by_id = {v["id"]: v for v in voices}
            self.assertGreaterEqual(len(voices), len(CURATED_PIPER_VOICES))
            self.assertTrue(by_id["en_US-lessac-medium"]["installed"])
            self.assertTrue(by_id["en_US-lessac-medium"]["path"])
            self.assertTrue(by_id["fr_FR-custom-low"]["installed"])
            self.assertFalse(by_id["pt_PT-tugão-medium"]["installed"])
            self.assertFalse(by_id["pt_BR-faber-medium"]["installed"])

            pt_voices = await client.list_voices(refresh=False, language="pt")
            pt_ids = {v["id"] for v in pt_voices}
            self.assertEqual(pt_ids, {"pt_PT-tugão-medium", "pt_BR-faber-medium"})

            pt_pt_voices = await client.list_voices(refresh=False, language="pt-PT")
            self.assertEqual([v["id"] for v in pt_pt_voices], ["pt_PT-tugão-medium"])

    async def test_piper_download_model_validation_atomic_and_unicode_encoding(self) -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _r: httpx.Response(500))
        ) as http:
            client = PiperTTSClient(models_dir=self.models_dir, http_client=http)
            for bad_id in ("../evil", "dir/model", "dir\\model", "", "   ", "bad\x00id"):
                with self.subTest(bad_id=bad_id), self.assertRaises(ValueError):
                    await client.download_model(bad_id)

        requested_urls: list[str] = []
        fake_cfg_bytes = json.dumps(
            {
                "audio": {"sample_rate": 22050, "quality": "medium"},
                "language": {"code": "pt_PT"},
                "num_speakers": 1,
            }
        ).encode("utf-8")
        fake_onnx_bytes = b"MOCK_ONNX_TUGAO_BYTES_123456"

        def _download_handler(request: httpx.Request) -> httpx.Response:
            raw_url = str(request.url)
            requested_urls.append(raw_url)
            if raw_url.endswith(".onnx.json"):
                return httpx.Response(200, content=fake_cfg_bytes)
            if raw_url.endswith(".onnx"):
                return httpx.Response(200, content=fake_onnx_bytes)
            return httpx.Response(404)

        async with httpx.AsyncClient(transport=httpx.MockTransport(_download_handler)) as http:
            client = PiperTTSClient(models_dir=self.models_dir, http_client=http)
            res = await client.download_model("pt_PT-tugão-medium")
            self.assertTrue(res["ok"])
            self.assertTrue(res["installed"])
            self.assertEqual(res["model"], "pt_PT-tugão-medium")

            onnx_file = self.models_dir / "pt_PT-tugão-medium.onnx"
            json_file = self.models_dir / "pt_PT-tugão-medium.onnx.json"
            self.assertTrue(onnx_file.is_file())
            self.assertTrue(json_file.is_file())
            self.assertEqual(onnx_file.read_bytes(), fake_onnx_bytes)
            self.assertEqual(json_file.read_bytes(), fake_cfg_bytes)
            self.assertEqual(list(self.models_dir.glob("*.tmp")), [])

            self.assertEqual(len(requested_urls), 2)
            self.assertIn("pt/pt_PT/tug%C3%A3o/medium/pt_PT-tug%C3%A3o-medium.onnx.json", requested_urls[0])
            self.assertIn("pt/pt_PT/tug%C3%A3o/medium/pt_PT-tug%C3%A3o-medium.onnx", requested_urls[1])

            # Already-installed check short-circuits without extra HTTP requests
            res2 = await client.download_model("pt_PT-tugão-medium")
            self.assertTrue(res2["installed"])
            self.assertEqual(len(requested_urls), 2)

            voices = await client.list_voices(refresh=False, language="pt_PT")
            self.assertTrue(voices[0]["installed"])

        # Failure mid-download cleans up temporary files without leaving partial install
        def _fail_onnx_handler(request: httpx.Request) -> httpx.Response:
            if str(request.url).endswith(".onnx.json"):
                return httpx.Response(200, content=fake_cfg_bytes)
            return httpx.Response(404)

        async with httpx.AsyncClient(transport=httpx.MockTransport(_fail_onnx_handler)) as http:
            client = PiperTTSClient(models_dir=self.models_dir, http_client=http)
            with self.assertRaises(httpx.HTTPStatusError):
                await client.download_model("en_US-amy-low")
            self.assertFalse((self.models_dir / "en_US-amy-low.onnx").exists())
            self.assertFalse((self.models_dir / "en_US-amy-low.onnx.json").exists())
            self.assertEqual(list(self.models_dir.glob("*.tmp")), [])

    async def test_piper_tier3_onnx_espeak_synthesis(self) -> None:
        self._write_fake_model(
            self.models_dir,
            "es_ES-sharvard-medium",
            language_code="es_ES",
            num_speakers=3,
            sample_rate=22050,
            noise_w=0.75,
            espeak_voice="es",
        )

        captured_runs: list[dict[str, Any]] = []
        t = np.arange(4410, dtype=np.float32) / 22050.0
        synth_wave = (0.5 * np.sin(2.0 * np.pi * 440.0 * t)).astype(np.float32)

        class _FakeInferenceSession:
            def __init__(self, model_path: str, **_kwargs: Any) -> None:
                self.model_path = model_path

            def run(self, _outputs: Any, inputs: dict[str, Any]) -> list[np.ndarray]:
                captured_runs.append(inputs)
                return [synth_wave[None, :]]

        class _FakeEspeakProc:
            returncode = 0

            async def communicate(self, _data: bytes | None = None) -> tuple[bytes, bytes]:
                return b"ola\n", b""

        async def _fake_subproc(*args: Any, **_kwargs: Any) -> _FakeEspeakProc:
            self.assertIn("espeak-ng", str(args[0]))
            return _FakeEspeakProc()

        client = PiperTTSClient(models_dir=self.models_dir)
        try:
            with (
                patch("bytey_ai_companion.tts.piper_provider._import_piper_voice", return_value=None),
                patch("bytey_ai_companion.tts.piper_provider.shutil.which", return_value=None),
                patch("bytey_ai_companion.tts.piper_provider._has_onnxruntime", return_value=True),
                patch(
                    "bytey_ai_companion.tts.piper_provider._find_espeak_ng",
                    return_value="/usr/bin/espeak-ng",
                ),
                patch("onnxruntime.InferenceSession", _FakeInferenceSession),
                patch(
                    "bytey_ai_companion.tts.piper_provider.asyncio.create_subprocess_exec",
                    side_effect=_fake_subproc,
                ),
                patch(
                    "bytey_ai_companion.tts.piper_provider.apply_pitch_shift_wav",
                    wraps=apply_pitch_shift_wav,
                ) as pitch_spy,
            ):
                self.assertEqual(client.detect_backend(), "onnx_espeak")
                wav_bytes, fmt = await client.synthesize(
                    "Hola.",
                    model="es_ES-sharvard-medium",
                    speed=1.25,
                    pitch=5.0,
                    noise_scale=0.55,
                    speaker_id=2,
                    auto_download=False,
                )
                self.assertEqual(fmt, "wav")
                self.assertTrue(wav_bytes.startswith(b"RIFF"))
                ch, sw, sr, nframes, rms = _inspect_wav_bytes(wav_bytes)
                self.assertEqual((ch, sw, sr), (1, 2, 22050))
                self.assertGreater(nframes, 0)
                self.assertGreater(rms, 0.1)
                self.assertEqual(pitch_spy.call_count, 1)
                self.assertEqual(pitch_spy.call_args.args[1], 5.0)

                # Verify speaker clamping & default speaker ID on multi-speaker model
                await client.synthesize(
                    "Hola.",
                    model="es_ES-sharvard-medium",
                    speed=1.0,
                    pitch=0.0,
                    noise_scale=0.667,
                    speaker_id=99,
                    auto_download=False,
                )
                await client.synthesize(
                    "Hola.",
                    model="es_ES-sharvard-medium",
                    speed=1.0,
                    pitch=0.0,
                    noise_scale=0.667,
                    speaker_id=None,
                    auto_download=False,
                )
        finally:
            await client.close()

        self.assertEqual(len(captured_runs), 3)
        first = captured_runs[0]
        np.testing.assert_allclose(
            first["scales"],
            np.array([0.55, round(1.0 / 1.25, 4), 0.75], dtype=np.float32),
            atol=1e-6,
        )
        np.testing.assert_array_equal(first["sid"], np.array([2], dtype=np.int64))
        self.assertEqual(int(first["input"][0, 0]), 1)
        self.assertEqual(int(first["input"][0, -1]), 2)
        self.assertEqual(int(first["input_lengths"][0]), int(first["input"].shape[1]))
        np.testing.assert_array_equal(captured_runs[1]["sid"], np.array([2], dtype=np.int64))
        np.testing.assert_array_equal(captured_runs[2]["sid"], np.array([0], dtype=np.int64))

    async def test_piper_tier1_python_and_tier2_cli_synthesis(self) -> None:
        self._write_fake_model(
            self.models_dir,
            "en_US-lessac-medium",
            language_code="en_US",
            num_speakers=2,
            sample_rate=22050,
            noise_w=0.8,
        )
        sample_wav = _make_sine_wav_bytes(duration_s=0.25, sample_rate=22050)
        with wave.open(io.BytesIO(sample_wav), "rb") as wf_in:
            sample_pcm = wf_in.readframes(wf_in.getnframes())

        # Tier 1: piper_python
        recorded_py: dict[str, Any] = {}

        class _FakeSynthesisConfig:
            def __init__(
                self,
                speaker_id: int | None = None,
                length_scale: float = 1.0,
                noise_scale: float = 0.667,
                noise_w: float = 0.8,
            ) -> None:
                self.speaker_id = speaker_id
                self.length_scale = length_scale
                self.noise_scale = noise_scale
                self.noise_w = noise_w

        class _FakePiperVoice:
            @classmethod
            def load(cls, model_path: str, config_path: str | None = None) -> _FakePiperVoice:
                recorded_py["load"] = (model_path, config_path)
                return cls()

            def synthesize_wav(
                self,
                text: str,
                wav_file: wave.Wave_write,
                syn_config: _FakeSynthesisConfig | None = None,
            ) -> None:
                recorded_py["text"] = text
                recorded_py["syn_config"] = syn_config
                wav_file.setnchannels(1)
                wav_file.setsampwidth(2)
                wav_file.setframerate(22050)
                wav_file.writeframes(sample_pcm)

        fake_piper_mod = SimpleNamespace(
            PiperVoice=_FakePiperVoice,
            SynthesisConfig=_FakeSynthesisConfig,
        )

        client1 = PiperTTSClient(models_dir=self.models_dir)
        try:
            with patch.dict(sys.modules, {"piper": fake_piper_mod}):
                self.assertEqual(client1.detect_backend(), "piper_python")
                wav1, fmt1 = await client1.synthesize(
                    "Hello Piper Python",
                    model="en_US-lessac-medium",
                    speed=2.0,
                    pitch=0.0,
                    noise_scale=0.5,
                    speaker_id=1,
                    auto_download=False,
                )
        finally:
            await client1.close()

        self.assertEqual(fmt1, "wav")
        self.assertTrue(wav1.startswith(b"RIFF"))
        _, _, sr1, nf1, rms1 = _inspect_wav_bytes(wav1)
        self.assertEqual(sr1, 22050)
        self.assertGreater(nf1, 0)
        self.assertGreater(rms1, 0.1)
        self.assertEqual(recorded_py["text"], "Hello Piper Python")
        self.assertEqual(recorded_py["syn_config"].length_scale, 0.5)
        self.assertEqual(recorded_py["syn_config"].noise_scale, 0.5)
        self.assertEqual(recorded_py["syn_config"].noise_w, 0.8)
        self.assertEqual(recorded_py["syn_config"].speaker_id, 1)

        # Tier 2: piper_cli
        recorded_cli: dict[str, Any] = {}

        class _FakePiperCliProc:
            returncode = 0

            async def communicate(self, stdin_bytes: bytes | None = None) -> tuple[bytes, bytes]:
                recorded_cli["stdin"] = stdin_bytes
                return sample_wav, b""

        async def _fake_cli_exec(*cmd: Any, **_kwargs: Any) -> _FakePiperCliProc:
            recorded_cli["cmd"] = [str(part) for part in cmd]
            return _FakePiperCliProc()

        def _fake_which(name: str) -> str | None:
            if name == "piper":
                return "/usr/local/bin/piper"
            if name == "/usr/bin/ffmpeg":
                return "/usr/bin/ffmpeg"
            return None

        client2 = PiperTTSClient(models_dir=self.models_dir)
        try:
            with (
                patch("bytey_ai_companion.tts.piper_provider._import_piper_voice", return_value=None),
                patch("bytey_ai_companion.tts.piper_provider.shutil.which", side_effect=_fake_which),
                patch(
                    "bytey_ai_companion.tts.piper_provider.asyncio.create_subprocess_exec",
                    side_effect=_fake_cli_exec,
                ),
            ):
                self.assertEqual(client2.detect_backend(), "piper_cli")
                wav2, fmt2 = await client2.synthesize(
                    "CLI test",
                    model="en_US-lessac-medium",
                    speed=0.8,
                    pitch=-4.0,
                    noise_scale=0.6,
                    speaker_id=3,
                    auto_download=False,
                )
        finally:
            await client2.close()

        self.assertEqual(fmt2, "wav")
        self.assertTrue(wav2.startswith(b"RIFF"))
        _, _, sr2, nf2, rms2 = _inspect_wav_bytes(wav2)
        self.assertEqual(sr2, 22050)
        self.assertGreater(nf2, 0)
        self.assertGreater(rms2, 0.05)
        self.assertEqual(recorded_cli["stdin"], b"CLI test")
        cmd = recorded_cli["cmd"]
        self.assertEqual(cmd[0], "/usr/local/bin/piper")
        self.assertIn("--length_scale", cmd)
        self.assertEqual(cmd[cmd.index("--length_scale") + 1], "1.25")
        self.assertEqual(cmd[cmd.index("--noise_scale") + 1], "0.6")
        self.assertEqual(cmd[cmd.index("--noise_w") + 1], "0.8")
        self.assertEqual(cmd[cmd.index("--speaker") + 1], "3")
        self.assertEqual(cmd[cmd.index("--output_file") + 1], "-")

    async def test_streaming_tts_player_piper_dispatch(self) -> None:
        expected_wav = _make_sine_wav_bytes(duration_s=0.1, sample_rate=22050)
        synth_calls: list[dict[str, Any]] = []
        played: list[tuple[bytes, str]] = []

        class _FakePiperClient:
            async def synthesize(self, text: str, **kwargs: Any) -> tuple[bytes, str]:
                synth_calls.append({"text": text, **kwargs})
                return expected_wav, "wav"

            async def close(self) -> None:
                return None

        cfg_voice_out = {
            "voice_out": {
                "tts_muted": False,
                "tts_provider": "piper",
                "piper_model": "pt_PT-tugão-medium",
                "piper_models_dir": "/tmp/custom-piper",
                "piper_speed": 1.35,
                "piper_pitch": 4.5,
                "piper_noise_scale": 0.85,
                "piper_speaker_id": 2,
            }
        }
        player = StreamingTTSPlayer(
            get_config=lambda: cfg_voice_out,
            get_secret=lambda _k: None,
            piper_client=_FakePiperClient(),  # type: ignore[arg-type]
        )

        async def _fake_play(audio_bytes: bytes, fmt: str, _gen: int) -> None:
            played.append((audio_bytes, fmt))

        try:
            direct = await player._synthesize("Direct check", player._read_settings())
            self.assertEqual(direct, (expected_wav, "wav"))

            with patch.object(player, "_play_audio", side_effect=_fake_play):
                await player.enqueue_sentence("Speak via queue!")
                await asyncio.wait_for(player.wait_idle(), timeout=3.0)
        finally:
            await player.stop()

        self.assertEqual(len(synth_calls), 2)
        for call in synth_calls:
            self.assertEqual(call["model"], "pt_PT-tugão-medium")
            self.assertEqual(call["models_dir"], "/tmp/custom-piper")
            self.assertEqual(call["speed"], 1.35)
            self.assertEqual(call["pitch"], 4.5)
            self.assertEqual(call["noise_scale"], 0.85)
            self.assertEqual(call["speaker_id"], 2)
        self.assertEqual(synth_calls[0]["text"], "Direct check")
        self.assertEqual(synth_calls[1]["text"], "Speak via queue!")
        self.assertEqual(played, [(expected_wav, "wav")])

    async def test_streaming_tts_player_pause_resume_and_barge_in(self) -> None:
        player = StreamingTTSPlayer(
            get_config=lambda: {},
            get_secret=lambda _k: None,
        )
        try:
            self.assertFalse(player.is_paused)
            # Idle pause returns False and does not flip is_paused
            self.assertFalse(await player.pause())
            self.assertFalse(player.is_paused)

            mock_proc = MagicMock()
            mock_proc.returncode = None
            mock_proc.send_signal = MagicMock()
            mock_proc.terminate = MagicMock()
            mock_proc.kill = MagicMock()
            mock_proc.wait = AsyncMock(return_value=0)

            player._speaking = True
            player._active_proc = mock_proc

            self.assertTrue(await player.pause())
            self.assertTrue(player.is_paused)
            self.assertFalse(player._pause_event.is_set())
            mock_proc.send_signal.assert_called_with(signal.SIGSTOP)

            self.assertTrue(await player.resume())
            self.assertFalse(player.is_paused)
            self.assertTrue(player._pause_event.is_set())
            mock_proc.send_signal.assert_called_with(signal.SIGCONT)

            # Resuming when not paused returns False
            self.assertFalse(await player.resume())

            # barge_in() resets _paused and terminates active proc
            self.assertTrue(await player.pause())
            self.assertTrue(player.is_paused)
            await player.barge_in()
            self.assertFalse(player.is_paused)
            self.assertTrue(player._pause_event.is_set())
            self.assertFalse(player._speaking)
            self.assertIsNone(player._active_proc)
        finally:
            await player.stop()


class TestPiperDaemonAPI(unittest.IsolatedAsyncioTestCase):
    """REST integration tests for Piper TTS voices, download, and config patch endpoints."""

    async def asyncSetUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp_dir.name)
        self.fake_home = self.root / "home"
        self.fake_home.mkdir(parents=True, exist_ok=True)
        self.models_dir = self.root / "piper_models"
        self.models_dir.mkdir(parents=True, exist_ok=True)
        self._home_patcher = patch("pathlib.Path.home", return_value=self.fake_home)
        self._home_patcher.start()

        self.download_requests: list[str] = []
        fake_cfg = json.dumps(
            {
                "audio": {"sample_rate": 22050, "quality": "medium"},
                "language": {"code": "en_US"},
                "num_speakers": 1,
            }
        ).encode("utf-8")
        fake_onnx = b"DAEMON_MOCK_ONNX_MODEL"

        def _piper_http(request: httpx.Request) -> httpx.Response:
            url_str = str(request.url)
            self.download_requests.append(url_str)
            if url_str.endswith(".onnx.json"):
                return httpx.Response(200, content=fake_cfg)
            if url_str.endswith(".onnx"):
                return httpx.Response(200, content=fake_onnx)
            return httpx.Response(404)

        self.piper_http = httpx.AsyncClient(transport=httpx.MockTransport(_piper_http))
        self.piper_client = PiperTTSClient(models_dir=self.models_dir, http_client=self.piper_http)
        self.daemon = ByteyDaemon(
            host="127.0.0.1",
            port=0,
            config_dir=self.root / "cfg",
            data_dir=self.root / "data",
            enable_voice_capture=False,
            llm_router=_MockLLMRouter(),  # type: ignore[arg-type]
            piper_client=self.piper_client,
        )
        self.daemon.config_manager.update_config(
            {"voice_out": {"tts_muted": True, "tts_provider": "disabled"}}
        )
        await self.daemon.start()
        self.base_url = f"http://127.0.0.1:{self.daemon.ipc.port}"

    async def asyncTearDown(self) -> None:
        try:
            await self.daemon.stop()
            await self.piper_http.aclose()
        finally:
            self._home_patcher.stop()
            self.tmp_dir.cleanup()

    async def test_piper_voices_download_and_config_endpoints(self) -> None:
        async with httpx.AsyncClient(base_url=self.base_url, timeout=10.0) as client:
            # GET /api/v1/tts/voices?provider=piper&language=en
            r_voices = await client.get("/api/v1/tts/voices", params={"provider": "piper", "language": "en"})
            self.assertEqual(r_voices.status_code, 200)
            payload = r_voices.json()
            self.assertEqual(payload["models_dir"], str(self.models_dir))
            self.assertIn(payload["backend"], ("piper_python", "piper_cli", "onnx_espeak", "unavailable"))
            self.assertEqual(payload["presets"], PIPER_VOICE_PRESETS)
            voice_ids = {v["id"] for v in payload["voices"]}
            self.assertIn("en_US-lessac-medium", voice_ids)
            self.assertIn("en_GB-alan-medium", voice_ids)
            self.assertTrue(all(str(v["language"]).startswith("en") for v in payload["voices"]))

            # POST /api/v1/tts/piper/download (valid)
            r_dl = await client.post("/api/v1/tts/piper/download", json={"model": "en_US-lessac-medium"})
            self.assertEqual(r_dl.status_code, 200)
            dl_data = r_dl.json()
            self.assertTrue(dl_data["ok"])
            self.assertTrue(dl_data["installed"])
            self.assertEqual(dl_data["model"], "en_US-lessac-medium")
            self.assertEqual(len(self.download_requests), 2)

            r_voices_after = await client.get(
                "/api/v1/tts/voices", params={"provider": "piper", "language": "en"}
            )
            self.assertEqual(r_voices_after.status_code, 200)
            lessac = next(v for v in r_voices_after.json()["voices"] if v["id"] == "en_US-lessac-medium")
            self.assertTrue(lessac["installed"])

            # POST /api/v1/tts/piper/download rejects traversal & empty model IDs
            for bad_model in ("../evil", "dir/model", "   "):
                r_bad = await client.post("/api/v1/tts/piper/download", json={"model": bad_model})
                self.assertGreaterEqual(r_bad.status_code, 400)
                self.assertLess(r_bad.status_code, 500)
                self.assertIn("error", r_bad.json())

            # PATCH /api/v1/config with Piper fields (voice_out + voice_output) & GET roundtrip
            r_patch1 = await client.patch(
                "/api/v1/config",
                json={
                    "voice_out": {
                        "tts_provider": "piper",
                        "piper_model": "en_US-lessac-medium",
                        "piper_speed": 1.4,
                        "piper_pitch": 6.5,
                        "piper_noise_scale": 0.9,
                        "piper_speaker_id": 1,
                    }
                },
            )
            self.assertEqual(r_patch1.status_code, 200)
            cfg1 = (await client.get("/api/v1/config")).json()
            self.assertEqual(cfg1["voice_out"]["tts_provider"], "piper")
            self.assertEqual(cfg1["voice_out"]["piper_model"], "en_US-lessac-medium")
            self.assertEqual(cfg1["voice_out"]["piper_speed"], 1.4)
            self.assertEqual(cfg1["voice_out"]["piper_pitch"], 6.5)
            self.assertEqual(cfg1["voice_out"]["piper_noise_scale"], 0.9)
            self.assertEqual(cfg1["voice_out"]["piper_speaker_id"], 1)
            self.assertEqual(cfg1["voice_output"]["provider"], "piper")
            self.assertEqual(cfg1["voice_output"]["piper_speed"], 1.4)
            self.assertEqual(cfg1["voice_output"]["piper_pitch"], 6.5)

            # Out-of-range values via voice_output mirror are clamped cleanly
            r_patch2 = await client.patch(
                "/api/v1/config",
                json={
                    "voice_output": {
                        "provider": "piper",
                        "piper_speed": 0.1,
                        "piper_pitch": -50,
                        "piper_noise_scale": 9.0,
                        "piper_speaker_id": -2,
                    }
                },
            )
            self.assertEqual(r_patch2.status_code, 200)
            cfg2 = (await client.get("/api/v1/config")).json()
            self.assertEqual(cfg2["voice_out"]["piper_speed"], 0.4)
            self.assertEqual(cfg2["voice_out"]["piper_pitch"], -12.0)
            self.assertEqual(cfg2["voice_out"]["piper_noise_scale"], 1.5)
            self.assertEqual(cfg2["voice_out"]["piper_speaker_id"], 0)
            self.assertEqual(cfg2["voice_output"]["piper_speed"], 0.4)
            self.assertEqual(cfg2["voice_output"]["piper_pitch"], -12.0)
            self.assertEqual(cfg2["voice_output"]["piper_noise_scale"], 1.5)
            self.assertEqual(cfg2["voice_output"]["piper_speaker_id"], 0)


class TestSystemPromptAndConfigReplace(unittest.IsolatedAsyncioTestCase):
    def test_default_system_prompt_loaded_from_markdown(self) -> None:
        from bytey_ai_companion.llm.emotions import DEFAULT_SYSTEM_PROMPT, load_default_system_prompt

        md_path = (
            REPO_ROOT
            / "core-daemon"
            / "bytey_ai_companion"
            / "llm"
            / "default_system_prompt.md"
        )
        self.assertTrue(md_path.is_file())
        md_text = md_path.read_text(encoding="utf-8").strip()
        self.assertEqual(DEFAULT_SYSTEM_PROMPT, md_text)
        self.assertEqual(load_default_system_prompt(), md_text)
        self.assertIn("[emotion:happy]", md_text)

        sys.path.insert(0, str(REPO_ROOT / "settings-ui"))
        try:
            from bytey_settings.api_client import DEFAULT_SYSTEM_PROMPT as UI_DEFAULT_PROMPT

            self.assertEqual(UI_DEFAULT_PROMPT, md_text)
        finally:
            if str(REPO_ROOT / "settings-ui") in sys.path:
                sys.path.remove(str(REPO_ROOT / "settings-ui"))

    def test_config_manager_replace_config_replaces_all_sections(self) -> None:
        from bytey_ai_companion.config import ConfigManager

        with tempfile.TemporaryDirectory() as tmp:
            cm = ConfigManager(config_dir=Path(tmp))
            cm.update_config(
                {
                    "general": {"pet_name": "OldName", "wake_phrase": "Hey Old", "system_prompt": "Old prompt"},
                    "visuals": {"eye_color": "#112233"},
                    "mcp_servers": {"old_srv": {"transport": "stdio", "command": "echo"}},
                }
            )
            replaced = cm.replace_config(
                {
                    "general": {"pet_name": "NewName", "system_prompt": "New prompt"},
                    "provider": {"active_provider": "openai", "model": "gpt-4o-mini"},
                }
            )
            self.assertEqual(replaced.general.pet_name, "NewName")
            self.assertEqual(replaced.general.system_prompt, "New prompt")
            self.assertEqual(replaced.general.wake_phrase, "Hey Bytey")
            self.assertEqual(replaced.visuals.eye_color, "#F7FAFF")
            self.assertEqual(replaced.mcp_servers, {})
            self.assertEqual(replaced.provider.active_provider, "openai")

            with self.assertRaises(ValueError):
                cm.replace_config({"unrelated": 123})

    async def test_put_config_replaces_full_config_and_exposes_default_prompt(self) -> None:
        from bytey_ai_companion.llm.emotions import DEFAULT_SYSTEM_PROMPT
        from bytey_ai_companion.main import ByteyDaemon

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            daemon = ByteyDaemon(
                host="127.0.0.1",
                port=0,
                config_dir=tmp_path / "cfg",
                data_dir=tmp_path / "data",
                enable_voice_capture=False,
            )
            await daemon.start()
            try:
                base_url = f"http://127.0.0.1:{daemon.ipc.port}"
                async with httpx.AsyncClient(base_url=base_url, timeout=5.0) as client:
                    r_patch = await client.patch(
                        "/api/v1/config",
                        json={
                            "general": {"pet_name": "CustomBot", "wake_phrase": "Hey Custom"},
                            "visuals": {"eye_color": "#ABCDEF"},
                        },
                    )
                    self.assertEqual(r_patch.status_code, 200)

                    r_put = await client.put(
                        "/api/v1/config",
                        json={
                            "general": {"pet_name": "ImportedBot", "system_prompt": "Imported prompt"},
                            "provider": {"active_provider": "ollama", "model": "llama3.2"},
                        },
                    )
                    self.assertEqual(r_put.status_code, 200)
                    body = r_put.json()
                    self.assertEqual(body["general"]["pet_name"], "ImportedBot")
                    self.assertEqual(body["general"]["system_prompt"], "Imported prompt")
                    self.assertEqual(body["general"]["wake_phrase"], "Hey Bytey")
                    self.assertEqual(body["visuals"]["eye_color"], "#F7FAFF")
                    self.assertEqual(body["default_system_prompt"], DEFAULT_SYSTEM_PROMPT)

                    r_get = await client.get("/api/v1/config")
                    self.assertEqual(r_get.status_code, 200)
                    self.assertEqual(r_get.json()["general"]["pet_name"], "ImportedBot")
                    self.assertEqual(r_get.json()["default_system_prompt"], DEFAULT_SYSTEM_PROMPT)
            finally:
                await daemon.stop()


class TestExpandedEmotionsAndPreview(unittest.IsolatedAsyncioTestCase):
    """Tests for expanded mood tags, idle/cadence rules, system prompt, and visuals preview API."""

    def test_valid_emotions_and_parser_support_new_tags(self) -> None:
        from bytey_ai_companion.llm.emotions import DEFAULT_SYSTEM_PROMPT

        for tag in ("happy", "annoyed", "love", "sleepy", "focus", "astonished"):
            self.assertIn(tag, VALID_EMOTIONS)
            self.assertIn(f"[emotion:{tag}]", DEFAULT_SYSTEM_PROMPT)

            seen: list[str] = []
            parser = EmotionStreamParser(on_emotion=seen.append)
            out = parser.feed(f"[emotion:{tag}] ") + parser.feed("Hello!") + parser.flush()
            self.assertEqual(parser.emotion, tag)
            self.assertEqual(seen, [tag])
            self.assertEqual(out, "Hello!")

    def test_idle_emotion_schedule_daytime_vs_nighttime(self) -> None:
        from bytey_ai_companion.main import ByteyDaemon

        # Under 60 minutes -> None
        self.assertIsNone(ByteyDaemon.evaluate_idle_emotion(3599.0, 12))
        self.assertIsNone(ByteyDaemon.evaluate_idle_emotion(1800.0, 22))

        # 8:00 AM to 7:59 PM (>= 60 min) -> annoyed
        self.assertEqual(ByteyDaemon.evaluate_idle_emotion(3600.0, 8), "annoyed")
        self.assertEqual(ByteyDaemon.evaluate_idle_emotion(4000.0, 15), "annoyed")
        self.assertEqual(ByteyDaemon.evaluate_idle_emotion(3600.0, 19), "annoyed")

        # 8:00 PM to 7:59 AM (>= 60 min) -> sleepy
        self.assertEqual(ByteyDaemon.evaluate_idle_emotion(3600.0, 20), "sleepy")
        self.assertEqual(ByteyDaemon.evaluate_idle_emotion(3600.0, 23), "sleepy")
        self.assertEqual(ByteyDaemon.evaluate_idle_emotion(3600.0, 0), "sleepy")
        self.assertEqual(ByteyDaemon.evaluate_idle_emotion(3600.0, 7), "sleepy")

    async def test_interaction_cadence_and_visuals_preview_endpoint(self) -> None:
        from bytey_ai_companion.main import ByteyDaemon

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            daemon = ByteyDaemon(
                host="127.0.0.1",
                port=0,
                config_dir=tmp_path / "cfg",
                data_dir=tmp_path / "data",
                enable_voice_capture=False,
            )
            await daemon.start()
            try:
                # Rapid interactions within 5 minutes
                daemon._record_interaction(1000.0)
                daemon._record_interaction(1060.0)
                daemon._record_interaction(1120.0)
                _, count = daemon._record_interaction(1180.0)
                self.assertEqual(count, 4)

                # Old interactions pruned after 5 minutes
                _, pruned_count = daemon._record_interaction(1600.0)
                self.assertEqual(pruned_count, 1)

                base_url = f"http://127.0.0.1:{daemon.ipc.port}"
                async with httpx.AsyncClient(base_url=base_url, timeout=5.0) as client:
                    r_ok = await client.post(
                        "/api/v1/visuals/preview",
                        json={"state": "THINKING", "emotion": "astonished"},
                    )
                    self.assertEqual(r_ok.status_code, 200)
                    self.assertEqual(r_ok.json()["state"], "THINKING")
                    self.assertEqual(r_ok.json()["emotion"], "astonished")

                    r_love = await client.post(
                        "/api/v1/visuals/preview",
                        json={"state": "IDLE", "emotion": "love"},
                    )
                    self.assertEqual(r_love.status_code, 200)
                    self.assertEqual(r_love.json()["emotion"], "love")

                    r_bad = await client.post(
                        "/api/v1/visuals/preview",
                        json={"state": "IDLE", "emotion": "not_a_mood"},
                    )
                    self.assertEqual(r_bad.status_code, 400)

                    r_reset = await client.post(
                        "/api/v1/visuals/preview",
                        json={"reset": True},
                    )
                    self.assertEqual(r_reset.status_code, 200)
                    self.assertEqual(r_reset.json()["emotion"], "neutral")
            finally:
                await daemon.stop()


class TestSkillRegistry(unittest.TestCase):
    """Unit tests for SkillRegistry discovery, parsing, security, and CRUD."""

    def test_frontmatter_parsing_and_invalid_slug(self) -> None:
        raw = (
            "---\n"
            "name: test-skill\n"
            'description: "Parse # safely" # comment\n'
            "requires_confirmation: true\n"
            'allowed_tools: [read, "write"]\n'
            "---\n\n"
            "Body line 1.\n"
        )
        meta, body = parse_frontmatter(raw)
        self.assertEqual(meta["name"], "test-skill")
        self.assertEqual(meta["description"], "Parse # safely")
        self.assertTrue(meta["requires_confirmation"])
        self.assertEqual(meta["allowed_tools"], ["read", "write"])
        self.assertEqual(body.strip(), "Body line 1.")

        with self.assertRaises(ValueError):
            parse_frontmatter("no frontmatter here")
        with self.assertRaises(ValueError):
            parse_frontmatter("---\nname: broken\n")

        self.assertTrue(is_valid_skill_slug("valid_skill-1"))
        self.assertFalse(is_valid_skill_slug("InvalidSlug"))
        self.assertFalse(is_valid_skill_slug("-leading-dash"))
        self.assertFalse(is_valid_skill_slug("bad/slug"))
        self.assertFalse(is_valid_skill_slug(""))

        with tempfile.TemporaryDirectory() as tmp:
            user_dir = Path(tmp) / "skills"
            bad_dir = user_dir / "BadSlug"
            bad_dir.mkdir(parents=True)
            (bad_dir / "SKILL.md").write_text(
                "---\nname: BadSlug\ndescription: Invalid\n---\n\nHello\n",
                encoding="utf-8",
            )
            reg = SkillRegistry(skills_dir=str(user_dir))
            reg.rescan()
            bad_skill = reg.get_skill("BadSlug")
            self.assertIsNotNone(bad_skill)
            assert bad_skill is not None
            self.assertFalse(bad_skill.valid)
            self.assertIn("Invalid skill slug", bad_skill.error or "")

    def test_precedence_user_over_extra_over_builtin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            user_dir = root / "user_skills"
            extra_dir = root / "extra_skills"

            (user_dir / "shared-skill").mkdir(parents=True)
            (user_dir / "shared-skill" / "SKILL.md").write_text(
                "---\nname: shared-skill\ndescription: From user\n---\n\nUser body\n",
                encoding="utf-8",
            )
            (extra_dir / "shared-skill").mkdir(parents=True)
            (extra_dir / "shared-skill" / "SKILL.md").write_text(
                "---\nname: shared-skill\ndescription: From extra\n---\n\nExtra body\n",
                encoding="utf-8",
            )
            (extra_dir / "extra-only").mkdir(parents=True)
            (extra_dir / "extra-only" / "SKILL.md").write_text(
                "---\nname: extra-only\ndescription: Extra only\n---\n\nExtra only body\n",
                encoding="utf-8",
            )

            reg = SkillRegistry(skills_dir=str(user_dir), extra_dirs=[str(extra_dir)])
            reg.rescan()

            shared = reg.get_skill("shared-skill")
            self.assertIsNotNone(shared)
            assert shared is not None
            self.assertEqual(shared.source, "user")
            self.assertEqual(shared.description, "From user")

            extra_only = reg.get_skill("extra-only")
            self.assertIsNotNone(extra_only)
            assert extra_only is not None
            self.assertEqual(extra_only.source, "extra")

            builtins = [s for s in reg.list_skills(include_disabled=True) if s.source == "builtin"]
            if builtins:
                b_name = builtins[0].name
                (extra_dir / b_name).mkdir(parents=True)
                (extra_dir / b_name / "SKILL.md").write_text(
                    f"---\nname: {b_name}\ndescription: Extra override\n---\n\nExtra\n",
                    encoding="utf-8",
                )
                reg.rescan()
                overridden = reg.get_skill(b_name)
                self.assertIsNotNone(overridden)
                assert overridden is not None
                self.assertEqual(overridden.source, "extra")

    def test_read_skill_resource_and_path_traversal_rejection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            user_dir = root / "user_skills"
            skill_dir = user_dir / "res-skill"
            (skill_dir / "references").mkdir(parents=True)
            (skill_dir / "resources").mkdir(parents=True)
            (skill_dir / "scripts").mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                "---\nname: res-skill\ndescription: Resource test\n---\n\nBody\n",
                encoding="utf-8",
            )
            (skill_dir / "references" / "guide.md").write_text("Guide text", encoding="utf-8")
            (skill_dir / "scripts" / "helper.py").write_text("print(1)", encoding="utf-8")

            outside = root / "secret.txt"
            outside.write_text("top-secret", encoding="utf-8")
            (skill_dir / "resources" / "escape.txt").symlink_to(outside)

            reg = SkillRegistry(skills_dir=str(user_dir))
            reg.rescan()

            self.assertEqual(reg.read_skill_resource("res-skill", "references/guide.md"), "Guide text")
            with self.assertRaises(ValueError):
                reg.read_skill_resource("res-skill", "../secret.txt")
            with self.assertRaises(ValueError):
                reg.read_skill_resource("res-skill", "/etc/passwd")
            with self.assertRaises(ValueError):
                reg.read_skill_resource("res-skill", "scripts/helper.py")
            with self.assertRaises(ValueError):
                reg.read_skill_resource("res-skill", "resources/escape.txt")

    def test_format_prompt_catalog_create_import_and_delete(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            user_dir = root / "user_skills"
            reg = SkillRegistry(
                skills_dir=str(user_dir),
                overrides={"disabled-skill": {"enabled": False}},
            )
            reg.create_or_update_skill(
                "custom-helper",
                description="Helps with tasks",
                body="Step 1",
                slash_command="helper",
            )
            reg.create_or_update_skill(
                "disabled-skill",
                description="Should be hidden",
                body="Hidden",
            )

            catalog = reg.format_prompt_catalog()
            self.assertIn("<available_skills>", catalog)
            self.assertIn("- custom-helper: Helps with tasks", catalog)
            self.assertNotIn("disabled-skill", catalog)

            # Import from folder
            src_folder = root / "folder_src"
            (src_folder / "references").mkdir(parents=True)
            (src_folder / "SKILL.md").write_text(
                "---\nname: folder-skill\ndescription: Imported folder\n---\n\nFolder body\n",
                encoding="utf-8",
            )
            (src_folder / "references" / "doc.md").write_text("Doc", encoding="utf-8")
            imported_folder = reg.import_skill(src_folder)
            self.assertEqual(imported_folder.name, "folder-skill")
            self.assertEqual(imported_folder.source, "user")
            self.assertIn("references/doc.md", imported_folder.references)

            # Import from .zip
            good_zip = root / "zip_skill.zip"
            with zipfile.ZipFile(good_zip, "w") as zf:
                zf.writestr(
                    "zip-skill/SKILL.md",
                    "---\nname: zip-skill\ndescription: Imported zip\n---\n\nZip body\n",
                )
            imported_zip = reg.import_skill(good_zip)
            self.assertEqual(imported_zip.name, "zip-skill")

            # Reject zip-slip
            bad_zip = root / "slip.zip"
            with zipfile.ZipFile(bad_zip, "w") as zf:
                zf.writestr("../evil.txt", "pwned")
                zf.writestr(
                    "SKILL.md",
                    "---\nname: evil-skill\ndescription: Bad\n---\n\nBad\n",
                )
            with self.assertRaises(ValueError):
                reg.import_skill(bad_zip)

            # Delete user skill succeeds; deleting builtin rejects
            self.assertTrue(reg.delete_user_skill("custom-helper"))
            self.assertIsNone(reg.get_skill("custom-helper"))

            builtins = [s for s in reg.list_skills(include_disabled=True) if s.source == "builtin"]
            if builtins:
                with self.assertRaises(ValueError):
                    reg.delete_user_skill(builtins[0].name)


class TestSkillScriptRunner(unittest.IsolatedAsyncioTestCase):
    """Unit tests for SkillScriptRunner sandboxing, env sanitization, timeout, and output cap."""

    async def test_run_script_sanitized_env_and_path_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            user_dir = root / "user_skills"
            skill_dir = user_dir / "script-skill"
            scripts_dir = skill_dir / "scripts"
            scripts_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                "---\nname: script-skill\ndescription: Script test\n---\n\nBody\n",
                encoding="utf-8",
            )
            (scripts_dir / "env_echo.py").write_text(
                "import os, sys\n"
                "print('GEMINI=' + os.environ.get('GEMINI_API_KEY', 'MISSING'))\n"
                "print('OPENAI=' + os.environ.get('OPENAI_API_KEY', 'MISSING'))\n"
                "print('ARGS=' + ','.join(sys.argv[1:]))\n",
                encoding="utf-8",
            )
            outside_script = root / "outside.py"
            outside_script.write_text("print('escaped')\n", encoding="utf-8")
            (scripts_dir / "escape.py").symlink_to(outside_script)

            reg = SkillRegistry(skills_dir=str(user_dir))
            reg.rescan()
            skill = reg.get_skill("script-skill")
            assert skill is not None

            runner = SkillScriptRunner()
            with patch.dict(
                os.environ,
                {"GEMINI_API_KEY": "secret-gemini", "OPENAI_API_KEY": "secret-openai"},
            ):
                res = await runner.run(skill, "env_echo.py", args=["alpha", "beta"])

            self.assertEqual(res.returncode, 0)
            self.assertFalse(res.timed_out)
            self.assertIn("GEMINI=MISSING", res.stdout)
            self.assertIn("OPENAI=MISSING", res.stdout)
            self.assertNotIn("secret-gemini", res.stdout)
            self.assertNotIn("secret-openai", res.stdout)
            self.assertIn("ARGS=alpha,beta", res.stdout)

            with self.assertRaises(ValueError):
                runner.resolve_script_path(skill, "../outside.py")
            with self.assertRaises(ValueError):
                runner.resolve_script_path(skill, "/bin/sh")
            with self.assertRaises(ValueError):
                runner.resolve_script_path(skill, "escape.py")

    async def test_run_script_timeout_and_output_cap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            user_dir = Path(tmp) / "user_skills"
            skill_dir = user_dir / "cap-skill"
            scripts_dir = skill_dir / "scripts"
            scripts_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                "---\nname: cap-skill\ndescription: Cap test\n---\n\nBody\n",
                encoding="utf-8",
            )
            (scripts_dir / "slow.py").write_text(
                "import time\ntime.sleep(2.0)\n",
                encoding="utf-8",
            )
            (scripts_dir / "noisy.py").write_text(
                "print('X' * 5000)\n",
                encoding="utf-8",
            )

            reg = SkillRegistry(skills_dir=str(user_dir))
            reg.rescan()
            skill = reg.get_skill("cap-skill")
            assert skill is not None

            runner = SkillScriptRunner(max_output_bytes=256)
            t_res = await runner.run(skill, "slow.py", timeout_s=0.1)
            self.assertTrue(t_res.timed_out)
            self.assertEqual(t_res.returncode, -1)

            c_res = await runner.run(skill, "noisy.py")
            self.assertEqual(c_res.returncode, 0)
            self.assertTrue(c_res.truncated)
            self.assertIn("[Truncated at 32 KiB]", c_res.stdout)


class TestWorkspaceToolchain(unittest.IsolatedAsyncioTestCase):
    """Unit tests for WorkspaceConfig, WorkspaceToolchain, workspace skills, and MCP expansion."""

    def test_workspace_config_validation_and_recent_paths_dedup_cap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dirs = [root / f"proj_{i}" for i in range(12)]
            for d in dirs:
                d.mkdir()

            with self.assertRaises(ValidationError):
                WorkspaceConfig(active_path=str(root / "missing_dir"))

            cfg = WorkspaceConfig(
                active_path=str(dirs[0]),
                recent_paths=[str(dirs[0]), str(dirs[0]), *[str(d) for d in dirs]],
            )
            self.assertEqual(cfg.active_path, str(dirs[0].resolve()))
            self.assertEqual(len(cfg.recent_paths), 10)
            self.assertEqual(len(set(cfg.recent_paths)), 10)
            self.assertEqual(cfg.recent_paths[0], str(dirs[0].resolve()))

    def test_list_files_grep_read_and_path_traversal_rejection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ws = root / "ws"
            ws.mkdir()
            (ws / "src").mkdir()
            (ws / "src" / "main.py").write_text("alpha = 1\nbeta = 2\ngamma = 3\n", encoding="utf-8")
            (ws / ".git").mkdir()
            (ws / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
            (ws / "node_modules").mkdir()
            (ws / "node_modules" / "pkg.js").write_text("beta = 99\n", encoding="utf-8")

            outside = root / "secret.txt"
            outside.write_text("top-secret\n", encoding="utf-8")
            (ws / "escape_link.txt").symlink_to(outside)

            tc = WorkspaceToolchain(ws)
            files = tc.list_files()
            self.assertEqual(files, ["src/main.py"])

            matches = tc.grep_search(r"beta")
            self.assertEqual(len(matches), 1)
            self.assertEqual(matches[0]["path"], "src/main.py")
            self.assertEqual(matches[0]["line"], 2)
            self.assertIn("beta = 2", matches[0]["text"])

            with self.assertRaises(ValueError):
                tc.grep_search("")

            snippet = tc.read_file("src/main.py", start_line=2, end_line=3)
            self.assertEqual(snippet, "2: beta = 2\n3: gamma = 3")

            for bad_path in ("../secret.txt", str(outside), "escape_link.txt"):
                with self.assertRaises(ValueError):
                    tc.read_file(bad_path)
                with self.assertRaises(ValueError):
                    tc.preview_edit(bad_path, "a", "b")
                with self.assertRaises(ValueError):
                    tc.apply_write(bad_path, "x")

    async def test_preview_apply_edit_write_run_command_and_load_workspace_rules(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "ws"
            ws.mkdir()
            (ws / "hello.txt").write_text("hello world\n", encoding="utf-8")
            (ws / "AGENTS.md").write_text("Rule 1: test first.", encoding="utf-8")
            (ws / ".agents").mkdir()
            (ws / ".agents" / "GEMINI.md").write_text("Rule 2: be concise.", encoding="utf-8")
            (ws / ".agents" / "CLAUDE.md").write_text("Rule 3: verify.", encoding="utf-8")

            tc = WorkspaceToolchain(ws)

            miss = tc.preview_edit("hello.txt", "missing", "new")
            self.assertFalse(miss["ok"])
            self.assertIn("not found", miss["error"].lower())

            prev = tc.preview_edit("hello.txt", "world", "bytey")
            self.assertTrue(prev["ok"])
            self.assertIn("-hello world", prev["diff"])
            self.assertIn("+hello bytey", prev["diff"])
            self.assertEqual((ws / "hello.txt").read_text(encoding="utf-8"), "hello world\n")

            applied = tc.apply_edit("hello.txt", "world", "bytey")
            self.assertTrue(applied["ok"])
            self.assertEqual((ws / "hello.txt").read_text(encoding="utf-8"), "hello bytey\n")

            w_prev = tc.preview_write("nested/new.txt", "line1\n")
            self.assertTrue(w_prev["ok"])
            self.assertTrue(w_prev["is_new"])
            self.assertFalse((ws / "nested" / "new.txt").exists())

            w_app = tc.apply_write("nested/new.txt", "line1\n")
            self.assertTrue(w_app["ok"])
            self.assertTrue(w_app["is_new"])
            self.assertEqual((ws / "nested" / "new.txt").read_text(encoding="utf-8"), "line1\n")

            w_rej = tc.preview_write("nested/new.txt", "replaced\n")
            self.assertFalse(w_rej["ok"])
            self.assertIn("already exists", w_rej["error"].lower())
            w_rej_app = tc.apply_write("nested/new.txt", "replaced\n")
            self.assertFalse(w_rej_app["ok"])
            self.assertEqual((ws / "nested" / "new.txt").read_text(encoding="utf-8"), "line1\n")

            w_ow = tc.apply_write("nested/new.txt", "replaced", overwrite=True)
            self.assertTrue(w_ow["ok"])
            self.assertFalse(w_ow["is_new"])
            self.assertEqual((ws / "nested" / "new.txt").read_text(encoding="utf-8"), "replaced")

            a_prev = tc.preview_append("nested/new.txt", "line2\n")
            self.assertTrue(a_prev["ok"])
            self.assertIn("+line2", a_prev["diff"])
            self.assertEqual((ws / "nested" / "new.txt").read_text(encoding="utf-8"), "replaced")

            a_app = tc.apply_append("nested/new.txt", "line2\n")
            self.assertTrue(a_app["ok"])
            self.assertEqual((ws / "nested" / "new.txt").read_text(encoding="utf-8"), "replaced\nline2\n")

            cmd_ok = await tc.run_command("pwd")
            self.assertTrue(cmd_ok["ok"])
            self.assertEqual(cmd_ok["returncode"], 0)
            self.assertEqual(cmd_ok["stdout"].strip(), str(ws.resolve()))

            cmd_to = await tc.run_command("sleep 1", timeout_s=0.1)
            self.assertFalse(cmd_to["ok"])
            self.assertTrue(cmd_to["timed_out"])
            self.assertEqual(cmd_to["returncode"], -1)

            rules = load_workspace_rules(ws)
            self.assertEqual(
                [r[0] for r in rules],
                ["AGENTS.md", ".agents/GEMINI.md", ".agents/CLAUDE.md"],
            )

            # Delete preview/apply + guards
            (ws / "obsolete.txt").write_text("bye\n", encoding="utf-8")
            d_prev = tc.preview_delete("obsolete.txt")
            self.assertTrue(d_prev["ok"])
            self.assertIn("-bye", d_prev["diff"])
            self.assertTrue((ws / "obsolete.txt").exists())

            d_app = tc.apply_delete("obsolete.txt")
            self.assertTrue(d_app["ok"])
            self.assertFalse((ws / "obsolete.txt").exists())
            self.assertFalse(tc.preview_delete("obsolete.txt")["ok"])
            self.assertFalse(tc.preview_delete("nested")["ok"])
            self.assertFalse(tc.preview_delete(".")["ok"])
            with self.assertRaises(ValueError):
                tc.preview_delete("../outside.txt")

            # Rename preview/apply + guards
            (ws / "src_file.txt").write_text("move me\n", encoding="utf-8")
            r_prev = tc.preview_rename("src_file.txt", "moved/sub/dst_file.txt")
            self.assertTrue(r_prev["ok"])
            self.assertIn("rename src_file.txt -> moved/sub/dst_file.txt", r_prev["diff"])
            self.assertTrue((ws / "src_file.txt").exists())

            r_app = tc.apply_rename("src_file.txt", "moved/sub/dst_file.txt")
            self.assertTrue(r_app["ok"])
            self.assertFalse((ws / "src_file.txt").exists())
            self.assertEqual(
                (ws / "moved" / "sub" / "dst_file.txt").read_text(encoding="utf-8"),
                "move me\n",
            )

            (ws / "conflict_src.txt").write_text("new\n", encoding="utf-8")
            (ws / "conflict_dst.txt").write_text("old\n", encoding="utf-8")
            self.assertFalse(
                tc.preview_rename("conflict_src.txt", "conflict_dst.txt")["ok"]
            )
            r_ov = tc.apply_rename("conflict_src.txt", "conflict_dst.txt", overwrite=True)
            self.assertTrue(r_ov["ok"])
            self.assertEqual(
                (ws / "conflict_dst.txt").read_text(encoding="utf-8"), "new\n"
            )
            self.assertFalse(tc.preview_rename("missing.txt", "x.txt")["ok"])
            self.assertFalse(tc.preview_rename("conflict_dst.txt", "conflict_dst.txt")["ok"])
            with self.assertRaises(ValueError):
                tc.preview_rename("conflict_dst.txt", "../escape.txt")

    def test_skill_registry_workspace_precedence_and_eviction(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ws = root / "ws"
            user_dir = root / "user_skills"
            extra_dir = root / "extra_skills"

            (ws / ".agents" / "skills" / "shared-skill").mkdir(parents=True)
            (ws / ".agents" / "skills" / "shared-skill" / "SKILL.md").write_text(
                "---\nname: shared-skill\ndescription: From workspace\n---\n\nWS body\n",
                encoding="utf-8",
            )
            (ws / ".agents" / "skills" / "ws-only").mkdir(parents=True)
            (ws / ".agents" / "skills" / "ws-only" / "SKILL.md").write_text(
                "---\nname: ws-only\ndescription: Only workspace\n---\n\nWS only\n",
                encoding="utf-8",
            )
            (user_dir / "shared-skill").mkdir(parents=True)
            (user_dir / "shared-skill" / "SKILL.md").write_text(
                "---\nname: shared-skill\ndescription: From user\n---\n\nUser body\n",
                encoding="utf-8",
            )
            (extra_dir / "shared-skill").mkdir(parents=True)
            (extra_dir / "shared-skill" / "SKILL.md").write_text(
                "---\nname: shared-skill\ndescription: From extra\n---\n\nExtra body\n",
                encoding="utf-8",
            )

            reg = SkillRegistry(
                skills_dir=str(user_dir),
                extra_dirs=[str(extra_dir)],
                workspace_dir=str(ws),
            )
            reg.rescan()
            shared = reg.get_skill("shared-skill")
            self.assertIsNotNone(shared)
            assert shared is not None
            self.assertEqual(shared.source, "workspace")
            self.assertEqual(shared.description, "From workspace")
            self.assertIsNotNone(reg.get_skill("ws-only"))

            reg.rescan(workspace_dir="")
            self.assertIsNone(reg.get_skill("ws-only"))
            shared_after = reg.get_skill("shared-skill")
            self.assertIsNotNone(shared_after)
            assert shared_after is not None
            self.assertEqual(shared_after.source, "user")

    async def test_mcp_client_manager_workspace_expansion(self) -> None:
        async def _noop_connect(_self: Any) -> None:
            return None

        mgr = MCPClientManager(workspace_path="/tmp/ws_alpha")
        with patch("bytey_ai_companion.mcp_host.manager._StdioMCPClient.connect", _noop_connect):
            client1 = await mgr._create_client(
                {
                    "command": sys.executable,
                    "args": ["${BYTEY_WORKSPACE}/server.py"],
                    "env": {"PROJECT_DIR": "${BYTEY_WORKSPACE}"},
                }
            )
            self.assertEqual(client1._args, ["/tmp/ws_alpha/server.py"])  # type: ignore[attr-defined]
            self.assertEqual(client1._env["PROJECT_DIR"], "/tmp/ws_alpha")  # type: ignore[attr-defined]

            mgr.set_workspace_path("/tmp/ws_beta")
            client2 = await mgr._create_client(
                {
                    "command": sys.executable,
                    "args": ["${BYTEY_WORKSPACE}/server.py"],
                    "env": {"PROJECT_DIR": "${BYTEY_WORKSPACE}"},
                }
            )
            self.assertEqual(client2._args, ["/tmp/ws_beta/server.py"])  # type: ignore[attr-defined]
            self.assertEqual(client2._env["PROJECT_DIR"], "/tmp/ws_beta")  # type: ignore[attr-defined]

    async def test_unknown_workspace_tool_returns_error_dict(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ws = root / "ws"
            ws.mkdir()
            daemon = ByteyDaemon(
                host="127.0.0.1",
                port=0,
                config_dir=root / "cfg",
                data_dir=root / "data",
                llm_router=_MockLLMRouter(),
            )
            daemon.config_manager.update_config({"workspace": {"active_path": str(ws)}})

            async def _deny(*_a: Any, **_k: Any) -> bool:
                return False

            res = await daemon._execute_workspace_tool(
                "nonexistent_tool", {}, daemon.config_manager.config, _deny
            )
            self.assertIsInstance(res, dict)
            self.assertFalse(res["ok"])
            self.assertIn("Unknown workspace tool", res["error"])


class TestGodModeAndCLIAgents(unittest.IsolatedAsyncioTestCase):
    """Tests for AgentRegistry, AgentRunner, God Mode API, and bytey_agent__run."""

    async def test_registry_detect_build_argv_and_runner(self) -> None:
        from bytey_ai_companion.agents import AgentRegistry, AgentRunner
        from bytey_ai_companion.config import AgentsConfig, ExternalAgentOverride

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_claude = root / "claude"
            fake_claude.write_text(
                "#!/usr/bin/env python3\n"
                "import sys\n"
                "if '--version' in sys.argv:\n"
                "    print('claude 1.2.3')\n"
                "    sys.exit(0)\n"
                "print('ARGS:' + '|'.join(sys.argv[1:]))\n",
                encoding="utf-8",
            )
            fake_claude.chmod(0o755)

            cfg = AgentsConfig(
                overrides={
                    "claude": ExternalAgentOverride(
                        enabled=True,
                        model="sonnet",
                        permission_mode="accept_edits",
                        custom_path=str(fake_claude),
                    )
                }
            )
            reg = AgentRegistry()
            with patch("bytey_ai_companion.agents.registry._resolve_binary", side_effect=lambda b, c="": c if c else ""):
                agents = await reg.detect_all(cfg)

            by_id = {a.id: a for a in agents}
            self.assertTrue(by_id["claude"].installed)
            self.assertEqual(by_id["claude"].version, "claude 1.2.3")
            self.assertTrue(by_id["claude"].enabled)
            self.assertFalse(by_id["copilot"].installed)
            self.assertFalse(by_id["copilot"].enabled)

            argv = reg.build_argv("claude", "build feature x", cfg)
            self.assertIn("--model", argv)
            self.assertIn("sonnet", argv)
            self.assertIn("--permission-mode", argv)
            self.assertIn("acceptEdits", argv)
            self.assertIn("build feature x", argv)

            # autonomous mode without allow_autonomous_flags=True must raise
            cfg_auto = AgentsConfig(
                overrides={
                    "claude": ExternalAgentOverride(
                        enabled=True,
                        permission_mode="autonomous",
                        allow_autonomous_flags=False,
                        custom_path=str(fake_claude),
                    )
                }
            )
            reg.sync_config(cfg_auto)
            with self.assertRaises(ValueError):
                reg.build_argv("claude", "hi", cfg_auto)

            runner = AgentRunner()
            res = await runner.run("claude", argv, root, timeout_seconds=10)
            self.assertEqual(res.exit_code, 0)
            self.assertIn("ARGS:-p|build feature x|--output-format|text|--model|sonnet", res.stdout)

            # Active output resets inactivity timer (total runtime > timeout_seconds does not time out)
            active_cli = root / "active_cli.py"
            active_cli.write_text(
                "import sys, time\n"
                "for i in range(4):\n"
                "    print(f'step {i}', flush=True)\n"
                "    time.sleep(0.25)\n",
                encoding="utf-8",
            )
            res_active = await runner.run(
                "claude", [sys.executable, str(active_cli)], root, timeout_seconds=0.6
            )
            self.assertFalse(res_active.timed_out)
            self.assertEqual(res_active.exit_code, 0)
            self.assertIn("step 3", res_active.stdout)

            # Silent/hung CLI does time out after inactivity limit
            silent_cli = root / "silent_cli.py"
            silent_cli.write_text("import time\ntime.sleep(5)\n", encoding="utf-8")
            res_silent = await runner.run(
                "claude", [sys.executable, str(silent_cli)], root, timeout_seconds=0.4
            )
            self.assertTrue(res_silent.timed_out)

    async def test_runner_bounds_output_during_read_and_streams_tail(self) -> None:
        import tracemalloc
        from bytey_ai_companion.agents import AgentRunner

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            noisy_cli = root / "noisy_cli.py"
            noisy_cli.write_text(
                "import sys, time\n"
                "for i in range(1000):\n"
                "    print(f'line_{i:04d}_' + ('x' * 4000), flush=True)\n"
                "time.sleep(0.7)\n"
                "print('POST_LIMIT_TAIL_MARKER', flush=True)\n",
                encoding="utf-8",
            )
            tails: list[str] = []

            async def _on_tail(tail: str) -> None:
                tails.append(tail)

            runner = AgentRunner()
            tracemalloc.start()
            try:
                res = await runner.run(
                    "claude",
                    [sys.executable, str(noisy_cli)],
                    root,
                    timeout_seconds=5,
                    max_output_chars=120,
                    on_output_tail=_on_tail,
                )
                _cur, peak = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()

            self.assertEqual(res.exit_code, 0)
            self.assertIn("... [stdout truncated]", res.stdout)
            self.assertLessEqual(len(res.stdout), 160)
            # Peak traced memory stays well below the ~4MB emitted stdout
            self.assertLess(peak, 2 * 1024 * 1024)
            self.assertTrue(any("POST_LIMIT_TAIL_MARKER" in t for t in tails))

    async def test_runner_task_cancellation_terminates_process_group(self) -> None:
        from bytey_ai_companion.agents import AgentRunner

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pid_file = root / "pids.txt"
            long_cli = root / "long_cli.py"
            long_cli.write_text(
                "import os, signal, subprocess, sys, time\n"
                "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
                "def stop(signum, frame):\n"
                "    child.wait()\n"
                "    raise SystemExit(0)\n"
                "signal.signal(signal.SIGTERM, stop)\n"
                f"with open({str(pid_file)!r}, 'w', encoding='utf-8') as fh:\n"
                "    fh.write(f'{os.getpid()},{child.pid}')\n"
                "time.sleep(30)\n",
                encoding="utf-8",
            )
            runner = AgentRunner()
            task = asyncio.create_task(
                runner.run("claude", [sys.executable, str(long_cli)], root, timeout_seconds=30)
            )
            pids: list[int] = []
            try:
                for _ in range(50):
                    if pid_file.exists():
                        raw = pid_file.read_text(encoding="utf-8").strip()
                        if "," in raw:
                            pids = [int(p) for p in raw.split(",")]
                            break
                    await asyncio.sleep(0.05)
                self.assertEqual(len(pids), 2)

                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task

                self.assertFalse(runner.is_running)
                for p in pids:
                    with self.assertRaises(ProcessLookupError):
                        os.kill(p, 0)
            finally:
                for p in pids:
                    try:
                        os.kill(p, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    except Exception:
                        pass

    async def test_runner_decodes_split_utf8_incrementally(self) -> None:
        from bytey_ai_companion.agents import AgentRunner

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # Emit 4-byte UTF-8 emoji split exactly across a 4096-byte read boundary
            cli = root / "split_utf8.py"
            cli.write_text(
                "import sys\n"
                "payload = b'a' * 4094 + '🚀'.encode('utf-8') + b'done'\n"
                "sys.stdout.buffer.write(payload)\n"
                "sys.stdout.buffer.flush()\n",
                encoding="utf-8",
            )
            runner = AgentRunner()
            res = await runner.run("claude", [sys.executable, str(cli)], root, timeout_seconds=5)
            self.assertEqual(res.exit_code, 0)
            self.assertNotIn("\ufffd", res.stdout)
            self.assertIn("🚀done", res.stdout)

    async def test_disabled_custom_cli_honored_in_detect_and_sync(self) -> None:
        from bytey_ai_companion.agents import AgentRegistry
        from bytey_ai_companion.config import AgentsConfig, CustomCLIAgentConfig, ExternalAgentOverride

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tool = root / "custom_tool"
            tool.write_text("#!/bin/sh\necho 'custom 1.0'\n", encoding="utf-8")
            tool.chmod(0o755)

            reg = AgentRegistry()
            cfg = AgentsConfig(
                custom_agents=[
                    CustomCLIAgentConfig(
                        id="custom1", display_name="Custom 1", command=str(tool), enabled=False
                    )
                ]
            )
            detected = {a.id: a for a in await reg.detect_all(cfg)}
            self.assertTrue(detected["custom1"].installed)
            self.assertFalse(detected["custom1"].enabled)

            synced = {a.id: a for a in reg.sync_config(cfg)}
            self.assertFalse(synced["custom1"].enabled)

            # Explicit per-agent override takes precedence
            cfg.overrides["custom1"] = ExternalAgentOverride(enabled=True)
            synced_ov = {a.id: a for a in reg.sync_config(cfg)}
            self.assertTrue(synced_ov["custom1"].enabled)

    async def test_sync_config_reresolves_when_custom_path_cleared_or_command_changed(self) -> None:
        from bytey_ai_companion.agents import AgentRegistry
        from bytey_ai_companion.config import AgentsConfig, CustomCLIAgentConfig, ExternalAgentOverride

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bin_a = root / "tool_a"
            bin_b = root / "tool_b"
            for p in (bin_a, bin_b):
                p.write_text("#!/bin/sh\necho 'tool 1.0'\n", encoding="utf-8")
                p.chmod(0o755)

            reg = AgentRegistry()
            cfg = AgentsConfig(
                overrides={"claude": ExternalAgentOverride(custom_path=str(bin_a))},
                custom_agents=[
                    CustomCLIAgentConfig(id="mycli", display_name="My CLI", command=str(bin_a))
                ],
            )
            detected = {a.id: a for a in await reg.detect_all(cfg)}
            self.assertEqual(detected["claude"].resolved_path, str(bin_a))
            self.assertEqual(detected["mycli"].resolved_path, str(bin_a))

            # Clearing custom_path re-resolves back to default binary (not stuck on bin_a)
            cfg.overrides["claude"] = ExternalAgentOverride(custom_path="")
            # Changing custom agent command re-resolves to bin_b
            cfg.custom_agents[0] = CustomCLIAgentConfig(
                id="mycli", display_name="My CLI", command=str(bin_b)
            )
            synced = {a.id: a for a in reg.sync_config(cfg)}
            self.assertNotEqual(synced["claude"].resolved_path, str(bin_a))
            self.assertEqual(synced["mycli"].resolved_path, str(bin_b))

    async def test_collect_git_status_timeout_kills_and_reaps_process(self) -> None:
        from unittest.mock import patch
        from bytey_ai_companion.agents import runner as runner_mod

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pid_file = root / "git.pid"
            orig_exec = asyncio.create_subprocess_exec

            async def _fake_exec(*_args: Any, **_kwargs: Any) -> asyncio.subprocess.Process:
                return await orig_exec(
                    sys.executable,
                    "-c",
                    f"import os, time; open({str(pid_file)!r}, 'w').write(str(os.getpid())); time.sleep(30)",
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                )

            with patch.object(runner_mod.asyncio, "create_subprocess_exec", side_effect=_fake_exec):
                out = await runner_mod._collect_git_status(root, timeout_s=0.15)
            self.assertEqual(out, "")
            self.assertTrue(pid_file.exists())
            pid = int(pid_file.read_text(encoding="utf-8").strip())
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)

    async def test_daemon_god_mode_routes_slash_command_and_run_tool(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ws = root / "ws"
            ws.mkdir()
            fake_claude = root / "claude"
            fake_claude.write_text(
                "#!/usr/bin/env python3\n"
                "import sys\n"
                "if '--version' in sys.argv:\n"
                "    print('claude 9.9.9')\n"
                "    sys.exit(0)\n"
                "print('CLAUDE_DONE:' + ' '.join(sys.argv[1:]))\n",
                encoding="utf-8",
            )
            fake_claude.chmod(0o755)

            mock_llm = _MockLLMRouter()
            daemon = ByteyDaemon(
                host="127.0.0.1",
                port=0,
                config_dir=root / "cfg",
                data_dir=root / "data",
                enable_voice_capture=False,
                llm_router=mock_llm,  # type: ignore[arg-type]
            )
            daemon.config_manager.update_config(
                {
                    "voice_out": {"tts_muted": True, "tts_provider": "disabled"},
                    "workspace": {"active_path": str(ws)},
                    "agents": {
                        "require_run_approval": False,
                        "overrides": {
                            "claude": {
                                "enabled": True,
                                "model": "opus",
                                "custom_path": str(fake_claude),
                            }
                        },
                    },
                }
            )
            await daemon.start()
            base_url = f"http://127.0.0.1:{daemon.ipc.port}"
            try:
                async with httpx.AsyncClient(base_url=base_url, timeout=5.0) as client:
                    # 1. /api/v1/agents lists all supported CLIs
                    r_list = await client.get("/api/v1/agents")
                    self.assertEqual(r_list.status_code, 200)
                    agents_data = {a["id"]: a for a in r_list.json()["agents"]}
                    self.assertIn("claude", agents_data)
                    self.assertIn("copilot", agents_data)
                    self.assertTrue(agents_data["claude"]["installed"])
                    self.assertEqual(agents_data["claude"]["model"], "opus")

                    # 2. Cannot enable a non-installed CLI
                    if not agents_data["agy"]["installed"]:
                        r_bad = await client.patch("/api/v1/agents/agy", json={"enabled": True})
                        self.assertEqual(r_bad.status_code, 400)

                    # 3. PATCH model update works
                    r_patch = await client.patch("/api/v1/agents/claude", json={"model": "sonnet"})
                    self.assertEqual(r_patch.status_code, 200)
                    self.assertEqual(r_patch.json()["agent"]["model"], "sonnet")

                # 4. /god toggles God Mode
                res_cmd = await daemon.handle_user_prompt("/god on", input_mode="text")
                self.assertIn("God Mode enabled", res_cmd["assistant_message"]["content"])
                self.assertTrue(daemon.config_manager.config.agents.god_mode_enabled)

                # 5. bytey_agent__run executes non-interactively
                tool_res = await daemon._execute_agent_tool(
                    "run",
                    {"agent": "claude", "prompt": "implement ping"},
                    daemon.config_manager.config,
                    confirm_cb=None,
                    call_id="c1",
                    assistant_id="a1",
                )
                self.assertTrue(tool_res["ok"])
                payload = json.loads(tool_res["content"])
                self.assertEqual(payload["exit_code"], 0)
                self.assertIn("CLAUDE_DONE:", payload["stdout"])
                self.assertIn("sonnet", payload["stdout"])

                # 6. Auto-Accept bypasses require_run_approval in God Mode
                daemon.config_manager.update_config(
                    {
                        "agents": {"require_run_approval": True},
                        "workspace": {
                            "require_write_approval": False,
                            "require_command_approval": False,
                        },
                    }
                )
                confirm_called = False

                async def _should_not_confirm(*_args: Any, **_kwargs: Any) -> bool:
                    nonlocal confirm_called
                    confirm_called = True
                    return False

                tool_res_auto = await daemon._execute_agent_tool(
                    "run",
                    {"agent": "claude", "prompt": "auto accept run"},
                    daemon.config_manager.config,
                    confirm_cb=_should_not_confirm,
                    call_id="c2",
                    assistant_id="a2",
                )
                self.assertFalse(confirm_called)
                self.assertTrue(tool_res_auto["ok"])
            finally:
                await daemon.stop()

    async def test_cancel_prompt_rejects_pending_confirmations_and_stops_agent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ws = root / "ws"
            ws.mkdir()
            long_cli = root / "long_cli.py"
            long_cli.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")

            daemon = ByteyDaemon(
                host="127.0.0.1",
                port=0,
                config_dir=root / "cfg",
                data_dir=root / "data",
                llm_router=_MockLLMRouter(),
            )
            await daemon.start()
            try:
                sid = await daemon._ensure_active_session()
                msg = await daemon.storage.add_message(sid, role="assistant", content="working")
                await daemon.storage.add_tool_call(
                    message_id=msg["id"],
                    server_name="bytey_workspace",
                    tool_name="write_file",
                    arguments_json="{}",
                    status="running",
                    call_id="c_pending",
                )

                confirm_task = asyncio.create_task(
                    daemon._request_tool_confirmation(
                        "c_pending",
                        msg["id"],
                        "bytey_workspace",
                        "write_file",
                        {"rel_path": "a.txt"},
                    )
                )
                for _ in range(50):
                    if "c_pending" in daemon._pending_confirmations:
                        break
                    await asyncio.sleep(0.01)
                self.assertIn("c_pending", daemon._pending_confirmations)

                agent_task = asyncio.create_task(
                    daemon.agent_runner.run(
                        "claude", [sys.executable, str(long_cli)], ws, timeout_seconds=30
                    )
                )
                for _ in range(50):
                    if daemon.agent_runner.is_running:
                        break
                    await asyncio.sleep(0.01)
                self.assertTrue(daemon.agent_runner.is_running)

                cancel_res = await daemon._route_cancel_prompt(None)  # type: ignore[arg-type]
                self.assertTrue(cancel_res["ok"])
                self.assertEqual(cancel_res["rejected_confirmations"], 1)
                self.assertTrue(cancel_res["agent_cancelled"])
                self.assertFalse(await confirm_task)

                agent_res = await agent_task
                self.assertTrue(agent_res.cancelled)
                self.assertFalse(daemon.agent_runner.is_running)

                # Subsequent confirmation requests while cancelled must immediately reject
                self.assertFalse(
                    await daemon._request_tool_confirmation(
                        "c_after",
                        msg["id"],
                        "bytey_workspace",
                        "write_file",
                        {"rel_path": "b.txt"},
                    )
                )
            finally:
                await daemon.stop()

    async def test_probe_version_timeout_terminates_and_reaps_process(self) -> None:
        from bytey_ai_companion.agents.registry import _probe_version

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pid_file = root / "probe.pid"
            hung_probe = root / "hung_probe.py"
            hung_probe.write_text(
                "import os, time\n"
                f"with open({str(pid_file)!r}, 'w', encoding='utf-8') as fh:\n"
                "    fh.write(str(os.getpid()))\n"
                "time.sleep(30)\n",
                encoding="utf-8",
            )
            version = await _probe_version(sys.executable, (str(hung_probe),))
            self.assertEqual(version, "")
            self.assertTrue(pid_file.exists())
            pid = int(pid_file.read_text(encoding="utf-8").strip())
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)


class TestGitToolchain(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp_dir.name)
        self.git = None

    async def asyncTearDown(self) -> None:
        self.tmp_dir.cleanup()

    async def _init_configured_repo(self):
        from bytey_ai_companion.workspace.git_tools import GitToolchain

        git = GitToolchain(self.root)
        init_res = await git.init(default_branch="main")
        self.assertTrue(init_res["ok"], init_res)
        for key, val in (("user.name", "Bytey Test"), ("user.email", "bytey@example.com")):
            proc = await asyncio.create_subprocess_exec(
                "git", "-C", str(self.root), "config", key, val
            )
            await proc.wait()
        return git

    async def test_git_lifecycle_operations(self) -> None:
        from bytey_ai_companion.workspace.git_tools import GitToolchain

        git = GitToolchain(self.root)
        self.assertFalse(await git.is_repo())
        non_repo = await git.status()
        self.assertFalse(non_repo["ok"])
        self.assertIn("Not a git repository", non_repo["error"])

        git = await self._init_configured_repo()
        self.assertTrue(await git.is_repo())

        (self.root / "app.py").write_text("print('v1')\n", encoding="utf-8")
        st = await git.status()
        self.assertTrue(st["ok"])
        self.assertIn("app.py", st["content"])

        self.assertTrue((await git.stage(paths=["app.py"]))["ok"])
        c1 = await git.commit("initial commit")
        self.assertTrue(c1["ok"], c1)

        log_res = await git.log(limit=5)
        self.assertTrue(log_res["ok"])
        self.assertIn("initial commit", log_res["content"])

        show_res = await git.show("HEAD", stat_only=True)
        self.assertTrue(show_res["ok"])
        self.assertIn("app.py", show_res["content"])

        blame_res = await git.blame("app.py", start_line=1, end_line=1)
        self.assertTrue(blame_res["ok"])
        self.assertIn("Bytey Test", blame_res["content"])

        (self.root / "app.py").write_text("print('v2')\n", encoding="utf-8")
        diff_res = await git.diff(rel_path="app.py")
        self.assertTrue(diff_res["ok"])
        self.assertIn("+print('v2')", diff_res["content"])

        # Stash push & pop
        self.assertTrue((await git.stash("push", message="wip"))["ok"])
        self.assertIn("wip", (await git.stash_list())["content"])
        self.assertTrue((await git.stash("pop"))["ok"])

        # Restore working tree
        self.assertTrue((await git.restore(["app.py"]))["ok"])
        self.assertEqual((self.root / "app.py").read_text(encoding="utf-8"), "print('v1')\n")

        # Branch create, commit, merge, delete, tag
        self.assertTrue((await git.branch_create("feat"))["ok"])
        (self.root / "feat.txt").write_text("feature\n", encoding="utf-8")
        self.assertTrue((await git.stage(all_files=True))["ok"])
        self.assertTrue((await git.commit("add feat"))["ok"])
        self.assertIn("feat", (await git.branch_list(include_remote=False))["content"])

        self.assertTrue((await git.checkout("main"))["ok"])
        self.assertTrue((await git.merge("feat", no_ff=True))["ok"])
        self.assertTrue((await git.branch_delete("feat"))["ok"])

        self.assertTrue((await git.tag_create("v1.0.0", message="release"))["ok"])
        self.assertIn("v1.0.0", (await git.tag_list())["content"])

    async def test_path_traversal_rejected(self) -> None:
        git = await self._init_configured_repo()
        with self.assertRaises(ValueError):
            await git.diff(rel_path="../outside.txt")
        with self.assertRaises(ValueError):
            await git.stage(paths=["/etc/passwd"])

    async def test_large_output_bounded_and_drained(self) -> None:
        from bytey_ai_companion.workspace.git_tools import MAX_GIT_BYTES

        git = await self._init_configured_repo()
        big_file = self.root / "big.txt"
        big_file.write_text(("A" * 100 + "\n") * 800, encoding="utf-8")
        self.assertTrue((await git.stage(paths=["big.txt"]))["ok"])
        diff_res = await git.diff(staged=True)
        self.assertTrue(diff_res["ok"])
        self.assertIn("[Truncated at 32 KiB]", diff_res["content"])
        self.assertLessEqual(len(diff_res["content"].encode("utf-8")), MAX_GIT_BYTES + 128)

    async def test_subdirectory_of_parent_repo_is_rejected(self) -> None:
        from bytey_ai_companion.workspace.git_tools import GitToolchain

        await self._init_configured_repo()
        sibling = self.root / "sibling.txt"
        sibling.write_text("keep\n", encoding="utf-8")
        sub = self.root / "sub"
        sub.mkdir()
        (sub / "local.txt").write_text("sub\n", encoding="utf-8")

        sub_git = GitToolchain(sub)
        self.assertFalse(await sub_git.is_repo())
        st_res = await sub_git.status()
        self.assertFalse(st_res["ok"])
        self.assertIn("Not a git repository", st_res["error"])
        stage_res = await sub_git.stage(all_files=True)
        self.assertFalse(stage_res["ok"])
        self.assertEqual(sibling.read_text(encoding="utf-8"), "keep\n")

    async def test_option_shaped_git_args_rejected(self) -> None:
        git = await self._init_configured_repo()
        victim = self.root / "victim.txt"
        with self.assertRaises(ValueError):
            await git.diff(ref=f"--output={victim}")
        with self.assertRaises(ValueError):
            await git.log(ref="--format=%H", author="--all")
        with self.assertRaises(ValueError):
            await git.show(ref="--output=/tmp/evil")
        with self.assertRaises(ValueError):
            await git.checkout(ref="-f")
        with self.assertRaises(ValueError):
            await git.push(remote="--receive-pack=evil")
        self.assertFalse(victim.exists())

    async def test_blame_short_file_clamps_default_end_line(self) -> None:
        git = await self._init_configured_repo()
        short_file = self.root / "short.py"
        short_file.write_text("line1\nline2\nline3\n", encoding="utf-8")
        self.assertTrue((await git.stage(paths=["short.py"]))["ok"])
        self.assertTrue((await git.commit("add short"))["ok"])

        blame_res = await git.blame("short.py")
        self.assertTrue(blame_res["ok"], blame_res)
        self.assertIn("line3", blame_res["content"])

    async def test_blame_read_error_returns_error(self) -> None:
        from unittest.mock import patch

        git = await self._init_configured_repo()
        locked = self.root / "locked.py"
        locked.write_text("secret\n", encoding="utf-8")
        self.assertTrue((await git.stage(paths=["locked.py"]))["ok"])
        self.assertTrue((await git.commit("add locked"))["ok"])

        with patch.object(git, "_count_lines_up_to", side_effect=PermissionError("Permission denied")):
            res = await git.blame("locked.py")
        self.assertFalse(res["ok"])
        self.assertIn("Permission denied", res["error"])


class TestGitDaemonIntegration(unittest.IsolatedAsyncioTestCase):
    async def test_mcp_host_and_execute_git_tool_confirmation(self) -> None:
        from bytey_ai_companion.main import ByteyDaemon

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "ws"
            cfg_dir = Path(tmp) / "cfg"
            data_dir = Path(tmp) / "data"
            root.mkdir()

            daemon = ByteyDaemon(
                host="127.0.0.1",
                port=0,
                config_dir=cfg_dir,
                data_dir=data_dir,
                enable_voice_capture=False,
                llm_router=_MockLLMRouter(),
            )
            daemon.config_manager.update_config(
                {
                    "workspace": {
                        "active_path": str(root),
                        "require_write_approval": True,
                    }
                }
            )
            await daemon.start()
            try:
                base_url = f"http://127.0.0.1:{daemon.ipc.port}"
                async with httpx.AsyncClient(base_url=base_url) as client:
                    resp = await client.get("/api/v1/mcp/host")
                    self.assertEqual(resp.status_code, 200)
                    names = {t["name"] for t in resp.json()["tools"]}
                    for expected in ("bytey_git__status", "bytey_git__commit", "bytey_git__push"):
                        self.assertIn(expected, names)

                cfg = daemon.config_manager.config

                async def deny_cb(*_args: Any, **_kwargs: Any) -> bool:
                    return False

                async def allow_cb(*_args: Any, **_kwargs: Any) -> bool:
                    return True

                # Init + stage + commit with confirmation
                init_res = await daemon._execute_git_tool("init", {"default_branch": "main"}, cfg, allow_cb)
                self.assertTrue(init_res["ok"], init_res)
                for key, val in (("user.name", "Bytey"), ("user.email", "bytey@example.com")):
                    proc = await asyncio.create_subprocess_exec(
                        "git", "-C", str(root), "config", key, val
                    )
                    await proc.wait()

                (root / "readme.md").write_text("hello\n", encoding="utf-8")
                st_res = await daemon._execute_git_tool("status", {}, cfg, deny_cb)
                self.assertTrue(st_res["ok"])
                self.assertIn("readme.md", st_res["content"])

                denied_stage = await daemon._execute_git_tool("stage", {"all": True}, cfg, deny_cb)
                self.assertFalse(denied_stage["ok"])
                self.assertTrue(denied_stage.get("denied"))

                ok_stage = await daemon._execute_git_tool("stage", {"all": True}, cfg, allow_cb)
                self.assertTrue(ok_stage["ok"], ok_stage)
                ok_commit = await daemon._execute_git_tool("commit", {"message": "init"}, cfg, allow_cb)
                self.assertTrue(ok_commit["ok"], ok_commit)
            finally:
                await daemon.stop()


class TestInterleavedMessageParts(unittest.IsolatedAsyncioTestCase):
    async def test_storage_persists_parts_and_falls_back_for_legacy_rows(self) -> None:
        from bytey_ai_companion.storage import StorageManager

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "bytey.db"
            storage = StorageManager(db_path)
            await storage.init_db()
            try:
                sess = await storage.create_session("Parts Test")
                sid = sess["id"]

                # Interleaved parts persisted and returned in order
                msg = await storage.add_message(
                    sid,
                    role="assistant",
                    content="Before\n\nAfter",
                    parts=[
                        {"type": "text", "text": "Before"},
                        {"type": "tool_call", "call_id": "call-1"},
                        {"type": "text", "text": "After"},
                    ],
                )
                await storage.add_tool_call(
                    message_id=msg["id"],
                    server_name="srv",
                    tool_name="ping",
                    arguments_json="{}",
                    status="ok",
                    call_id="call-1",
                )
                listed = await storage.list_messages(sid)
                self.assertEqual(len(listed), 1)
                self.assertEqual(
                    listed[0]["parts"],
                    [
                        {"type": "text", "text": "Before"},
                        {"type": "tool_call", "call_id": "call-1"},
                        {"type": "text", "text": "After"},
                    ],
                )

                # Legacy row (empty parts_json) reconstructs [text, ...tool_calls]
                legacy_msg = await storage.add_message(
                    sid, role="assistant", content="Legacy body"
                )
                await storage.add_tool_call(
                    message_id=legacy_msg["id"],
                    server_name="srv",
                    tool_name="old_tool",
                    arguments_json="{}",
                    status="ok",
                    call_id="call-legacy",
                )
                listed2 = await storage.list_messages(sid)
                self.assertEqual(
                    listed2[1]["parts"],
                    [
                        {"type": "text", "text": "Legacy body"},
                        {"type": "tool_call", "call_id": "call-legacy"},
                    ],
                )
            finally:
                await storage.close()

    async def test_prompt_turn_interleaves_text_and_tool_calls(self) -> None:
        class _TwoRoundToolRouter:
            def __init__(self) -> None:
                self.calls = 0

            async def generate_chat(self, **kwargs: Any) -> dict[str, Any]:
                on_text_chunk = kwargs.get("on_text_chunk")
                self.calls += 1
                if self.calls == 1:
                    if on_text_chunk:
                        await on_text_chunk("[emotion:curious] Checking status...")
                    return {
                        "content": "[emotion:curious] Checking status...",
                        "emotion": "curious",
                        "tool_calls": [
                            {
                                "id": "tc-1",
                                "name": "bytey_workspace__list_files",
                                "arguments": {"path": "."},
                            }
                        ],
                        "raw_assistant_message": {
                            "role": "assistant",
                            "content": "Checking status...",
                        },
                    }
                if on_text_chunk:
                    await on_text_chunk("Done checking!")
                return {
                    "content": "Done checking!",
                    "emotion": "happy",
                    "tool_calls": [],
                    "raw_assistant_message": {
                        "role": "assistant",
                        "content": "Done checking!",
                    },
                }

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            ws = tmp / "ws"
            ws.mkdir()
            (ws / "a.txt").write_text("hi", encoding="utf-8")
            daemon = ByteyDaemon(
                host="127.0.0.1",
                port=0,
                config_dir=tmp / "cfg",
                data_dir=tmp / "data",
                enable_voice_capture=False,
                llm_router=_TwoRoundToolRouter(),
            )
            daemon.config_manager.update_config(
                {
                    "workspace": {"active_path": str(ws), "require_write_approval": False},
                    "voice_out": {"tts_provider": "disabled"},
                }
            )
            broadcasts: list[dict[str, Any]] = []
            orig_broadcast = daemon.ipc.connection_manager.broadcast

            async def _capture_broadcast(payload: dict[str, Any]) -> None:
                broadcasts.append(payload)
                await orig_broadcast(payload)

            daemon.ipc.connection_manager.broadcast = _capture_broadcast  # type: ignore[method-assign]
            daemon.ipc.port = 0
            await daemon.start()
            try:
                sess = await daemon.storage.get_or_create_active_session()
                await daemon.handle_user_prompt(
                    "list files", session_id=sess["id"], input_mode="text"
                )
                msgs = await daemon.storage.list_messages(sess["id"])
                assistant = [m for m in msgs if m["role"] == "assistant"][-1]
                self.assertEqual(len(assistant["tool_calls"]), 1)
                call_id = assistant["tool_calls"][0]["id"]
                self.assertEqual(
                    assistant["parts"],
                    [
                        {"type": "text", "text": "Checking status..."},
                        {"type": "tool_call", "call_id": call_id},
                        {"type": "text", "text": "Done checking!"},
                    ],
                )
                final_deltas = [
                    b for b in broadcasts
                    if b.get("type") == "transcript_delta"
                    and b.get("is_final") is True
                    and b.get("role") == "assistant"
                ]
                self.assertEqual(len(final_deltas), 1)
                self.assertEqual(final_deltas[0]["parts"], assistant["parts"])
            finally:
                await daemon.stop()


class TestAttachments(unittest.IsolatedAsyncioTestCase):
    async def test_text_and_image_attachments_stored_per_session_and_cleaned_up(self) -> None:
        from bytey_ai_companion.llm.router import LLMRouter

        captured_messages: list[list[dict[str, Any]]] = []

        class _CaptureRouter(LLMRouter):
            async def generate_chat(self, **kwargs: Any) -> dict[str, Any]:
                captured_messages.append(list(kwargs.get("messages") or []))
                on_text_chunk = kwargs.get("on_text_chunk")
                if on_text_chunk:
                    await on_text_chunk("Got your files.")
                return {"content": "Got your files.", "emotion": "happy", "tool_calls": []}

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            src_txt = tmp / "notes.py"
            src_txt.write_text("print('hello attachment')\n", encoding="utf-8")
            # Minimal 1x1 PNG bytes
            png_bytes = (
                b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
                b"\x08\x06\x00\x00\x00\x1f\x15c4\x00\x00\x00\nIDATx\x9cc\x00\x01\x00"
                b"\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
            )
            src_img = tmp / "shot.png"
            src_img.write_bytes(png_bytes)

            data_dir = tmp / "data"
            daemon = ByteyDaemon(
                host="127.0.0.1",
                port=0,
                config_dir=tmp / "cfg",
                data_dir=data_dir,
                enable_voice_capture=False,
                llm_router=_CaptureRouter(),
            )
            daemon.config_manager.update_config({"voice_out": {"tts_provider": "disabled"}})
            await daemon.start()
            try:
                sess = await daemon.storage.get_or_create_active_session()
                sid = sess["id"]
                await daemon.handle_user_prompt(
                    "Check these",
                    session_id=sid,
                    input_mode="text",
                    attachments=[
                        {"path": str(src_txt), "name": "notes.py"},
                        {"path": str(src_img), "name": "shot.png"},
                    ],
                )
                att_dir = daemon._attachments_dir(sid)
                self.assertTrue(att_dir.is_dir())
                stored_files = list(att_dir.iterdir())
                self.assertEqual(len(stored_files), 2)

                msgs = await daemon.storage.list_messages(sid)
                user_msg = [m for m in msgs if m["role"] == "user"][-1]
                att_parts = [p for p in user_msg["parts"] if p["type"] == "attachment"]
                self.assertEqual(len(att_parts), 2)
                self.assertEqual({p["name"] for p in att_parts}, {"notes.py", "shot.png"})

                self.assertEqual(len(captured_messages), 1)
                last_u = [m for m in captured_messages[0] if m["role"] == "user"][-1]
                self.assertIn("print('hello attachment')", last_u["content"])
                self.assertEqual(len(last_u.get("images") or []), 1)
                self.assertEqual(last_u["images"][0]["mime_type"], "image/png")

                # Deleting session cleans up attachments directory
                base_url = f"http://127.0.0.1:{daemon.ipc.port}"
                async with httpx.AsyncClient(base_url=base_url) as client:
                    del_resp = await client.delete(f"/api/v1/sessions/{sid}")
                    self.assertEqual(del_resp.status_code, 200)
                self.assertFalse(att_dir.exists())
            finally:
                await daemon.stop()


class TestVisualsConfigBounds(unittest.TestCase):
    def test_island_width_default_and_clamping(self) -> None:
        self.assertEqual(VisualsConfig().island_width, 140)
        self.assertEqual(VisualsConfig(island_width=50).island_width, 140)
        self.assertEqual(VisualsConfig(island_width=900).island_width, 450)
        self.assertEqual(VisualsConfig(island_width=240).island_width, 240)
        self.assertEqual(VisualsConfig().chat_opacity, 96)
        self.assertEqual(VisualsConfig(chat_opacity=2).chat_opacity, 10)
        self.assertEqual(VisualsConfig(chat_opacity=150).chat_opacity, 100)
        self.assertFalse(VisualsConfig().chat_sidebar_open)
        self.assertTrue(VisualsConfig(chat_sidebar_open=True).chat_sidebar_open)


class TestModelDiscoverySorting(unittest.IsolatedAsyncioTestCase):
    async def test_discover_models_sorts_case_insensitively_a_to_z(self) -> None:
        from bytey_ai_companion.llm.router import LLMRouter

        router = LLMRouter(ConfigManager())
        router._discover_ollama_models = unittest.mock.AsyncMock(
            return_value={
                "models": [
                    {"id": "qwen2.5:7b", "name": "qwen2.5:7b"},
                    {"id": "Llama3.2:3b", "name": "Llama3.2:3b"},
                    {"id": "gemma2:9b", "name": "gemma2:9b"},
                    {"id": "deepseek-r1:8b", "name": "deepseek-r1:8b"},
                ],
                "error": None,
            }
        )
        res = await router.discover_models(provider="ollama")
        ids = [m["id"] for m in res["models"]]
        self.assertEqual(
            ids,
            ["deepseek-r1:8b", "gemma2:9b", "Llama3.2:3b", "qwen2.5:7b"],
        )


class TestTokenUsageTracking(unittest.IsolatedAsyncioTestCase):
    async def test_usage_normalization_and_storage_roundtrip(self) -> None:
        from bytey_ai_companion.llm.router import (
            _extract_anthropic_usage,
            _extract_openai_usage,
            _normalize_usage,
        )
        from bytey_ai_companion.storage import StorageManager

        self.assertEqual(
            _normalize_usage(100, 50),
            {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
        )
        self.assertEqual(
            _extract_openai_usage({"prompt_tokens": 200, "completion_tokens": 40, "total_tokens": 240}),
            {"prompt_tokens": 200, "completion_tokens": 40, "total_tokens": 240},
        )
        self.assertEqual(
            _extract_anthropic_usage({"input_tokens": 100, "cache_read_input_tokens": 50, "output_tokens": 25}),
            {"prompt_tokens": 150, "completion_tokens": 25, "total_tokens": 175},
        )

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            storage = StorageManager(db_path=db_path)
            await storage.init_db()
            try:
                sess = await storage.create_session("Usage Test")
                msg = await storage.add_message(
                    sess["id"],
                    "assistant",
                    "initial",
                    usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
                )
                self.assertEqual(
                    msg["usage"],
                    {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
                )
                updated = await storage.update_message(
                    msg["id"],
                    content="final",
                    usage={"prompt_tokens": 50000, "completion_tokens": 4834, "total_tokens": 54834},
                )
                self.assertEqual(updated["usage"]["total_tokens"], 54834)
                listed = await storage.list_messages(sess["id"])
                self.assertEqual(listed[0]["usage"]["total_tokens"], 54834)
            finally:
                await storage.close()

    async def test_router_chat_returns_usage_and_daemon_broadcasts_it(self) -> None:
        from bytey_ai_companion.llm.router import LLMRouter

        router = LLMRouter(ConfigManager())
        router._chat_ollama = unittest.mock.AsyncMock(
            return_value=([], {"prompt_tokens": 1200, "completion_tokens": 300, "total_tokens": 1500})
        )
        res = await router.generate_chat(
            provider="ollama",
            messages=[{"role": "user", "content": "hi"}],
            system_prompt="",
            model="llama3.2",
        )
        self.assertEqual(
            res["usage"],
            {"prompt_tokens": 1200, "completion_tokens": 300, "total_tokens": 1500},
        )

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            daemon = ByteyDaemon(
                config_dir=tmp_path / "cfg",
                data_dir=tmp_path / "data",
                enable_voice_capture=False,
            )
            daemon.config_manager.update_config(
                {
                    "provider": {"active_provider": "ollama", "model": "llama3.2"},
                    "voice_out": {"tts_provider": "disabled", "tts_muted": True},
                }
            )
            daemon.llm_router.generate_chat = unittest.mock.AsyncMock(
                return_value={
                    "content": "hello",
                    "emotion": "happy",
                    "emotion_explicit": True,
                    "title": None,
                    "tool_calls": [],
                    "usage": {"prompt_tokens": 50000, "completion_tokens": 4834, "total_tokens": 54834},
                    "raw_assistant_message": {"role": "assistant", "content": "hello"},
                }
            )
            broadcasts: list[dict[str, Any]] = []
            orig_broadcast = daemon.ipc.connection_manager.broadcast

            async def _capture_broadcast(payload: dict[str, Any]) -> None:
                broadcasts.append(payload)
                await orig_broadcast(payload)

            daemon.ipc.connection_manager.broadcast = _capture_broadcast  # type: ignore[method-assign]
            daemon.ipc.port = 0
            await daemon.start()
            try:
                sess = await daemon.storage.create_session("Token Usage Session")
                await daemon.handle_user_prompt("hi", input_mode="text", session_id=sess["id"])
                finals = [
                    b for b in broadcasts
                    if b.get("type") == "transcript_delta"
                    and b.get("role") == "assistant"
                    and b.get("is_final") is True
                ]
                self.assertEqual(len(finals), 1)
                self.assertEqual(
                    finals[0].get("usage"),
                    {"prompt_tokens": 50000, "completion_tokens": 4834, "total_tokens": 54834},
                )
                msgs = await daemon.storage.list_messages(sess["id"])
                asst = [m for m in msgs if m["role"] == "assistant"][-1]
                self.assertEqual(asst["usage"]["total_tokens"], 54834)
            finally:
                await daemon.stop()


class TestFocusWindow(unittest.IsolatedAsyncioTestCase):
    async def test_provider_config_focus_window_validation(self) -> None:
        from bytey_ai_companion.config import ProviderConfig

        self.assertEqual(ProviderConfig().focus_window, 0)
        self.assertEqual(ProviderConfig(focus_window=None).focus_window, 0)
        self.assertEqual(ProviderConfig(focus_window="").focus_window, 0)
        self.assertEqual(ProviderConfig(focus_window=-5).focus_window, 0)
        self.assertEqual(ProviderConfig(focus_window="10").focus_window, 10)

    async def test_focus_window_limits_llm_history_without_deleting_db(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            daemon = ByteyDaemon(
                config_dir=tmp_path / "cfg",
                data_dir=tmp_path / "data",
                enable_voice_capture=False,
            )
            daemon.config_manager.update_config(
                {
                    "provider": {
                        "active_provider": "ollama",
                        "model": "llama3.2",
                        "focus_window": 2,
                    },
                    "voice_out": {"tts_provider": "disabled", "tts_muted": True},
                }
            )
            captured_messages: list[list[dict[str, Any]]] = []

            async def _fake_generate_chat(**kwargs: Any) -> dict[str, Any]:
                captured_messages.append(list(kwargs.get("messages") or []))
                on_chunk = kwargs.get("on_text_chunk")
                if callable(on_chunk):
                    await on_chunk("ok")
                return {
                    "content": "ok",
                    "emotion": "happy",
                    "emotion_explicit": True,
                    "title": None,
                    "tool_calls": [],
                    "raw_assistant_message": {"role": "assistant", "content": "ok"},
                }

            daemon.llm_router.generate_chat = _fake_generate_chat  # type: ignore[method-assign]
            daemon.ipc.port = 0
            await daemon.start()
            try:
                sess = await daemon.storage.create_session("Focus Window Session")
                sid = sess["id"]
                for i in range(6):
                    role = "user" if i % 2 == 0 else "assistant"
                    await daemon.storage.add_message(sid, role=role, content=f"msg-{i}")

                await daemon.handle_user_prompt("msg-current", input_mode="text", session_id=sid)
                self.assertEqual(len(captured_messages), 1)
                self.assertEqual(
                    [m["content"] for m in captured_messages[0]],
                    ["msg-4", "msg-5", "msg-current"],
                )

                # DB still retains all 6 seeded + user + assistant = 8 messages
                all_msgs = await daemon.storage.list_messages(sid, limit=None)
                self.assertEqual(len(all_msgs), 8)
                self.assertEqual(await daemon.storage.list_messages(sid, limit=0), [])

                # Setting focus_window=0 sends full history (> 40 messages)
                for i in range(45):
                    role = "user" if i % 2 == 0 else "assistant"
                    await daemon.storage.add_message(sid, role=role, content=f"extra-{i}")
                daemon.config_manager.update_config({"provider": {"focus_window": 0}})
                await daemon.handle_user_prompt("final-msg", input_mode="text", session_id=sid)
                self.assertEqual(len(captured_messages), 2)
                self.assertEqual(len(captured_messages[1]), 8 + 45 + 1)
                self.assertEqual(captured_messages[1][0]["content"], "msg-0")
                self.assertEqual(captured_messages[1][-1]["content"], "final-msg")
            finally:
                await daemon.stop()


if __name__ == "__main__":
    unittest.main()
