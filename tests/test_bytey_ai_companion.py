"""Comprehensive automated test suite for Bytey AI Companion."""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
import os
import py_compile
import re
import socket
import sqlite3
import stat
import struct
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import httpx
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
    ConfigManager,
    ProviderConfig,
    SecretStore,
    VisualsConfig,
    resolve_config_dir,
)
from bytey_ai_companion.llm.emotions import EmotionStreamParser, VALID_EMOTIONS  # noqa: E402
from bytey_ai_companion.llm.router import LLMRouter  # noqa: E402
from bytey_ai_companion.main import ByteyDaemon  # noqa: E402
from bytey_ai_companion.mcp_host import (  # noqa: E402
    MCPClientManager,
    from_mcp_servers_json,
    to_mcp_servers_json,
)
from bytey_ai_companion.storage import (  # noqa: E402
    DEFAULT_DB_FILENAME,
    LEGACY_DB_FILENAME,
    StorageManager,
    resolve_data_dir,
    resolve_db_path,
)
from bytey_ai_companion.tts.chunker import SentenceChunker  # noqa: E402
from bytey_settings.api_client import DaemonClient, resolve_local_config_path  # noqa: E402
from bytey_settings.window import (  # noqa: E402
    POSITION_MODE_OPTIONS as SETTINGS_POSITION_MODE_OPTIONS,
    ByteyPreferencesWindow,
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
            tool_name = tools[0]["function"]["name"]
            tc = {
                "id": "call_mock_1",
                "name": tool_name,
                "arguments": {"text": "daemon-ok"},
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

    def test_secret_store_env_permissions_redaction_and_expansion(self) -> None:
        store = SecretStore(config_dir=self.cfg_dir)
        secret_val = "sk-super-secret-value-9988"
        store.set_secret("OPENAI_API_KEY", secret_val)

        self.assertTrue(store.env_file.is_file())
        mode = stat.S_IMODE(store.env_file.stat().st_mode)
        self.assertEqual(mode, 0o600)
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
        self.assertEqual(stat.S_IMODE(store.env_file.stat().st_mode), 0o600)

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


class TestSchemaSyncAndMCP(unittest.IsolatedAsyncioTestCase):
    """Test schema_sync round-trips and MCPClientManager against a real stdio server."""

    async def asyncSetUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.server_script = Path(self.tmp_dir.name) / "mock_mcp.py"
        self.server_script.write_text(MOCK_MCP_SERVER_SCRIPT, encoding="utf-8")
        self.secret_store = SecretStore(config_dir=Path(self.tmp_dir.name) / "cfg")
        self.secret_store.set_secret("MCP_SECRET_TOKEN", "tok-abc-123")
        self.manager = MCPClientManager(secret_resolver=self.secret_store)

    async def asyncTearDown(self) -> None:
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
        self.assertEqual(meta.get("shell-version"), ["48", "49", "50"])
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


if __name__ == "__main__":
    unittest.main()
