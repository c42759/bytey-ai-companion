"""Comprehensive automated test suite for AI Pet."""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
import os
import py_compile
import socket
import sqlite3
import stat
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

import httpx
import websockets

os.environ["AI_PET_DISABLE_KEYRING"] = "1"

REPO_ROOT = Path(__file__).resolve().parent.parent
CORE_DAEMON_DIR = REPO_ROOT / "core-daemon"
SETTINGS_UI_DIR = REPO_ROOT / "settings-ui"
GNOME_EXT_DIR = REPO_ROOT / "gnome-extension" / "ai-pet@local"

for _p in (str(CORE_DAEMON_DIR), str(SETTINGS_UI_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from ai_pet.audio.vad import VADProcessor  # noqa: E402
from ai_pet.audio.wakeword import WakeWordMatcher  # noqa: E402
from ai_pet.config import ConfigManager, SecretStore  # noqa: E402
from ai_pet.llm.emotions import EmotionStreamParser, VALID_EMOTIONS  # noqa: E402
from ai_pet.main import AiPetDaemon  # noqa: E402
from ai_pet.mcp_host import (  # noqa: E402
    MCPClientManager,
    from_mcp_servers_json,
    to_mcp_servers_json,
)
from ai_pet.storage import StorageManager  # noqa: E402
from ai_pet.tts.chunker import SentenceChunker  # noqa: E402
from ai_pet_settings.api_client import DaemonClient  # noqa: E402


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
    ) -> dict[str, Any]:
        self.calls.append(
            {
                "messages": messages,
                "provider": provider,
                "model": model,
                "tools": tools,
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


class TestConfigAndSecrets(unittest.TestCase):
    """Test ConfigManager and SecretStore behavior."""

    def setUp(self) -> None:
        os.environ["AI_PET_DISABLE_KEYRING"] = "1"
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


class TestStorageManager(unittest.IsolatedAsyncioTestCase):
    """Test SQLite StorageManager CRUD, tool_call attachment, and FK cascades."""

    async def asyncSetUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp_dir.name) / "pet.db"
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


class TestWakeWordAndVAD(unittest.TestCase):
    """Test WakeWordMatcher and VADProcessor."""

    def test_wake_word_matches_phonetic_and_rejects_negative(self) -> None:
        matched, rem = WakeWordMatcher.match_wake_phrase("Hey Nova", "Hey Nova")
        self.assertTrue(matched)
        self.assertEqual(rem, "")

        matched, rem = WakeWordMatcher.match_wake_phrase(
            "hey nova what is the status", "Hey Nova"
        )
        self.assertTrue(matched)
        self.assertEqual(rem, "what is the status")

        matched, rem = WakeWordMatcher.match_wake_phrase(
            "Hey Novah check calendar", "Hey Nova"
        )
        self.assertTrue(matched)
        self.assertEqual(rem, "check calendar")

        matched, rem = WakeWordMatcher.match_wake_phrase("hello world", "Hey Nova")
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


class TestLiveDaemonIntegration(unittest.IsolatedAsyncioTestCase):
    """End-to-end tests against a live AiPetDaemon on ephemeral localhost port."""

    async def asyncSetUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp_dir.name)
        self.cfg_dir = self.root / "config"
        self.data_dir = self.root / "data"
        self.server_script = self.root / "mock_mcp.py"
        self.server_script.write_text(MOCK_MCP_SERVER_SCRIPT, encoding="utf-8")

        self.mock_llm = _MockLLMRouter()
        self.daemon = AiPetDaemon(
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
                        json={"content": "Hello Nova", "input_mode": "text"},
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

os.environ["AI_PET_DISABLE_KEYRING"] = "1"
sys.path.insert(0, %r)
from ai_pet.main import AiPetDaemon

async def main():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        daemon = AiPetDaemon(
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
            env={**os.environ, "AI_PET_DISABLE_KEYRING": "1"},
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
                self.assertEqual(get_res.data["general"]["pet_name"], "Nova")

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

        meta_path = GNOME_EXT_DIR / "metadata.json"
        self.assertTrue(meta_path.is_file())
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        self.assertEqual(meta.get("uuid"), "ai-pet@local")
        self.assertIsInstance(meta.get("name"), str)
        self.assertIsInstance(meta.get("description"), str)
        self.assertIsInstance(meta.get("shell-version"), list)
        self.assertGreater(len(meta["shell-version"]), 0)
        self.assertIsInstance(meta.get("version"), int)


if __name__ == "__main__":
    unittest.main()
