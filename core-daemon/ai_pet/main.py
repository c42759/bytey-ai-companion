"""Main AI Pet core daemon coordinating IPC, SQLite, voice, TTS, LLM, and MCP."""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import shlex
import signal
import uuid
from pathlib import Path
from typing import Any

from .audio import VoiceInputService
from .config import ConfigManager, MCPServerConfig, SecretStore
from .ipc import IPCRequest, IPCResponse, IPCServer
from .llm import LLMRouter
from .llm.emotions import parse_emotion_prefix
from .mcp_host import MCPClientManager, from_mcp_servers_json, to_mcp_servers_json
from .tts import ElevenLabsTTSClient, KokoroTTSClient, SentenceChunker, StreamingTTSPlayer
from .storage import StorageManager

logger = logging.getLogger(__name__)

_PROVIDER_SECRET_KEYS: dict[str, str] = {
    "gemini": "GEMINI_API_KEY",
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "custom": "CUSTOM_LLM_API_KEY",
}

_FLAT_FIELD_MAP: dict[str, tuple[str, str]] = {
    "pet_name": ("general", "pet_name"),
    "wake_phrase": ("general", "wake_phrase"),
    "system_prompt": ("general", "system_prompt"),
    "llm_provider": ("provider", "active_provider"),
    "provider": ("provider", "active_provider"),
    "llm_base_url": ("provider", "base_url"),
    "llm_model": ("provider", "model"),
    "model": ("provider", "model"),
    "mic_muted": ("voice_in", "mic_muted"),
    "wake_sensitivity": ("voice_in", "wake_sensitivity"),
    "vad_silence_timeout_ms": ("voice_in", "silence_timeout_ms"),
    "silence_timeout_ms": ("voice_in", "silence_timeout_ms"),
    "stt_backend": ("voice_in", "stt_backend"),
    "whisper_model": ("voice_in", "whisper_model"),
    "stt_model": ("voice_in", "whisper_model"),
    "stt_api_url": ("voice_in", "stt_api_url"),
    "tts_muted": ("voice_out", "tts_muted"),
    "tts_provider": ("voice_out", "tts_provider"),
    "kokoro_base_url": ("voice_out", "kokoro_base_url"),
    "kokoro_voice": ("voice_out", "kokoro_voice"),
    "kokoro_speed": ("voice_out", "kokoro_speed"),
    "elevenlabs_voice_id": ("voice_out", "elevenlabs_voice_id"),
    "elevenlabs_model_id": ("voice_out", "elevenlabs_model_id"),
    "animation_mode": ("visuals", "animation_mode"),
    "cursor_tracking": ("visuals", "cursor_tracking"),
    "position_mode": ("visuals", "position_mode"),
    "monitor_index": ("visuals", "monitor_index"),
    "fixed_alignment": ("visuals", "fixed_alignment"),
    "offset_x": ("visuals", "offset_x"),
    "offset_y": ("visuals", "offset_y"),
    "float_x": ("visuals", "float_x"),
    "float_y": ("visuals", "float_y"),
    "chat_width": ("visuals", "chat_width"),
    "chat_height": ("visuals", "chat_height"),
    "chat_density": ("visuals", "chat_density"),
}


def _normalize_mcp_entry(entry: Any) -> dict[str, Any]:
    """Normalize GUI/JSON MCP server dicts into canonical MCPServerConfig kwargs."""
    if not isinstance(entry, dict):
        raise ValueError("MCP server entry must be a JSON object")
    out = copy.deepcopy(entry)
    if "disabled" in out and "enabled" not in out:
        out["enabled"] = not bool(out.pop("disabled"))
    elif "disabled" in out:
        out.pop("disabled")

    raw_args = out.get("args")
    if isinstance(raw_args, str):
        out["args"] = shlex.split(raw_args) if raw_args.strip() else []

    if not out.get("transport"):
        if out.get("command"):
            out["transport"] = "stdio"
        elif out.get("url"):
            out["transport"] = "sse" if str(out["url"]).rstrip("/").lower().endswith("/sse") else "http"

    return MCPServerConfig.model_validate(out).model_dump(mode="json")


def _export_mcp_map_for_ui(servers: dict[str, MCPServerConfig]) -> dict[str, dict[str, Any]]:
    """Serialize MCP servers with both `enabled` and UI-friendly `disabled` flags."""
    exported: dict[str, dict[str, Any]] = {}
    for name, srv in servers.items():
        item = srv.model_dump(mode="json") if isinstance(srv, MCPServerConfig) else dict(srv)
        item["disabled"] = not bool(item.get("enabled", True))
        exported[str(name)] = item
    return exported


class AiPetDaemon:
    """Orchestrates IPC, storage, voice pipeline, LLM router, MCP host, and TTS."""

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 8765,
        config_dir: Path | None = None,
        data_dir: Path | None = None,
        enable_voice_capture: bool = True,
        llm_router: LLMRouter | None = None,
        mcp_manager: MCPClientManager | None = None,
        kokoro_client: KokoroTTSClient | None = None,
        elevenlabs_client: ElevenLabsTTSClient | None = None,
    ) -> None:
        self.secret_store = SecretStore(config_dir=config_dir)
        self.config_manager = ConfigManager(config_dir=config_dir, secret_store=self.secret_store)
        db_path = (Path(data_dir) / "pet.db") if data_dir is not None else None
        self.storage = StorageManager(db_path=db_path)

        self.llm_router = llm_router or LLMRouter()
        self._owns_llm_router = llm_router is None
        self.mcp_manager = mcp_manager or MCPClientManager(secret_resolver=self.secret_store)
        self._owns_mcp_manager = mcp_manager is None
        self.kokoro_client = kokoro_client or KokoroTTSClient()
        self._owns_kokoro = kokoro_client is None
        self.elevenlabs_client = elevenlabs_client or ElevenLabsTTSClient()
        self._owns_elevenlabs = elevenlabs_client is None

        self.ipc = IPCServer(host=host, port=port, on_ws_message=self._on_ws_message)
        self.tts_player = StreamingTTSPlayer(
            get_config=lambda: self.config_manager.config,
            get_secret=self.secret_store.get_secret,
            on_audio_level=self._on_audio_level,
            on_state_change=self._on_subsystem_state_change,
            kokoro_client=self.kokoro_client,
            elevenlabs_client=self.elevenlabs_client,
        )
        self.enable_voice_capture = enable_voice_capture
        self.voice_input = VoiceInputService(
            get_config=lambda: self.config_manager.config,
            get_secret=self.secret_store.get_secret,
            on_audio_level=self._on_audio_level,
            on_state_change=self._on_subsystem_state_change,
            on_prompt_transcribed=self._on_voice_prompt,
            on_barge_in=self.tts_player.barge_in,
            is_busy=lambda: self.state in ("THINKING", "TOOL_CALLING", "SPEAKING")
            or self._chat_lock.locked(),
        )

        self.state: str = "IDLE"
        self.emotion: str = "neutral"
        self.emotion_reset_delay_s: float = 3.0
        self._emotion_reset_task: asyncio.Task[None] | None = None
        self._active_session_id: str | None = None
        self._detected_tools_support: bool = False
        self._pending_confirmations: dict[str, asyncio.Future[bool]] = {}
        self._chat_lock = asyncio.Lock()
        self._background_tasks: set[asyncio.Task[Any]] = set()
        self._stop_event = asyncio.Event()
        self._started = False

        self._register_routes()

    def _track_task(self, coro: Any) -> asyncio.Task[Any]:
        task = asyncio.create_task(coro)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return task

    async def start(self) -> None:
        """Initialize database, tool support metadata, IPC server, and voice capture."""
        if self._started:
            return
        await self.storage.init_db()
        active = await self.storage.get_or_create_active_session()
        self._active_session_id = str(active["id"])

        cfg = self.config_manager.config
        if cfg.voice_in.mic_muted:
            self.state = "MUTED"

        try:
            self._detected_tools_support = await self.llm_router.detect_tool_support(
                cfg.provider.active_provider,
                cfg.provider.model,
                override=cfg.provider.supports_tools_override,
                base_url=cfg.provider.base_url,
                api_key=self._get_provider_api_key(cfg.provider.active_provider),
            )
        except Exception:
            self._detected_tools_support = bool(cfg.provider.supports_tools_override)

        await self.ipc.start()
        if self.enable_voice_capture:
            await self.voice_input.start()
        self._started = True
        logger.info("AI Pet daemon listening on http://%s:%s", self.ipc.host, self.ipc.port)

    async def stop(self) -> None:
        """Gracefully shut down all subsystems, subprocesses, and sockets."""
        if not self._started and self._stop_event.is_set():
            return
        self._started = False
        self._stop_event.set()

        for call_id, fut in list(self._pending_confirmations.items()):
            if not fut.done():
                fut.set_result(False)
            self._pending_confirmations.pop(call_id, None)

        current_task = asyncio.current_task()
        other_tasks = [
            task
            for task in list(self._background_tasks)
            if task is not current_task and not task.done()
        ]
        for task in other_tasks:
            task.cancel()
        if other_tasks:
            await asyncio.gather(*other_tasks, return_exceptions=True)

        await self.voice_input.stop()
        await self.tts_player.stop()
        if self._owns_mcp_manager:
            await self.mcp_manager.close_all()
        if self._owns_llm_router:
            await self.llm_router.close()
        if self._owns_kokoro:
            await self.kokoro_client.close()
        if self._owns_elevenlabs:
            await self.elevenlabs_client.close()
        await self.ipc.stop()
        await self.storage.close()

    async def wait_until_stopped(self) -> None:
        """Block until daemon shutdown is requested."""
        await self._stop_event.wait()

    def _get_provider_api_key(self, provider: str) -> str | None:
        key_name = _PROVIDER_SECRET_KEYS.get((provider or "").strip().lower())
        if not key_name:
            return None
        return self.secret_store.get_secret(key_name)

    def _serialize_config(self) -> dict[str, Any]:
        """Return redacted config with compatibility mirrors for both UIs."""
        data = self.config_manager.get_redacted_dict()
        cfg = self.config_manager.config
        mcp_ui = _export_mcp_map_for_ui(cfg.mcp_servers)

        data["llm"] = {
            "provider": cfg.provider.active_provider,
            "model": cfg.provider.model,
            "base_url": cfg.provider.base_url,
            "supports_tools": self._detected_tools_support,
            "force_enable_tools": cfg.provider.supports_tools_override is True,
            "temperature": cfg.provider.temperature,
        }
        data["voice_input"] = {
            "mic_muted": cfg.voice_in.mic_muted,
            "wake_sensitivity": cfg.voice_in.wake_sensitivity,
            "vad_silence_timeout_ms": cfg.voice_in.silence_timeout_ms,
            "silence_timeout_ms": cfg.voice_in.silence_timeout_ms,
            "stt_backend": cfg.voice_in.stt_backend,
            "whisper_model": cfg.voice_in.whisper_model,
            "stt_api_url": cfg.voice_in.stt_api_url,
            "vad_threshold": cfg.voice_in.vad_threshold,
        }
        data["voice_output"] = {
            "tts_muted": cfg.voice_out.tts_muted,
            "provider": cfg.voice_out.tts_provider,
            "tts_provider": cfg.voice_out.tts_provider,
            "kokoro_base_url": cfg.voice_out.kokoro_base_url,
            "kokoro_voice": cfg.voice_out.kokoro_voice,
            "kokoro_speed": cfg.voice_out.kokoro_speed,
            "elevenlabs_voice_id": cfg.voice_out.elevenlabs_voice_id,
            "elevenlabs_model_id": cfg.voice_out.elevenlabs_model_id,
        }
        data["mcpServers"] = mcp_ui
        return data

    def _normalize_patch(self, raw_patch: dict[str, Any]) -> tuple[dict[str, Any], bool | None]:
        """Translate nested/alias/flat UI patches into canonical PetConfig patches."""
        if not isinstance(raw_patch, dict):
            raise ValueError("Config patch must be a JSON object")

        canonical: dict[str, Any] = {}
        tools_hint: bool | None = None

        def merge_section(sec: str, values: dict[str, Any]) -> None:
            canonical.setdefault(sec, {}).update(values)

        if isinstance(raw_patch.get("general"), dict):
            merge_section("general", raw_patch["general"])
        if isinstance(raw_patch.get("provider"), dict):
            merge_section("provider", raw_patch["provider"])
        if isinstance(raw_patch.get("voice_in"), dict):
            merge_section("voice_in", raw_patch["voice_in"])
        if isinstance(raw_patch.get("voice_out"), dict):
            merge_section("voice_out", raw_patch["voice_out"])
        if isinstance(raw_patch.get("visuals"), dict):
            merge_section("visuals", raw_patch["visuals"])

        # settings-ui `llm` mirror
        if isinstance(raw_patch.get("llm"), dict):
            llm = raw_patch["llm"]
            mapped: dict[str, Any] = {}
            if "provider" in llm:
                mapped["active_provider"] = llm["provider"]
            if "active_provider" in llm:
                mapped["active_provider"] = llm["active_provider"]
            if "model" in llm:
                mapped["model"] = llm["model"]
            if "base_url" in llm:
                mapped["base_url"] = llm["base_url"]
            if "temperature" in llm:
                mapped["temperature"] = llm["temperature"]
            if "supports_tools" in llm and isinstance(llm["supports_tools"], bool):
                tools_hint = llm["supports_tools"]
            if "force_enable_tools" in llm:
                mapped["supports_tools_override"] = True if llm["force_enable_tools"] else None
            if "supports_tools_override" in llm:
                mapped["supports_tools_override"] = llm["supports_tools_override"]
            if mapped:
                merge_section("provider", mapped)

        # settings-ui `voice_input` mirror
        if isinstance(raw_patch.get("voice_input"), dict):
            vi = raw_patch["voice_input"]
            mapped_vi: dict[str, Any] = {}
            for key in (
                "mic_muted",
                "stt_backend",
                "whisper_model",
                "stt_api_url",
                "wake_sensitivity",
                "silence_timeout_ms",
                "vad_threshold",
            ):
                if key in vi:
                    mapped_vi[key] = vi[key]
            if "vad_silence_timeout_ms" in vi:
                mapped_vi["silence_timeout_ms"] = vi["vad_silence_timeout_ms"]
            if mapped_vi:
                merge_section("voice_in", mapped_vi)

        # settings-ui `voice_output` mirror
        if isinstance(raw_patch.get("voice_output"), dict):
            vo = raw_patch["voice_output"]
            mapped_vo: dict[str, Any] = {}
            if "provider" in vo:
                mapped_vo["tts_provider"] = vo["provider"]
            if "tts_provider" in vo:
                mapped_vo["tts_provider"] = vo["tts_provider"]
            for key in (
                "tts_muted",
                "kokoro_base_url",
                "kokoro_voice",
                "kokoro_speed",
                "elevenlabs_voice_id",
                "elevenlabs_model_id",
            ):
                if key in vo:
                    mapped_vo[key] = vo[key]
            if mapped_vo:
                merge_section("voice_out", mapped_vo)

        # Flat key aliases
        for flat_key, (sec, target_key) in _FLAT_FIELD_MAP.items():
            if flat_key in raw_patch and not isinstance(raw_patch[flat_key], dict):
                merge_section(sec, {target_key: raw_patch[flat_key]})
        if "llm_force_tools" in raw_patch:
            merge_section(
                "provider",
                {"supports_tools_override": True if raw_patch["llm_force_tools"] else None},
            )
        if "supports_tools" in raw_patch and isinstance(raw_patch["supports_tools"], bool):
            tools_hint = raw_patch["supports_tools"]

        # MCP servers map
        raw_mcp = raw_patch.get("mcp_servers")
        if raw_mcp is None and "mcpServers" in raw_patch:
            raw_mcp = raw_patch["mcpServers"]
        if raw_mcp is not None:
            if not isinstance(raw_mcp, dict):
                raise ValueError("mcp_servers/mcpServers must be an object")
            canonical["mcp_servers"] = {
                str(name): _normalize_mcp_entry(cfg)
                for name, cfg in raw_mcp.items()
            }

        return canonical, tools_hint

    async def _set_state(
        self,
        state: str,
        emotion: str | None = None,
        label: str | None = None,
    ) -> None:
        self.state = state
        if emotion is not None:
            self.emotion = emotion
        if state in ("THINKING", "TOOL_CALLING", "SPEAKING"):
            self._cancel_emotion_reset()
        payload: dict[str, Any] = {
            "type": "state_changed",
            "state": self.state,
            "emotion": self.emotion,
        }
        if label is not None:
            payload["label"] = label
        await self.ipc.connection_manager.broadcast(payload)

    def _cancel_emotion_reset(self) -> None:
        if self._emotion_reset_task is not None and not self._emotion_reset_task.done():
            self._emotion_reset_task.cancel()
        self._emotion_reset_task = None

    def _schedule_emotion_reset(
        self,
        delay_s: float | None = None,
        *,
        reset_error_state: bool = False,
    ) -> None:
        self._cancel_emotion_reset()
        wait_s = self.emotion_reset_delay_s if delay_s is None else max(0.0, float(delay_s))

        async def _reset_worker() -> None:
            try:
                await asyncio.sleep(wait_s)
                await self.tts_player.wait_idle()
                if self.state == "LISTENING":
                    if self.emotion != "neutral":
                        await self._set_state("LISTENING", emotion="neutral")
                elif self.state in ("IDLE", "MUTED") or (
                    reset_error_state and self.state == "ERROR"
                ):
                    target_state = (
                        "MUTED"
                        if self.config_manager.config.voice_in.mic_muted
                        else "IDLE"
                    )
                    if self.emotion != "neutral" or self.state != target_state:
                        await self._set_state(target_state, emotion="neutral")
            except asyncio.CancelledError:
                pass

        self._emotion_reset_task = self._track_task(_reset_worker())

    async def _on_subsystem_state_change(self, state: str) -> None:
        prev_state = self.state
        norm = str(state or "IDLE").upper()
        if norm == "IDLE" and self.config_manager.config.voice_in.mic_muted:
            norm = "MUTED"
        if prev_state == "SPEAKING" and norm in ("IDLE", "MUTED") and not self._chat_lock.locked():
            await self._set_state(norm, emotion="neutral")
            return
        await self._set_state(norm)

    async def _on_audio_level(self, source: str, rms: float) -> None:
        await self.ipc.connection_manager.broadcast(
            {"type": "audio_level", "source": str(source), "rms": round(float(rms), 4)}
        )

    async def _on_voice_prompt(self, text: str) -> None:
        cleaned = (text or "").strip()
        if not cleaned:
            return
        await self.handle_user_prompt(cleaned, input_mode="voice")

    async def _on_ws_message(self, _ws: Any, raw_msg: str | bytes) -> None:
        if isinstance(raw_msg, bytes):
            raw_msg = raw_msg.decode("utf-8", errors="replace")
        try:
            event = json.loads(raw_msg)
        except json.JSONDecodeError:
            return
        if not isinstance(event, dict):
            return
        ev_type = str(event.get("type") or "").lower()
        if ev_type == "barge_in":
            await self.tts_player.barge_in()
        elif ev_type == "trigger_voice":
            await self.voice_input.trigger_manual_listen()
        elif ev_type == "confirm_tool":
            call_id = str(event.get("call_id") or "")
            approved = bool(event.get("approved", False))
            fut = self._pending_confirmations.get(call_id)
            if fut is not None and not fut.done():
                fut.set_result(approved)

    async def _ensure_active_session(self, requested_session_id: str | None = None) -> str:
        if requested_session_id and requested_session_id != "active":
            sessions = await self.storage.list_sessions()
            for s in sessions:
                if s["id"] == requested_session_id:
                    self._active_session_id = requested_session_id
                    return requested_session_id
        if self._active_session_id:
            sessions = await self.storage.list_sessions()
            if any(s["id"] == self._active_session_id for s in sessions):
                return self._active_session_id
        active = await self.storage.get_or_create_active_session()
        self._active_session_id = str(active["id"])
        return self._active_session_id

    async def _request_tool_confirmation(
        self,
        call_id: str,
        message_id: str,
        server_name: str,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> bool:
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[bool] = loop.create_future()
        self._pending_confirmations[call_id] = fut

        await self.storage.update_tool_call(call_id, "awaiting_confirm")
        await self.ipc.connection_manager.broadcast(
            {
                "type": "tool_call_status",
                "call_id": call_id,
                "message_id": message_id,
                "server": server_name,
                "tool": tool_name,
                "arguments": arguments,
                "status": "awaiting_confirm",
                "summary": "Waiting for user approval...",
            }
        )
        try:
            approved = await asyncio.wait_for(fut, timeout=60.0)
            return bool(approved)
        except asyncio.TimeoutError:
            return False
        finally:
            self._pending_confirmations.pop(call_id, None)

    async def handle_user_prompt(
        self,
        prompt_text: str,
        *,
        input_mode: str = "text",
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Run a full user turn: persist message, call LLM + MCP tools, stream TTS."""
        text = (prompt_text or "").strip()
        if not text:
            raise ValueError("Prompt text cannot be empty")

        async with self._chat_lock:
            await self.tts_player.barge_in()
            sid = await self._ensure_active_session(session_id)

            user_msg = await self.storage.add_message(
                sid,
                role="user",
                content=text,
                input_mode=input_mode,
                emotion="neutral",
            )
            await self.ipc.connection_manager.broadcast(
                {
                    "type": "transcript_delta",
                    "session_id": sid,
                    "message_id": user_msg["id"],
                    "role": "user",
                    "source": input_mode,
                    "text": text,
                    "is_final": True,
                }
            )

            await self._set_state("THINKING", emotion="neutral")

            history = await self.storage.list_messages(sid, limit=40)
            llm_messages: list[dict[str, Any]] = [
                {"role": m["role"], "content": m["content"]}
                for m in history
                if m.get("content")
            ]

            assistant_msg = await self.storage.add_message(
                sid,
                role="assistant",
                content="",
                input_mode=input_mode,
                emotion="neutral",
            )
            assistant_id = str(assistant_msg["id"])

            cfg = self.config_manager.config
            api_key = self._get_provider_api_key(cfg.provider.active_provider)

            try:
                supports_tools = await self.llm_router.detect_tool_support(
                    cfg.provider.active_provider,
                    cfg.provider.model,
                    override=cfg.provider.supports_tools_override,
                    base_url=cfg.provider.base_url,
                    api_key=api_key,
                )
            except Exception:
                supports_tools = bool(cfg.provider.supports_tools_override)
            self._detected_tools_support = supports_tools

            tools: list[dict[str, Any]] = []
            if supports_tools and cfg.mcp_servers:
                try:
                    tools = await self.mcp_manager.list_active_tools(cfg.mcp_servers)
                except Exception as exc:
                    logger.warning("Failed listing active MCP tools: %s", exc)
                    tools = []

            chunker = SentenceChunker()
            final_emotion = "neutral"
            accumulated_parts: list[str] = []

            async def on_emotion(tag: str) -> None:
                nonlocal final_emotion
                final_emotion = tag
                await self._set_state("THINKING", emotion=tag)

            async def on_text_chunk(delta: str) -> None:
                if not delta:
                    return
                accumulated_parts.append(delta)
                await self.ipc.connection_manager.broadcast(
                    {
                        "type": "transcript_delta",
                        "session_id": sid,
                        "message_id": assistant_id,
                        "role": "assistant",
                        "source": input_mode,
                        "delta": delta,
                        "emotion": final_emotion,
                        "is_final": False,
                    }
                )
                for sentence in chunker.feed(delta):
                    await self.tts_player.enqueue_sentence(sentence)

            try:
                for _round in range(6):
                    result = await self.llm_router.generate_chat(
                        messages=llm_messages,
                        system_prompt=cfg.general.system_prompt,
                        provider=cfg.provider.active_provider,
                        model=cfg.provider.model,
                        base_url=cfg.provider.base_url,
                        api_key=api_key,
                        temperature=cfg.provider.temperature,
                        tools=tools or None,
                        on_text_chunk=on_text_chunk,
                        on_emotion=on_emotion,
                    )
                    if result.get("emotion"):
                        final_emotion = str(result["emotion"])
                    tool_calls = result.get("tool_calls") or []
                    if not tool_calls:
                        break

                    llm_messages.append(
                        result.get("raw_assistant_message")
                        or {"role": "assistant", "content": result.get("content", "")}
                    )

                    for tc in tool_calls:
                        ns_name = str(tc.get("name") or "")
                        args = tc.get("arguments") if isinstance(tc.get("arguments"), dict) else {}
                        call_id = str(uuid.uuid4())
                        split = self.mcp_manager._split_namespaced_tool(ns_name, cfg.mcp_servers)
                        srv_name, tool_name = split if split else ("mcp", ns_name)

                        await self.storage.add_tool_call(
                            message_id=assistant_id,
                            server_name=srv_name,
                            tool_name=tool_name,
                            arguments_json=json.dumps(args, ensure_ascii=False),
                            status="running",
                            call_id=call_id,
                        )
                        await self._set_state("TOOL_CALLING", emotion=final_emotion, label=tool_name)
                        await self.ipc.connection_manager.broadcast(
                            {
                                "type": "tool_call_status",
                                "call_id": call_id,
                                "message_id": assistant_id,
                                "server": srv_name,
                                "tool": tool_name,
                                "arguments": args,
                                "status": "running",
                                "summary": "",
                            }
                        )

                        async def confirm_cb(
                            c_srv: str,
                            c_tool: str,
                            c_args: dict[str, Any],
                            _cid: str = call_id,
                        ) -> bool:
                            approved = await self._request_tool_confirmation(
                                _cid, assistant_id, c_srv, c_tool, c_args
                            )
                            if approved:
                                await self.storage.update_tool_call(_cid, "running")
                                await self.ipc.connection_manager.broadcast(
                                    {
                                        "type": "tool_call_status",
                                        "call_id": _cid,
                                        "message_id": assistant_id,
                                        "server": c_srv,
                                        "tool": c_tool,
                                        "arguments": c_args,
                                        "status": "running",
                                        "summary": "Approved",
                                    }
                                )
                            return approved

                        tool_res = await self.mcp_manager.execute_tool_call(
                            ns_name,
                            args,
                            cfg.mcp_servers,
                            confirm_callback=confirm_cb,
                        )
                        status_str = (
                            "ok"
                            if tool_res.get("ok")
                            else ("denied" if tool_res.get("denied") else "error")
                        )
                        summary = str(tool_res.get("content") or tool_res.get("error") or "")
                        await self.storage.update_tool_call(call_id, status_str, summary)
                        await self.ipc.connection_manager.broadcast(
                            {
                                "type": "tool_call_status",
                                "call_id": call_id,
                                "message_id": assistant_id,
                                "server": srv_name,
                                "tool": tool_name,
                                "arguments": args,
                                "status": status_str,
                                "summary": summary[:400],
                            }
                        )
                        llm_messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": str(tc.get("id") or call_id),
                                "name": ns_name,
                                "content": summary,
                            }
                        )

                    await self._set_state("THINKING", emotion=final_emotion)

                tail = chunker.flush()
                if tail:
                    await self.tts_player.enqueue_sentence(tail)

                full_content = "".join(accumulated_parts).strip()
                updated_msg = await self.storage.update_message(
                    assistant_id, content=full_content, emotion=final_emotion
                )
                await self.ipc.connection_manager.broadcast(
                    {
                        "type": "transcript_delta",
                        "session_id": sid,
                        "message_id": assistant_id,
                        "role": "assistant",
                        "source": input_mode,
                        "text": full_content,
                        "emotion": final_emotion,
                        "is_final": True,
                    }
                )
                target_idle_state = "MUTED" if cfg.voice_in.mic_muted else "IDLE"
                continue_voice = (
                    input_mode == "voice"
                    and not cfg.voice_in.mic_muted
                    and self.enable_voice_capture
                )
                if cfg.voice_out.tts_muted or cfg.voice_out.tts_provider == "disabled":
                    if continue_voice:
                        self.emotion = final_emotion
                        await self.voice_input.start_followup_listen()
                    else:
                        await self._set_state(
                            target_idle_state,
                            emotion=final_emotion,
                        )
                    if final_emotion != "neutral":
                        self._schedule_emotion_reset(self.emotion_reset_delay_s)
                else:
                    t0 = asyncio.get_running_loop().time()
                    await self.tts_player.wait_idle()
                    tts_elapsed = asyncio.get_running_loop().time() - t0
                    if tts_elapsed >= 0.35:
                        # Audio reproduction completed: return mood back to neutral immediately.
                        if continue_voice:
                            self.emotion = "neutral"
                            await self.voice_input.start_followup_listen()
                        else:
                            await self._set_state(
                                target_idle_state,
                                emotion="neutral",
                            )
                    else:
                        # No audio played (or instant failure): keep mood briefly, then reset.
                        if continue_voice:
                            self.emotion = final_emotion
                            await self.voice_input.start_followup_listen()
                        else:
                            await self._set_state(
                                target_idle_state,
                                emotion=final_emotion,
                            )
                        if final_emotion != "neutral":
                            self._schedule_emotion_reset(
                                max(0.5, self.emotion_reset_delay_s - tts_elapsed)
                            )

                return {
                    "session_id": sid,
                    "user_message": user_msg,
                    "assistant_message": updated_msg or assistant_msg,
                }

            except Exception as exc:
                logger.exception("Error during chat turn")
                err_text = f"Sorry, I hit an error: {exc}"
                await self.storage.update_message(assistant_id, content=err_text, emotion="error")
                await self._set_state("ERROR", emotion="error", label=str(exc)[:60])
                self._schedule_emotion_reset(self.emotion_reset_delay_s, reset_error_state=True)
                await self.ipc.connection_manager.broadcast(
                    {
                        "type": "transcript_delta",
                        "session_id": sid,
                        "message_id": assistant_id,
                        "role": "assistant",
                        "source": input_mode,
                        "text": err_text,
                        "emotion": "error",
                        "is_final": True,
                    }
                )
                return IPCResponse(status=500, body={"error": str(exc)})  # type: ignore[return-value]

    def _register_routes(self) -> None:
        self.ipc.register_route("GET", "/api/v1/health", self._route_health)
        self.ipc.register_route("GET", "/health", self._route_health)

        self.ipc.register_route("GET", "/api/v1/config", self._route_get_config)
        self.ipc.register_route("PATCH", "/api/v1/config", self._route_patch_config)
        self.ipc.register_route("PUT", "/api/v1/secrets", self._route_put_secrets)

        self.ipc.register_route("GET", "/api/v1/providers/{provider}/models", self._route_list_models)

        self.ipc.register_route("GET", "/api/v1/mcp/json", self._route_get_mcp_json)
        self.ipc.register_route("GET", "/api/v1/mcp", self._route_get_mcp_json)
        self.ipc.register_route("PUT", "/api/v1/mcp/json", self._route_put_mcp_json)
        self.ipc.register_route("PUT", "/api/v1/mcp", self._route_put_mcp_json)
        self.ipc.register_route("POST", "/api/v1/mcp/test", self._route_test_mcp)
        self.ipc.register_route("POST", "/api/v1/mcp/confirm", self._route_confirm_tool)
        self.ipc.register_route("POST", "/api/v1/tools/{call_id}/confirm", self._route_confirm_tool)

        self.ipc.register_route("GET", "/api/v1/tts/voices", self._route_list_voices)
        self.ipc.register_route("POST", "/api/v1/tts/replay", self._route_replay_tts)
        self.ipc.register_route(
            "POST",
            "/api/v1/sessions/{session_id}/messages/{message_id}/replay",
            self._route_replay_tts,
        )

        self.ipc.register_route("GET", "/api/v1/sessions", self._route_list_sessions)
        self.ipc.register_route("GET", "/api/v1/sessions/active", self._route_active_session)
        self.ipc.register_route("GET", "/api/v1/sessions/{session_id}/messages", self._route_session_messages)
        self.ipc.register_route("POST", "/api/v1/sessions", self._route_post_sessions)
        self.ipc.register_route("POST", "/api/v1/sessions/{session_id}/messages", self._route_post_message)
        self.ipc.register_route("DELETE", "/api/v1/sessions/{session_id}", self._route_delete_session)
        self.ipc.register_route("POST", "/api/v1/chat", self._route_chat)

        self.ipc.register_route("POST", "/api/v1/voice/trigger", self._route_voice_trigger)
        self.ipc.register_route("POST", "/api/v1/voice/barge-in", self._route_voice_barge_in)
        self.ipc.register_route("POST", "/api/v1/system/exit", self._route_system_exit)

    async def _route_health(self, _req: IPCRequest) -> dict[str, Any]:
        return {"status": "ok", "state": self.state, "emotion": self.emotion}

    async def _route_get_config(self, _req: IPCRequest) -> dict[str, Any]:
        return self._serialize_config()

    async def _route_patch_config(self, req: IPCRequest) -> dict[str, Any]:
        raw_patch = req.json()
        prev_muted = self.config_manager.config.voice_in.mic_muted
        canonical_patch, tools_hint = self._normalize_patch(raw_patch)
        updated = self.config_manager.update_config(canonical_patch)

        if tools_hint is not None:
            self._detected_tools_support = tools_hint
        elif "provider" in canonical_patch:
            try:
                self._detected_tools_support = await self.llm_router.detect_tool_support(
                    updated.provider.active_provider,
                    updated.provider.model,
                    override=updated.provider.supports_tools_override,
                    base_url=updated.provider.base_url,
                    api_key=self._get_provider_api_key(updated.provider.active_provider),
                )
            except Exception:
                pass

        if updated.voice_in.mic_muted != prev_muted:
            await self.voice_input.set_muted(updated.voice_in.mic_muted)

        serialized = self._serialize_config()
        await self.ipc.connection_manager.broadcast(
            {"type": "config_updated", "config": serialized}
        )
        if "visuals" in canonical_patch:
            await self.ipc.connection_manager.broadcast(
                {"type": "position_updated", "visuals": serialized["visuals"]}
            )
        return serialized

    async def _route_put_secrets(self, req: IPCRequest) -> dict[str, Any]:
        body = req.json()
        if not isinstance(body, dict):
            raise ValueError("Request body must be a JSON object")
        secrets_map = body.get("secrets") if isinstance(body.get("secrets"), dict) else body
        if not isinstance(secrets_map, dict) or not secrets_map:
            raise ValueError("No secrets provided")

        updated_keys: list[str] = []
        for k, v in secrets_map.items():
            if not isinstance(k, str) or not isinstance(v, str):
                raise ValueError("Secret keys and values must be strings")
            self.secret_store.set_secret(k, v)
            updated_keys.append(k)

        serialized = self._serialize_config()
        await self.ipc.connection_manager.broadcast(
            {"type": "config_updated", "config": serialized}
        )
        return {
            "ok": True,
            "updated": updated_keys,
            "secrets_configured": self.secret_store.list_configured_secrets(),
        }

    async def _route_list_models(self, req: IPCRequest) -> dict[str, Any]:
        provider = req.path_params["provider"]
        cfg = self.config_manager.config
        base_url = req.query.get("base_url") or (
            cfg.provider.base_url if provider == cfg.provider.active_provider else ""
        )
        api_key = self._get_provider_api_key(provider)
        return await self.llm_router.discover_models(
            provider=provider,
            base_url=base_url,
            api_key=api_key,
        )

    async def _route_get_mcp_json(self, _req: IPCRequest) -> dict[str, Any]:
        cfg = self.config_manager.config
        raw_json = to_mcp_servers_json(cfg.mcp_servers)
        return {
            "raw_json": raw_json,
            "mcpServers": _export_mcp_map_for_ui(cfg.mcp_servers),
        }

    async def _route_put_mcp_json(self, req: IPCRequest) -> dict[str, Any]:
        body = req.json()
        if not isinstance(body, dict):
            raise ValueError("Request body must be a JSON object")

        if "raw_json" in body:
            raw_json_str = str(body["raw_json"])
            parsed = json.loads(raw_json_str)
        elif "mcpServers" in body:
            parsed = {"mcpServers": body["mcpServers"]}
        elif "mcp_servers" in body:
            parsed = {"mcpServers": body["mcp_servers"]}
        else:
            raise ValueError("Expected 'raw_json' or 'mcpServers'")

        if isinstance(parsed, dict) and isinstance(parsed.get("mcpServers"), dict):
            normalized_input = {}
            for name, entry in parsed["mcpServers"].items():
                if not isinstance(entry, dict):
                    raise ValueError(f"Server '{name}' config must be a JSON object")
                item = dict(entry)
                if "disabled" in item and "enabled" not in item:
                    item["enabled"] = not bool(item.pop("disabled"))
                else:
                    item.pop("disabled", None)
                normalized_input[name] = item
            parsed = {"mcpServers": normalized_input}

        parsed_servers = from_mcp_servers_json(
            json.dumps(parsed),
            existing=self.config_manager.config.mcp_servers,
        )
        self.config_manager.update_config({"mcp_servers": parsed_servers})

        serialized = self._serialize_config()
        await self.ipc.connection_manager.broadcast(
            {"type": "config_updated", "config": serialized}
        )
        return {
            "raw_json": to_mcp_servers_json(self.config_manager.config.mcp_servers),
            "mcpServers": _export_mcp_map_for_ui(self.config_manager.config.mcp_servers),
        }

    async def _route_test_mcp(self, req: IPCRequest) -> dict[str, Any]:
        body = req.json()
        if not isinstance(body, dict):
            raise ValueError("Request body must be a JSON object")
        name = str(body.get("name") or "test-server")
        raw_cfg = body.get("config") if isinstance(body.get("config"), dict) else {
            k: v for k, v in body.items() if k != "name"
        }
        norm_cfg = _normalize_mcp_entry(raw_cfg)
        return await self.mcp_manager.test_server(name, norm_cfg)

    async def _route_confirm_tool(self, req: IPCRequest) -> dict[str, Any]:
        body = req.json()
        call_id = req.path_params.get("call_id") or str(body.get("call_id") or "")
        approved = bool(body.get("approved", False))
        fut = self._pending_confirmations.get(call_id)
        if fut is None or fut.done():
            return IPCResponse(
                status=404,
                body={"ok": False, "error": f"No pending confirmation for {call_id!r}"},
            )
        fut.set_result(approved)
        return {"ok": True, "call_id": call_id, "approved": approved}

    async def _route_list_voices(self, req: IPCRequest) -> dict[str, Any]:
        cfg = self.config_manager.config
        provider = (req.query.get("provider") or cfg.voice_out.tts_provider).strip().lower()
        if provider == "kokoro":
            base_url = req.query.get("base_url") or cfg.voice_out.kokoro_base_url
            api_key = self.secret_store.get_secret("KOKORO_API_KEY")
            voices = await self.kokoro_client.list_voices(base_url=base_url, api_key=api_key)
            return {"voices": voices}
        if provider == "elevenlabs":
            api_key = self.secret_store.get_secret("ELEVENLABS_API_KEY") or ""
            voices = await self.elevenlabs_client.list_voices(api_key=api_key)
            return {"voices": voices}
        return {"voices": []}

    async def _route_replay_tts(self, req: IPCRequest) -> dict[str, Any]:
        body = req.json()
        text = str(body.get("text") or "").strip()
        msg_id = req.path_params.get("message_id") or body.get("message_id")
        replay_emotion: str | None = None
        if msg_id:
            sid = await self._ensure_active_session(req.path_params.get("session_id"))
            for msg in await self.storage.list_messages(sid, limit=200):
                if msg["id"] == msg_id:
                    replay_emotion = str(msg.get("emotion") or "neutral")
                    if not text:
                        _, text = parse_emotion_prefix(str(msg.get("content") or ""))
                    break
        if not text:
            raise ValueError("No text available to replay")
        await self.tts_player.barge_in()
        if replay_emotion and replay_emotion != "neutral":
            await self._set_state(self.state, emotion=replay_emotion)
        await self.tts_player.speak_text(text)
        return {"ok": True}

    async def _route_list_sessions(self, _req: IPCRequest) -> dict[str, Any]:
        sid = await self._ensure_active_session()
        sessions = await self.storage.list_sessions()
        messages = await self.storage.list_messages(sid, limit=100)
        return {
            "active_session_id": sid,
            "sessions": sessions,
            "messages": messages,
        }

    async def _route_active_session(self, _req: IPCRequest) -> dict[str, Any]:
        sid = await self._ensure_active_session()
        sessions = await self.storage.list_sessions()
        session_obj = next((s for s in sessions if s["id"] == sid), {"id": sid, "title": "New Chat"})
        messages = await self.storage.list_messages(sid, limit=100)
        return {
            "id": sid,
            "active_session_id": sid,
            "session": session_obj,
            "messages": messages,
        }

    async def _route_session_messages(self, req: IPCRequest) -> dict[str, Any]:
        sid = await self._ensure_active_session(req.path_params.get("session_id"))
        messages = await self.storage.list_messages(sid, limit=100)
        return {"session_id": sid, "messages": messages}

    async def _route_post_sessions(self, req: IPCRequest) -> dict[str, Any]:
        body = req.json()
        if isinstance(body, dict) and body.get("action") == "message":
            text = str(body.get("content") or body.get("text") or "").strip()
            mode = str(body.get("input_mode") or body.get("source") or "text")
            sid = body.get("session_id")
            return await self.handle_user_prompt(text, input_mode=mode, session_id=sid)

        title = str((body or {}).get("title") or "New Chat")
        session = await self.storage.create_session(title=title)
        self._active_session_id = str(session["id"])
        return {**session, "session": session}

    async def _route_delete_session(self, req: IPCRequest) -> dict[str, Any]:
        sid = req.path_params["session_id"]
        await self.storage.delete_session(sid)
        if self._active_session_id == sid:
            self._active_session_id = None
        new_sid = await self._ensure_active_session()
        return {"ok": True, "active_session_id": new_sid}

    async def _route_post_message(self, req: IPCRequest) -> dict[str, Any]:
        body = req.json()
        text = str(body.get("content") or body.get("text") or body.get("prompt") or "").strip()
        mode = str(body.get("input_mode") or body.get("source") or "text")
        sid = req.path_params.get("session_id")
        res = await self.handle_user_prompt(text, input_mode=mode, session_id=sid)
        if isinstance(res, IPCResponse):
            return res  # type: ignore[return-value]
        return res

    async def _route_chat(self, req: IPCRequest) -> dict[str, Any]:
        body = req.json()
        text = str(body.get("content") or body.get("text") or body.get("prompt") or "").strip()
        mode = str(body.get("input_mode") or body.get("source") or "text")
        sid = body.get("session_id")
        res = await self.handle_user_prompt(text, input_mode=mode, session_id=sid)
        if isinstance(res, IPCResponse):
            return res  # type: ignore[return-value]
        return res

    async def _route_voice_trigger(self, _req: IPCRequest) -> dict[str, Any]:
        await self.voice_input.trigger_manual_listen()
        return {"ok": True}

    async def _route_voice_barge_in(self, _req: IPCRequest) -> dict[str, Any]:
        await self.tts_player.barge_in()
        await self._set_state("MUTED" if self.config_manager.config.voice_in.mic_muted else "IDLE")
        return {"ok": True}

    async def _route_system_exit(self, _req: IPCRequest) -> dict[str, Any]:
        await self.ipc.connection_manager.broadcast({"type": "shutdown"})

        async def _delayed_stop() -> None:
            await asyncio.sleep(0.05)
            await self.stop()

        self._track_task(_delayed_stop())
        return {"ok": True, "status": "shutting_down"}


async def async_main() -> None:
    logging.basicConfig(
        level=os.environ.get("AI_PET_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    host = os.environ.get("AI_PET_HOST", "127.0.0.1")
    port = int(os.environ.get("AI_PET_PORT", "8765"))
    daemon = AiPetDaemon(host=host, port=port)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, lambda: daemon._track_task(daemon.stop()))
        except NotImplementedError:
            pass

    await daemon.start()
    try:
        await daemon.wait_until_stopped()
    finally:
        await daemon.stop()


def main() -> None:
    """CLI entrypoint for `python3 -m ai_pet.main`."""
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
