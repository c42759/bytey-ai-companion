"""Main Bytey AI Companion core daemon coordinating IPC, SQLite, voice, TTS, LLM, and MCP."""

from __future__ import annotations

import os
import copy
import json
import time
import uuid
import shlex
import inspect
import asyncio
import logging
import signal

from typing import Any
from pathlib import Path
from collections import deque
from datetime import datetime

from .agents import AgentRegistry, AgentRunner
from .audio import VoiceInputService
from .config import (
    PIPER_VOICE_PRESETS,
    ConfigManager,
    ExternalAgentOverride,
    MCPServerConfig,
    SecretStore,
    SkillOverrideConfig,
    resolve_default_skills_dir,
    resolve_skills_dir,
)
from .ipc import IPCRequest, IPCResponse, IPCServer
from .llm import LLMRouter
from .llm.emotions import (
    DEFAULT_SYSTEM_PROMPT,
    VALID_EMOTIONS,
    parse_emotion_prefix,
    parse_reply_tags,
)
from .mcp_host import MCPClientManager, from_mcp_servers_json, to_mcp_servers_json
from .skills import SkillDefinition, SkillRegistry, SkillScriptRunner
from .tts import (
    ElevenLabsTTSClient,
    KokoroTTSClient,
    PiperTTSClient,
    SentenceChunker,
    StreamingTTSPlayer,
)
from .storage import StorageManager, resolve_db_path
from .workspace.git_tools import (
    ALWAYS_CONFIRM_GIT_TOOLS,
    READ_ONLY_GIT_TOOLS,
    GitToolchain,
)
from .workspace.tools import WorkspaceToolchain, detect_git_branch, load_workspace_rules

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
    "llm_context_size": ("provider", "context_size"),
    "context_size": ("provider", "context_size"),
    "max_tool_rounds": ("provider", "max_tool_rounds"),
    "llm_max_tool_rounds": ("provider", "max_tool_rounds"),
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
    "piper_model": ("voice_out", "piper_model"),
    "piper_models_dir": ("voice_out", "piper_models_dir"),
    "piper_speed": ("voice_out", "piper_speed"),
    "piper_pitch": ("voice_out", "piper_pitch"),
    "piper_noise_scale": ("voice_out", "piper_noise_scale"),
    "piper_speaker_id": ("voice_out", "piper_speaker_id"),
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
    "eye_color": ("visuals", "eye_color"),
    "background_color": ("visuals", "background_color"),
    "border_color": ("visuals", "border_color"),
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


def _accepts_kwarg(fn: Any, kwarg: str) -> bool:
    """Return True if fn accepts `kwarg` explicitly or via **kwargs."""
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return True
    return kwarg in sig.parameters or any(
        p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
    )


def _auto_title_from_prompt(text: str, max_len: int = 48) -> str:
    """Build a clean single-line title from the first user prompt."""
    clean = " ".join((text or "").split())
    if not clean:
        return "New Chat"
    if len(clean) <= max_len:
        return clean
    cutoff = max_len - 3
    snippet = clean[:cutoff]
    last_space = snippet.rfind(" ")
    if last_space >= cutoff // 2:
        snippet = snippet[:last_space]
    return snippet.rstrip() + "..."


def _compact_older_tool_messages(
    messages: list[dict[str, Any]],
    *,
    keep_recent: int = 4,
    max_chars: int = 800,
) -> None:
    """Truncate older `role='tool'` outputs in place so long multi-round loops fit context."""
    tool_indices = [
        idx for idx, msg in enumerate(messages) if isinstance(msg, dict) and msg.get("role") == "tool"
    ]
    if len(tool_indices) <= keep_recent:
        return
    for idx in tool_indices[:-keep_recent]:
        content = str(messages[idx].get("content") or "")
        if len(content) > max_chars:
            messages[idx]["content"] = (
                content[:max_chars].rstrip() + "\n...[older tool output compacted]"
            )


class ByteyDaemon:
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
        piper_client: PiperTTSClient | None = None,
    ) -> None:
        self.secret_store = SecretStore(config_dir=config_dir)
        self.config_manager = ConfigManager(config_dir=config_dir, secret_store=self.secret_store)
        db_path = resolve_db_path(data_dir) if data_dir is not None else None
        self.storage = StorageManager(db_path=db_path)

        self.llm_router = llm_router or LLMRouter()
        self._owns_llm_router = llm_router is None
        self.mcp_manager = mcp_manager or MCPClientManager(secret_resolver=self.secret_store)
        self._owns_mcp_manager = mcp_manager is None
        cfg_init = self.config_manager.config
        self.mcp_manager.set_workspace_path(cfg_init.workspace.active_path)
        self.skill_registry = SkillRegistry(
            skills_dir=cfg_init.skills.skills_dir,
            extra_dirs=cfg_init.skills.extra_dirs,
            overrides=cfg_init.skills.overrides,
        )
        self.skill_runner = SkillScriptRunner()
        self._rescan_skills()
        self.agent_registry = AgentRegistry()
        self.agent_runner = AgentRunner()
        self._agent_sessions: dict[str, str] = {}
        self.kokoro_client = kokoro_client or KokoroTTSClient()
        self._owns_kokoro = kokoro_client is None
        self.elevenlabs_client = elevenlabs_client or ElevenLabsTTSClient()
        self._owns_elevenlabs = elevenlabs_client is None
        self.piper_client = piper_client or PiperTTSClient()
        self._owns_piper = piper_client is None

        self.ipc = IPCServer(host=host, port=port, on_ws_message=self._on_ws_message)
        self.tts_player = StreamingTTSPlayer(
            get_config=lambda: self.config_manager.config,
            get_secret=self.secret_store.get_secret,
            on_audio_level=self._on_audio_level,
            on_state_change=self._on_subsystem_state_change,
            kokoro_client=self.kokoro_client,
            elevenlabs_client=self.elevenlabs_client,
            piper_client=self.piper_client,
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
        self.idle_annoyed_after_s: float = 3600.0
        self.love_window_s: float = 300.0
        self.love_interaction_threshold: int = 4
        self.focus_data_threshold_chars: int = 3500
        self.astonished_data_threshold_chars: int = 16000
        self.idle_check_interval_s: float = 30.0
        self._last_interaction_wall: float = time.time()
        self._recent_interaction_walls: deque[float] = deque(maxlen=32)
        self._idle_mood_task: asyncio.Task[None] | None = None
        self._active_session_id: str | None = None
        self._speaking_message_id: str | None = None
        self._detected_tools_support: bool = False
        self._detected_context_length: int | None = None
        self._pending_confirmations: dict[str, asyncio.Future[bool]] = {}
        self._chat_lock = asyncio.Lock()
        self._prompt_cancel_event = asyncio.Event()
        self._background_tasks: set[asyncio.Task[Any]] = set()
        self._stop_event = asyncio.Event()
        self._started = False

        self._register_routes()

    def _rescan_skills(self) -> list[SkillDefinition]:
        """Re-scan workspace, user, extra, and built-in skills using current config."""
        cfg = self.config_manager.config
        return self.skill_registry.rescan(
            skills_dir=cfg.skills.skills_dir,
            extra_dirs=cfg.skills.extra_dirs,
            overrides=cfg.skills.overrides,
            workspace_dir=cfg.workspace.active_path,
            auto_load_workspace_skills=cfg.workspace.auto_load_skills,
        )

    def _build_workspace_payload(self) -> dict[str, Any]:
        """Build the current workspace status payload for REST and WebSocket events."""
        cfg = self.config_manager.config
        active_path = (cfg.workspace.active_path or "").strip()
        git_branch = detect_git_branch(Path(active_path)) if active_path else None
        workspace_skills_count = sum(
            1
            for s in self.skill_registry.list_skills(include_disabled=True)
            if s.source == "workspace"
        )
        return {
            "active_path": active_path,
            "recent_paths": list(cfg.workspace.recent_paths),
            "git_branch": git_branch,
            "workspace_skills_count": workspace_skills_count,
        }

    async def _set_active_workspace(
        self, path: str, bind_session: bool = True
    ) -> dict[str, Any]:
        """Validate, persist, and broadcast a new active workspace path (or clear with '')."""
        cleaned = (path or "").strip()
        if cleaned:
            resolved = Path(cleaned).expanduser().resolve()
            if not resolved.is_dir():
                raise ValueError(f"Workspace path is not an existing directory: {path!r}")
            resolved_str = str(resolved)
        else:
            resolved_str = ""

        recent = list(self.config_manager.config.workspace.recent_paths)
        if resolved_str:
            recent = [resolved_str] + [p for p in recent if p != resolved_str]
            recent = recent[:10]

        self.config_manager.update_config(
            {"workspace": {"active_path": resolved_str, "recent_paths": recent}}
        )
        if bind_session and self._active_session_id:
            await self.storage.set_session_workspace(
                self._active_session_id, resolved_str or None
            )

        self.mcp_manager.set_workspace_path(resolved_str)
        self._rescan_skills()
        await self._broadcast_skills_updated()
        payload = self._build_workspace_payload()
        await self.ipc.connection_manager.broadcast(
            {"type": "workspace_updated", **payload}
        )
        return payload

    @staticmethod
    def _serialize_skill(
        skill: SkillDefinition, *, include_content: bool = False
    ) -> dict[str, Any]:
        """Serialize a SkillDefinition for REST API and WebSocket payloads."""
        data: dict[str, Any] = {
            "name": skill.name,
            "description": skill.description,
            "version": skill.version,
            "slash_command": skill.slash_command,
            "requires_confirmation": skill.requires_confirmation,
            "allowed_tools": list(skill.allowed_tools),
            "source": skill.source,
            "path": str(skill.path),
            "enabled": skill.enabled,
            "valid": skill.valid,
            "error": skill.error,
            "editable": skill.editable,
            "has_scripts": skill.has_scripts,
            "scripts": list(skill.scripts),
            "references": list(skill.references),
            "resources": list(skill.resources),
        }
        if include_content:
            data["body"] = skill.body
            data["raw_content"] = skill.raw_content
        return data

    async def _broadcast_skills_updated(self) -> list[dict[str, Any]]:
        skills = [
            self._serialize_skill(s)
            for s in self.skill_registry.list_skills(include_disabled=True)
        ]
        await self.ipc.connection_manager.broadcast(
            {"type": "skills_updated", "skills": skills}
        )
        return skills

    def _match_skill_slash_command(
        self, text: str
    ) -> tuple[SkillDefinition | None, str]:
        """Match `/skill <name> [rest]` or `/<slug> [rest]` against enabled skills."""
        stripped = (text or "").strip()
        if not stripped.startswith("/"):
            return None, stripped

        parts = stripped.split(None, 1)
        head = parts[0]
        tail = parts[1].strip() if len(parts) > 1 else ""
        if head.lower() == "/skill":
            if not tail:
                return None, stripped
            sub_parts = tail.split(None, 1)
            cmd = sub_parts[0]
            rest = sub_parts[1].strip() if len(sub_parts) > 1 else ""
        else:
            cmd = head
            rest = tail

        skill = self.skill_registry.find_by_slash_command(cmd)
        if skill is None:
            return None, stripped
        return skill, (rest if rest else stripped)

    @staticmethod
    def _build_skill_tools(cfg: Any) -> list[dict[str, Any]]:
        """Build OpenAI-style built-in pseudo-tool schemas for skills."""
        tools: list[dict[str, Any]] = [
            {
                "type": "function",
                "function": {
                    "name": "bytey_skill__load",
                    "description": (
                        "Load the full instructions for an enabled skill by name, "
                        "or read a file under its references/ or resources/ directory."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "name": {
                                "type": "string",
                                "description": "Skill slug name from <available_skills>.",
                            },
                            "resource_path": {
                                "type": "string",
                                "description": "Optional relative path under references/ or resources/.",
                            },
                        },
                        "required": ["name"],
                    },
                },
                "server": "bytey_skill",
                "tool": "load",
            }
        ]
        if cfg.skills.allow_script_execution:
            tools.append(
                {
                    "type": "function",
                    "function": {
                        "name": "bytey_skill__run_script",
                        "description": (
                            "Execute a helper script bundled in an enabled skill's scripts/ directory."
                        ),
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "name": {
                                    "type": "string",
                                    "description": "Skill slug name.",
                                },
                                "script": {
                                    "type": "string",
                                    "description": "Script filename or relative path inside scripts/.",
                                },
                                "args": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                    "description": "Optional command-line arguments for the script.",
                                },
                            },
                            "required": ["name", "script"],
                        },
                    },
                    "server": "bytey_skill",
                    "tool": "run_script",
                }
            )
        return tools

    async def _execute_skill_tool(
        self,
        tool_name: str,
        args: dict[str, Any],
        cfg: Any,
        confirm_cb: Any,
    ) -> dict[str, Any]:
        """Execute bytey_skill__load or bytey_skill__run_script."""
        if not cfg.skills.enabled:
            return {"ok": False, "error": "Skills are disabled in configuration."}

        skill_name = str(args.get("name") or "").strip()
        skill = self.skill_registry.get_skill(skill_name)
        if skill is None:
            return {"ok": False, "error": f"Unknown skill: {skill_name!r}"}
        if not skill.enabled:
            return {"ok": False, "error": f"Skill {skill_name!r} is disabled."}
        if not skill.valid:
            return {
                "ok": False,
                "error": f"Skill {skill_name!r} is invalid: {skill.error or 'unknown error'}",
            }

        if tool_name == "load":
            resource_path = str(args.get("resource_path") or "").strip()
            if resource_path:
                try:
                    content = self.skill_registry.read_skill_resource(
                        skill.name, resource_path
                    )
                except (ValueError, FileNotFoundError, OSError) as exc:
                    return {"ok": False, "error": str(exc)}
                return {"ok": True, "content": content}

            max_bytes = 64 * 1024
            body_bytes = skill.body.encode("utf-8")
            if len(body_bytes) > max_bytes:
                body_text = (
                    body_bytes[:max_bytes].decode("utf-8", errors="replace")
                    + "\n\n[Truncated at 64 KiB]"
                )
            else:
                body_text = skill.body
            sections = [f"# Skill: {skill.name}\n{skill.description}\n\n{body_text}"]
            if skill.scripts:
                sections.append(f"Available scripts: {', '.join(skill.scripts)}")
            if skill.references:
                sections.append(f"Available references: {', '.join(skill.references)}")
            if skill.resources:
                sections.append(f"Available resources: {', '.join(skill.resources)}")
            return {"ok": True, "content": "\n\n".join(sections)}

        if tool_name == "run_script":
            if not cfg.skills.allow_script_execution:
                return {
                    "ok": False,
                    "error": "Skill script execution is disabled in configuration.",
                }
            script = str(args.get("script") or "").strip()
            raw_args = args.get("args")
            script_args = (
                [str(a) for a in raw_args] if isinstance(raw_args, list) else []
            )

            override = cfg.skills.overrides.get(skill.name)
            if override is not None and override.require_confirmation is False:
                needs_confirm = False
            else:
                needs_confirm = True

            if needs_confirm:
                approved = await confirm_cb("bytey_skill", "run_script", args)
                if not approved:
                    return {
                        "ok": False,
                        "denied": True,
                        "error": "User denied script execution.",
                    }

            try:
                res = await self.skill_runner.run(skill, script, script_args)
            except (ValueError, FileNotFoundError, PermissionError, OSError) as exc:
                return {"ok": False, "error": str(exc)}

            output_parts = [f"exit_code={res.returncode}"]
            if res.timed_out:
                output_parts.append("timed_out=true")
            if res.stdout:
                output_parts.append(f"stdout:\n{res.stdout}")
            if res.stderr:
                output_parts.append(f"stderr:\n{res.stderr}")
            return {
                "ok": res.returncode == 0 and not res.timed_out,
                "content": "\n".join(output_parts),
                "error": res.stderr if (res.returncode != 0 or res.timed_out) else "",
            }

        return {"ok": False, "error": f"Unknown skill tool: {tool_name!r}"}

    @staticmethod
    def _build_agent_tools() -> list[dict[str, Any]]:
        """Build built-in pseudo-tool schema for delegating prompts to external AI CLIs."""
        return [
            {
                "type": "function",
                "function": {
                    "name": "bytey_agent__run",
                    "description": (
                        "Run a prepared prompt non-interactively in an enabled external AI CLI agent "
                        "(e.g. claude, copilot, agy, gemini, codex, opencode, aider) and return its output."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "agent": {
                                "type": "string",
                                "description": "Target CLI agent ID (e.g. 'claude', 'copilot').",
                            },
                            "prompt": {
                                "type": "string",
                                "description": "Complete self-contained prompt to execute non-interactively.",
                            },
                            "working_dir": {
                                "type": "string",
                                "description": "Optional working directory override; defaults to active workspace.",
                            },
                            "resume": {
                                "type": "boolean",
                                "description": "If true, resume previous CLI session when supported.",
                            },
                        },
                        "required": ["agent", "prompt"],
                    },
                },
            }
        ]

    async def _execute_agent_tool(
        self,
        tool_name: str,
        args: dict[str, Any],
        cfg: Any,
        confirm_cb: Any,
        *,
        call_id: str,
        assistant_id: str,
    ) -> dict[str, Any]:
        """Execute bytey_agent__run with preflight validation, approval gate, and output capture."""
        if tool_name != "run":
            return {"ok": False, "error": f"Unknown agent tool: {tool_name!r}"}

        agent_id = str(args.get("agent") or "").strip().lower()
        prompt = str(args.get("prompt") or "").strip()
        working_dir_arg = str(args.get("working_dir") or "").strip()
        resume = bool(args.get("resume", False))

        if not agent_id:
            return {"ok": False, "error": "Missing required 'agent' argument."}
        if not prompt:
            return {"ok": False, "error": "Missing required 'prompt' argument."}

        if not self.agent_registry.list_agents():
            await self.agent_registry.detect_all(cfg.agents)
        else:
            self.agent_registry.sync_config(cfg.agents)

        info = self.agent_registry.get_agent(agent_id)
        if info is None:
            return {"ok": False, "error": f"Unknown CLI agent: {agent_id!r}"}
        if not info.installed:
            return {"ok": False, "error": f"CLI agent '{agent_id}' is not installed on this machine."}
        if not info.enabled:
            return {"ok": False, "error": f"CLI agent '{agent_id}' is disabled in God Mode settings."}

        cwd_str = working_dir_arg or (cfg.workspace.active_path or "").strip()
        if not cwd_str:
            return {
                "ok": False,
                "error": "No active workspace is set. Select a workspace folder or pass 'working_dir'.",
            }
        cwd = Path(cwd_str).expanduser().resolve()
        if not cwd.is_dir():
            return {"ok": False, "error": f"Working directory does not exist: {cwd_str!r}"}

        resume_sid = self._agent_sessions.get(agent_id, "") if resume else ""
        try:
            argv = self.agent_registry.build_argv(
                agent_id,
                prompt,
                cfg.agents,
                resume_session_id=resume_sid,
            )
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}

        auto_accept = (not cfg.workspace.require_write_approval) and (
            not cfg.workspace.require_command_approval
        )
        if cfg.agents.require_run_approval and not auto_accept:
            argv_preview = " ".join(
                shlex.quote(a if a != prompt else "<prompt>") for a in argv
            )
            extra = {
                "kind": "agent_run",
                "agent": agent_id,
                "model": info.model or "(default)",
                "permission_mode": info.permission_mode,
                "cwd": str(cwd),
                "command": argv_preview,
                "prompt": prompt,
                "diff": (
                    f"Agent: {info.display_name} ({agent_id})\n"
                    f"Model: {info.model or 'CLI default'}\n"
                    f"Permission Mode: {info.permission_mode}\n"
                    f"CWD: {cwd}\n"
                    f"Command: {argv_preview}\n\n"
                    f"--- Prompt ---\n{prompt}"
                ),
            }
            approved = await confirm_cb("bytey_agent", "run", args, extra)
            if not approved:
                return {"ok": False, "denied": True, "error": "User rejected external AI CLI execution."}

        async def _on_tail(tail: str) -> None:
            await self.ipc.connection_manager.broadcast(
                {
                    "type": "tool_call_status",
                    "call_id": call_id,
                    "message_id": assistant_id,
                    "server": "bytey_agent",
                    "tool": "run",
                    "arguments": {"agent": agent_id},
                    "status": "running",
                    "summary": tail,
                }
            )

        res = await self.agent_runner.run(
            agent_id,
            argv,
            cwd,
            timeout_seconds=cfg.agents.timeout_seconds,
            max_output_chars=cfg.agents.max_output_chars,
            on_output_tail=_on_tail,
        )
        if res.session_id:
            self._agent_sessions[agent_id] = res.session_id

        payload = {
            "agent": res.agent,
            "exit_code": res.exit_code,
            "duration_ms": res.duration_ms,
            "timed_out": res.timed_out,
            "cancelled": res.cancelled,
            "auth_hint": res.auth_hint,
            "git_status": res.git_status,
            "stdout": res.stdout,
            "stderr": res.stderr,
        }
        ok = res.exit_code == 0 and not res.timed_out and not res.cancelled
        return {
            "ok": ok,
            "content": json.dumps(payload, ensure_ascii=False),
            "error": res.auth_hint or res.stderr or (f"exit_code={res.exit_code}" if not ok else ""),
        }

    @staticmethod
    def _build_workspace_tools() -> list[dict[str, Any]]:
        """Build OpenAI-style built-in pseudo-tool schemas for active workspace operations."""
        return [
            {
                "type": "function",
                "function": {
                    "name": "bytey_workspace__list_files",
                    "description": "List files inside the active workspace directory.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "rel_path": {
                                "type": "string",
                                "description": "Relative directory path inside the workspace (default '.').",
                            },
                            "max_depth": {
                                "type": "integer",
                                "description": "Maximum directory traversal depth (default 3).",
                            },
                        },
                    },
                },
                "server": "bytey_workspace",
                "tool": "list_files",
            },
            {
                "type": "function",
                "function": {
                    "name": "bytey_workspace__grep_search",
                    "description": "Search workspace files for a regex pattern.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "pattern": {
                                "type": "string",
                                "description": "Regular expression pattern to search for.",
                            },
                            "rel_path": {
                                "type": "string",
                                "description": "Relative file or directory path inside the workspace (default '.').",
                            },
                        },
                        "required": ["pattern"],
                    },
                },
                "server": "bytey_workspace",
                "tool": "grep_search",
            },
            {
                "type": "function",
                "function": {
                    "name": "bytey_workspace__read_file",
                    "description": "Read numbered lines from a file inside the active workspace.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "rel_path": {
                                "type": "string",
                                "description": "Relative path to the file inside the workspace.",
                            },
                            "start_line": {
                                "type": "integer",
                                "description": "1-based starting line number (default 1).",
                            },
                            "end_line": {
                                "type": "integer",
                                "description": "1-based ending line number (default 200).",
                            },
                        },
                        "required": ["rel_path"],
                    },
                },
                "server": "bytey_workspace",
                "tool": "read_file",
            },
            {
                "type": "function",
                "function": {
                    "name": "bytey_workspace__propose_edit",
                    "description": "Replace an exact target snippet in an existing workspace file.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "rel_path": {
                                "type": "string",
                                "description": "Relative path to the file inside the workspace.",
                            },
                            "target": {
                                "type": "string",
                                "description": "Exact existing text to replace.",
                            },
                            "replacement": {
                                "type": "string",
                                "description": "Replacement text.",
                            },
                        },
                        "required": ["rel_path", "target", "replacement"],
                    },
                },
                "server": "bytey_workspace",
                "tool": "propose_edit",
            },
            {
                "type": "function",
                "function": {
                    "name": "bytey_workspace__write_file",
                    "description": (
                        "Create a new file inside the active workspace, or completely replace an "
                        "existing file when overwrite=true. Do NOT use for partial edits or "
                        "appending to existing files—use bytey_workspace__propose_edit or "
                        "bytey_workspace__append_file instead."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "rel_path": {
                                "type": "string",
                                "description": "Relative path to the file inside the workspace.",
                            },
                            "content": {
                                "type": "string",
                                "description": "Full file content to write.",
                            },
                            "overwrite": {
                                "type": "boolean",
                                "description": "Set true only to completely replace an existing file. Default false.",
                            },
                        },
                        "required": ["rel_path", "content"],
                    },
                },
                "server": "bytey_workspace",
                "tool": "write_file",
            },
            {
                "type": "function",
                "function": {
                    "name": "bytey_workspace__append_file",
                    "description": "Append content to the end of an existing workspace file (or create it if missing).",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "rel_path": {
                                "type": "string",
                                "description": "Relative path to the file inside the workspace.",
                            },
                            "content": {
                                "type": "string",
                                "description": "Content to append to the end of the file.",
                            },
                        },
                        "required": ["rel_path", "content"],
                    },
                },
                "server": "bytey_workspace",
                "tool": "append_file",
            },
            {
                "type": "function",
                "function": {
                    "name": "bytey_workspace__delete_file",
                    "description": "Delete a file inside the active workspace.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "rel_path": {
                                "type": "string",
                                "description": "Relative path to the file to delete.",
                            },
                        },
                        "required": ["rel_path"],
                    },
                },
                "server": "bytey_workspace",
                "tool": "delete_file",
            },
            {
                "type": "function",
                "function": {
                    "name": "bytey_workspace__rename_file",
                    "description": "Rename or move a file inside the active workspace.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "src_rel_path": {
                                "type": "string",
                                "description": "Relative path to the existing source file.",
                            },
                            "dst_rel_path": {
                                "type": "string",
                                "description": "Relative destination path inside the workspace.",
                            },
                            "overwrite": {
                                "type": "boolean",
                                "description": "Whether to overwrite dst_rel_path if it already exists.",
                                "default": False,
                            },
                        },
                        "required": ["src_rel_path", "dst_rel_path"],
                    },
                },
                "server": "bytey_workspace",
                "tool": "rename_file",
            },
            {
                "type": "function",
                "function": {
                    "name": "bytey_workspace__run_command",
                    "description": "Execute a shell command inside the active workspace root.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "command": {
                                "type": "string",
                                "description": "Shell command to run in the workspace directory.",
                            },
                        },
                        "required": ["command"],
                    },
                },
                "server": "bytey_workspace",
                "tool": "run_command",
            },
        ]

    async def _execute_workspace_tool(
        self,
        tool_name: str,
        args: dict[str, Any],
        cfg: Any,
        confirm_cb: Any,
    ) -> dict[str, Any]:
        """Execute a bytey_workspace__* tool call with optional user approval."""
        active_ws = (cfg.workspace.active_path or "").strip()
        if not active_ws:
            return {"ok": False, "error": "No active workspace is configured."}
        try:
            toolchain = WorkspaceToolchain(active_ws)
            if tool_name == "list_files":
                rel_path = str(args.get("rel_path") or ".")
                max_depth = int(args.get("max_depth", 3))
                entries = toolchain.list_files(rel_path=rel_path, max_depth=max_depth)
                return {
                    "ok": True,
                    "content": "\n".join(entries) if entries else "(no files found)",
                }
            if tool_name == "grep_search":
                pattern = str(args.get("pattern") or "")
                rel_path = str(args.get("rel_path") or ".")
                matches = toolchain.grep_search(pattern=pattern, rel_path=rel_path)
                lines = [f"{m['path']}:{m['line']}: {m['text']}" for m in matches]
                return {
                    "ok": True,
                    "content": "\n".join(lines) if lines else "(no matches)",
                }
            if tool_name == "read_file":
                rel_path = str(args.get("rel_path") or "")
                start_line = int(args.get("start_line", 1))
                end_line = int(args.get("end_line", 200))
                content = toolchain.read_file(
                    rel_path=rel_path, start_line=start_line, end_line=end_line
                )
                return {"ok": True, "content": content}
            if tool_name == "propose_edit":
                rel_path = str(args.get("rel_path") or "")
                target = str(args.get("target") or "")
                replacement = str(args.get("replacement") or "")
                preview = toolchain.preview_edit(rel_path, target, replacement)
                if not preview.get("ok"):
                    return {
                        "ok": False,
                        "error": str(preview.get("error") or "Edit preview failed"),
                    }
                if cfg.workspace.require_write_approval:
                    approved = await confirm_cb(
                        "bytey_workspace",
                        "propose_edit",
                        args,
                        {"diff": preview["diff"], "rel_path": preview["rel_path"]},
                    )
                    if not approved:
                        return {
                            "ok": False,
                            "denied": True,
                            "error": "User rejected the proposed change/command.",
                        }
                res = toolchain.apply_edit(rel_path, target, replacement)
                if not res.get("ok"):
                    return {
                        "ok": False,
                        "error": str(res.get("error") or "Edit failed"),
                    }
                return {"ok": True, "content": res.get("diff") or f"Edited {rel_path}"}
            if tool_name == "write_file":
                rel_path = str(args.get("rel_path") or "")
                content = str(args.get("content") or "")
                overwrite = bool(args.get("overwrite", False))
                preview = toolchain.preview_write(rel_path, content, overwrite=overwrite)
                if not preview.get("ok"):
                    return {
                        "ok": False,
                        "error": str(preview.get("error") or "Write preview failed"),
                    }
                if cfg.workspace.require_write_approval:
                    approved = await confirm_cb(
                        "bytey_workspace",
                        "write_file",
                        args,
                        {"diff": preview["diff"], "rel_path": preview["rel_path"]},
                    )
                    if not approved:
                        return {
                            "ok": False,
                            "denied": True,
                            "error": "User rejected the proposed change/command.",
                        }
                res = toolchain.apply_write(rel_path, content, overwrite=overwrite)
                if not res.get("ok"):
                    return {
                        "ok": False,
                        "error": str(res.get("error") or "Write failed"),
                    }
                return {"ok": True, "content": res.get("diff") or f"Wrote {rel_path}"}
            if tool_name == "append_file":
                rel_path = str(args.get("rel_path") or "")
                content = str(args.get("content") or "")
                preview = toolchain.preview_append(rel_path, content)
                if not preview.get("ok"):
                    return {
                        "ok": False,
                        "error": str(preview.get("error") or "Append preview failed"),
                    }
                if cfg.workspace.require_write_approval:
                    approved = await confirm_cb(
                        "bytey_workspace",
                        "append_file",
                        args,
                        {"diff": preview["diff"], "rel_path": preview["rel_path"]},
                    )
                    if not approved:
                        return {
                            "ok": False,
                            "denied": True,
                            "error": "User rejected the proposed change/command.",
                        }
                res = toolchain.apply_append(rel_path, content)
                if not res.get("ok"):
                    return {
                        "ok": False,
                        "error": str(res.get("error") or "Append failed"),
                    }
                return {"ok": True, "content": res.get("diff") or f"Appended to {rel_path}"}
            if tool_name == "delete_file":
                rel_path = str(args.get("rel_path") or "")
                preview = toolchain.preview_delete(rel_path)
                if not preview.get("ok"):
                    return {
                        "ok": False,
                        "error": str(preview.get("error") or "Delete preview failed"),
                    }
                if cfg.workspace.require_write_approval:
                    approved = await confirm_cb(
                        "bytey_workspace",
                        "delete_file",
                        args,
                        {"diff": preview["diff"], "rel_path": preview["rel_path"]},
                    )
                    if not approved:
                        return {
                            "ok": False,
                            "denied": True,
                            "error": "User rejected the proposed change/command.",
                        }
                res = toolchain.apply_delete(rel_path)
                if not res.get("ok"):
                    return {
                        "ok": False,
                        "error": str(res.get("error") or "Delete failed"),
                    }
                return {"ok": True, "content": res.get("diff") or f"Deleted {rel_path}"}
            if tool_name == "rename_file":
                src_rel_path = str(args.get("src_rel_path") or "")
                dst_rel_path = str(args.get("dst_rel_path") or "")
                overwrite = bool(args.get("overwrite", False))
                preview = toolchain.preview_rename(
                    src_rel_path, dst_rel_path, overwrite=overwrite
                )
                if not preview.get("ok"):
                    return {
                        "ok": False,
                        "error": str(preview.get("error") or "Rename preview failed"),
                    }
                if cfg.workspace.require_write_approval:
                    approved = await confirm_cb(
                        "bytey_workspace",
                        "rename_file",
                        args,
                        {"diff": preview["diff"], "rel_path": preview["rel_path"]},
                    )
                    if not approved:
                        return {
                            "ok": False,
                            "denied": True,
                            "error": "User rejected the proposed change/command.",
                        }
                res = toolchain.apply_rename(
                    src_rel_path, dst_rel_path, overwrite=overwrite
                )
                if not res.get("ok"):
                    return {
                        "ok": False,
                        "error": str(res.get("error") or "Rename failed"),
                    }
                return {
                    "ok": True,
                    "content": res.get("diff") or f"Renamed {src_rel_path} -> {dst_rel_path}",
                }
            if tool_name == "run_command":
                command = str(args.get("command") or "").strip()
                if not command:
                    return {"ok": False, "error": "Command must not be empty"}
                if cfg.workspace.require_command_approval:
                    approved = await confirm_cb(
                        "bytey_workspace",
                        "run_command",
                        args,
                        {"command": command, "rel_path": "."},
                    )
                    if not approved:
                        return {
                            "ok": False,
                            "denied": True,
                            "error": "User rejected the proposed change/command.",
                        }
                res = await toolchain.run_command(command)
                parts = [f"exit_code={res['returncode']}"]
                if res.get("timed_out"):
                    parts.append("timed_out=true")
                if res.get("stdout"):
                    parts.append(f"stdout:\n{res['stdout']}")
                if res.get("stderr"):
                    parts.append(f"stderr:\n{res['stderr']}")
                ok = bool(res.get("ok"))
                return {
                    "ok": ok,
                    "content": "\n".join(parts),
                    "error": str(res.get("stderr") or "") if not ok else "",
                }
        except (ValueError, FileNotFoundError, NotADirectoryError, PermissionError, OSError) as exc:
            return {"ok": False, "error": str(exc)}

    @staticmethod
    def _build_git_tools() -> list[dict[str, Any]]:
        """Build OpenAI-style built-in pseudo-tool schemas for workspace Git operations."""
        specs: list[tuple[str, str, dict[str, Any], list[str]]] = [
            (
                "status",
                "Show Git working tree status for the active workspace.",
                {"short": {"type": "boolean", "description": "Use short status with branch (default true).", "default": True}},
                [],
            ),
            (
                "diff",
                "Show Git diff for unstaged, staged, or ref-to-ref changes.",
                {
                    "staged": {"type": "boolean", "description": "Show staged diff (--staged).", "default": False},
                    "ref": {"type": "string", "description": "Base git ref/commit."},
                    "target_ref": {"type": "string", "description": "Optional target git ref/commit."},
                    "rel_path": {"type": "string", "description": "Optional relative file/dir path."},
                    "stat_only": {"type": "boolean", "description": "Show diffstat only (--stat).", "default": False},
                },
                [],
            ),
            (
                "log",
                "Show Git commit history for the active workspace.",
                {
                    "limit": {"type": "integer", "description": "Max commits to show (1-200, default 20).", "default": 20},
                    "ref": {"type": "string", "description": "Branch, tag, or commit ref."},
                    "rel_path": {"type": "string", "description": "Optional relative file/dir path."},
                    "oneline": {"type": "boolean", "description": "Format one line per commit (default true).", "default": True},
                    "author": {"type": "string", "description": "Filter commits by author."},
                    "since": {"type": "string", "description": "Filter commits since date/time (e.g. '1 week ago')."},
                },
                [],
            ),
            (
                "show",
                "Inspect a Git commit, tag, or object.",
                {
                    "ref": {"type": "string", "description": "Git ref/commit (default HEAD).", "default": "HEAD"},
                    "stat_only": {"type": "boolean", "description": "Show diffstat only (--stat).", "default": False},
                    "rel_path": {"type": "string", "description": "Optional relative file path."},
                },
                [],
            ),
            (
                "blame",
                "Show line-by-line Git authorship for a workspace file.",
                {
                    "rel_path": {"type": "string", "description": "Relative path to file."},
                    "start_line": {"type": "integer", "description": "1-based start line (default 1).", "default": 1},
                    "end_line": {"type": "integer", "description": "1-based end line (default 200).", "default": 200},
                },
                ["rel_path"],
            ),
            (
                "branch_list",
                "List local and optionally remote Git branches.",
                {"include_remote": {"type": "boolean", "description": "Include remote-tracking branches (default true).", "default": True}},
                [],
            ),
            (
                "tag_list",
                "List Git tags in the repository.",
                {"pattern": {"type": "string", "description": "Optional glob pattern to filter tags."}},
                [],
            ),
            ("stash_list", "List Git stashes.", {}, []),
            ("remote_list", "List configured Git remotes.", {}, []),
            (
                "init",
                "Initialize a new Git repository in the active workspace.",
                {"default_branch": {"type": "string", "description": "Optional initial branch name (e.g. main)."}},
                [],
            ),
            (
                "stage",
                "Stage workspace files for commit (git add).",
                {
                    "paths": {"type": "array", "items": {"type": "string"}, "description": "Relative paths to stage."},
                    "all": {"type": "boolean", "description": "Stage all changes (git add -A).", "default": False},
                },
                [],
            ),
            (
                "unstage",
                "Unstage workspace files from the Git index.",
                {"paths": {"type": "array", "items": {"type": "string"}, "description": "Relative paths to unstage (default all)."}},
                [],
            ),
            (
                "commit",
                "Create a Git commit in the active workspace.",
                {
                    "message": {"type": "string", "description": "Commit message."},
                    "amend": {"type": "boolean", "description": "Amend the previous commit.", "default": False},
                    "all": {"type": "boolean", "description": "Automatically stage modified/deleted tracked files (-a).", "default": False},
                },
                [],
            ),
            (
                "branch_create",
                "Create a new Git branch and optionally check it out.",
                {
                    "name": {"type": "string", "description": "New branch name."},
                    "start_point": {"type": "string", "description": "Optional base ref/commit."},
                    "checkout": {"type": "boolean", "description": "Switch to the new branch (default true).", "default": True},
                },
                ["name"],
            ),
            (
                "checkout",
                "Switch to an existing Git branch or ref.",
                {"ref": {"type": "string", "description": "Target branch, tag, or commit ref."}},
                ["ref"],
            ),
            (
                "branch_delete",
                "Delete a local Git branch.",
                {
                    "name": {"type": "string", "description": "Branch name to delete."},
                    "force": {"type": "boolean", "description": "Force delete (-D).", "default": False},
                },
                ["name"],
            ),
            (
                "merge",
                "Merge a branch into the current branch, or abort an in-progress merge.",
                {
                    "branch": {"type": "string", "description": "Branch to merge."},
                    "no_ff": {"type": "boolean", "description": "Create a merge commit (--no-ff).", "default": False},
                    "abort": {"type": "boolean", "description": "Abort current conflicted merge (--abort).", "default": False},
                },
                [],
            ),
            (
                "rebase",
                "Rebase current branch onto another ref, or continue/abort rebase.",
                {
                    "onto": {"type": "string", "description": "Upstream branch/ref."},
                    "abort": {"type": "boolean", "description": "Abort in-progress rebase (--abort).", "default": False},
                    "continue": {"type": "boolean", "description": "Continue rebase (--continue).", "default": False},
                },
                [],
            ),
            (
                "cherry_pick",
                "Apply a commit onto the current branch, or continue/abort cherry-pick.",
                {
                    "commit": {"type": "string", "description": "Commit hash/ref to cherry-pick."},
                    "abort": {"type": "boolean", "description": "Abort in-progress cherry-pick (--abort).", "default": False},
                    "continue": {"type": "boolean", "description": "Continue cherry-pick (--continue).", "default": False},
                },
                [],
            ),
            (
                "stash",
                "Save, pop, apply, or drop a Git stash.",
                {
                    "action": {"type": "string", "enum": ["push", "pop", "apply", "drop"], "description": "Stash action (default push).", "default": "push"},
                    "message": {"type": "string", "description": "Optional stash message (for push)."},
                    "ref": {"type": "string", "description": "Optional stash ref like stash@{0}."},
                    "include_untracked": {"type": "boolean", "description": "Include untracked files (-u, for push).", "default": False},
                },
                [],
            ),
            (
                "tag_create",
                "Create a lightweight or annotated Git tag.",
                {
                    "name": {"type": "string", "description": "Tag name."},
                    "message": {"type": "string", "description": "Optional annotation message."},
                    "ref": {"type": "string", "description": "Optional target commit ref."},
                },
                ["name"],
            ),
            (
                "restore",
                "Discard working tree changes for specified paths (git restore).",
                {
                    "paths": {"type": "array", "items": {"type": "string"}, "description": "Relative paths to restore."},
                    "source": {"type": "string", "description": "Optional source ref (--source)."},
                },
                ["paths"],
            ),
            (
                "reset",
                "Reset current HEAD to a specified state (soft, mixed, or hard).",
                {
                    "ref": {"type": "string", "description": "Target ref (default HEAD).", "default": "HEAD"},
                    "mode": {"type": "string", "enum": ["soft", "mixed", "hard"], "description": "Reset mode (default mixed).", "default": "mixed"},
                },
                [],
            ),
            (
                "fetch",
                "Fetch objects and refs from a remote repository.",
                {
                    "remote": {"type": "string", "description": "Remote name (default origin).", "default": "origin"},
                    "prune": {"type": "boolean", "description": "Prune deleted remote-tracking branches (--prune).", "default": False},
                    "all": {"type": "boolean", "description": "Fetch all remotes (--all).", "default": False},
                },
                [],
            ),
            (
                "pull",
                "Fetch from and integrate with another repository or local branch.",
                {
                    "remote": {"type": "string", "description": "Remote name."},
                    "branch": {"type": "string", "description": "Remote branch name."},
                    "rebase": {"type": "boolean", "description": "Rebase instead of merge (--rebase).", "default": False},
                },
                [],
            ),
            (
                "push",
                "Update remote refs along with associated objects.",
                {
                    "remote": {"type": "string", "description": "Remote name (default origin).", "default": "origin"},
                    "branch": {"type": "string", "description": "Branch name to push."},
                    "set_upstream": {"type": "boolean", "description": "Set upstream tracking (-u).", "default": False},
                    "force_with_lease": {"type": "boolean", "description": "Use --force-with-lease.", "default": False},
                },
                [],
            ),
        ]
        tools: list[dict[str, Any]] = []
        for name, desc, props, req in specs:
            params: dict[str, Any] = {"type": "object", "properties": props}
            if req:
                params["required"] = req
            tools.append(
                {
                    "type": "function",
                    "function": {
                        "name": f"bytey_git__{name}",
                        "description": desc,
                        "parameters": params,
                    },
                    "server": "bytey_git",
                    "tool": name,
                }
            )
        return tools

    async def _execute_git_tool(
        self,
        tool_name: str,
        args: dict[str, Any],
        cfg: Any,
        confirm_cb: Any,
    ) -> dict[str, Any]:
        """Execute a bytey_git__* tool call with confirmation gating."""
        active_ws = (cfg.workspace.active_path or "").strip()
        if not active_ws:
            return {"ok": False, "error": "No active workspace is configured."}
        try:
            git = GitToolchain(active_ws)
            needs_confirm = (
                tool_name in ALWAYS_CONFIRM_GIT_TOOLS
                or (tool_name not in READ_ONLY_GIT_TOOLS and bool(cfg.workspace.require_write_approval))
            )
            if needs_confirm:
                approved = await confirm_cb(
                    "bytey_git",
                    tool_name,
                    args,
                    {"command": f"git {tool_name}", "rel_path": "."},
                )
                if not approved:
                    return {
                        "ok": False,
                        "denied": True,
                        "error": "User rejected the proposed Git operation.",
                    }

            handlers = {
                "status": lambda: git.status(short=bool(args.get("short", True))),
                "diff": lambda: git.diff(
                    staged=bool(args.get("staged", False)),
                    ref=str(args.get("ref") or ""),
                    target_ref=str(args.get("target_ref") or ""),
                    rel_path=str(args.get("rel_path") or ""),
                    stat_only=bool(args.get("stat_only", False)),
                ),
                "log": lambda: git.log(
                    limit=int(args.get("limit", 20)),
                    ref=str(args.get("ref") or ""),
                    rel_path=str(args.get("rel_path") or ""),
                    oneline=bool(args.get("oneline", True)),
                    author=str(args.get("author") or ""),
                    since=str(args.get("since") or ""),
                ),
                "show": lambda: git.show(
                    ref=str(args.get("ref") or "HEAD"),
                    stat_only=bool(args.get("stat_only", False)),
                    rel_path=str(args.get("rel_path") or ""),
                ),
                "blame": lambda: git.blame(
                    rel_path=str(args.get("rel_path") or ""),
                    start_line=int(args.get("start_line", 1)),
                    end_line=int(args.get("end_line", 200)),
                ),
                "branch_list": lambda: git.branch_list(include_remote=bool(args.get("include_remote", True))),
                "tag_list": lambda: git.tag_list(pattern=str(args.get("pattern") or "")),
                "stash_list": lambda: git.stash_list(),
                "remote_list": lambda: git.remote_list(),
                "init": lambda: git.init(default_branch=str(args.get("default_branch") or "")),
                "stage": lambda: git.stage(
                    paths=list(args.get("paths") or []),
                    all_files=bool(args.get("all", False)),
                ),
                "unstage": lambda: git.unstage(paths=list(args.get("paths") or [])),
                "commit": lambda: git.commit(
                    message=str(args.get("message") or ""),
                    amend=bool(args.get("amend", False)),
                    all_tracked=bool(args.get("all", False)),
                ),
                "branch_create": lambda: git.branch_create(
                    name=str(args.get("name") or ""),
                    start_point=str(args.get("start_point") or ""),
                    checkout=bool(args.get("checkout", True)),
                ),
                "checkout": lambda: git.checkout(ref=str(args.get("ref") or "")),
                "branch_delete": lambda: git.branch_delete(
                    name=str(args.get("name") or ""),
                    force=bool(args.get("force", False)),
                ),
                "merge": lambda: git.merge(
                    branch=str(args.get("branch") or ""),
                    no_ff=bool(args.get("no_ff", False)),
                    abort=bool(args.get("abort", False)),
                ),
                "rebase": lambda: git.rebase(
                    onto=str(args.get("onto") or ""),
                    abort=bool(args.get("abort", False)),
                    continue_rebase=bool(args.get("continue", False)),
                ),
                "cherry_pick": lambda: git.cherry_pick(
                    commit=str(args.get("commit") or ""),
                    abort=bool(args.get("abort", False)),
                    continue_cp=bool(args.get("continue", False)),
                ),
                "stash": lambda: git.stash(
                    action=str(args.get("action") or "push"),
                    message=str(args.get("message") or ""),
                    ref=str(args.get("ref") or ""),
                    include_untracked=bool(args.get("include_untracked", False)),
                ),
                "tag_create": lambda: git.tag_create(
                    name=str(args.get("name") or ""),
                    message=str(args.get("message") or ""),
                    ref=str(args.get("ref") or ""),
                ),
                "restore": lambda: git.restore(
                    paths=list(args.get("paths") or []),
                    source=str(args.get("source") or ""),
                ),
                "reset": lambda: git.reset(
                    ref=str(args.get("ref") or "HEAD"),
                    mode=str(args.get("mode") or "mixed"),
                ),
                "fetch": lambda: git.fetch(
                    remote=str(args.get("remote") or "origin"),
                    prune=bool(args.get("prune", False)),
                    all_remotes=bool(args.get("all", False)),
                ),
                "pull": lambda: git.pull(
                    remote=str(args.get("remote") or ""),
                    branch=str(args.get("branch") or ""),
                    rebase=bool(args.get("rebase", False)),
                ),
                "push": lambda: git.push(
                    remote=str(args.get("remote") or "origin"),
                    branch=str(args.get("branch") or ""),
                    set_upstream=bool(args.get("set_upstream", False)),
                    force_with_lease=bool(args.get("force_with_lease", False)),
                ),
            }
            handler = handlers.get(tool_name)
            if handler is None:
                return {"ok": False, "error": f"Unknown Git tool: {tool_name}"}
            return await handler()
        except (ValueError, FileNotFoundError, NotADirectoryError, PermissionError, OSError) as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": False, "error": f"Unknown workspace tool: {tool_name!r}"}

    def _track_task(self, coro: Any) -> asyncio.Task[Any]:
        task = asyncio.create_task(coro)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return task

    async def _detect_active_model_capabilities(
        self,
        cfg: Any,
        api_key: str | None,
    ) -> tuple[bool, int | None]:
        """Detect (supports_tools, context_length) for the active provider and model."""
        try:
            detect_caps = getattr(self.llm_router, "detect_model_capabilities", None)
            if callable(detect_caps):
                return await detect_caps(
                    cfg.provider.active_provider,
                    cfg.provider.model,
                    override=cfg.provider.supports_tools_override,
                    base_url=cfg.provider.base_url,
                    api_key=api_key,
                )
            supported = await self.llm_router.detect_tool_support(
                cfg.provider.active_provider,
                cfg.provider.model,
                override=cfg.provider.supports_tools_override,
                base_url=cfg.provider.base_url,
                api_key=api_key,
            )
            return bool(supported), None
        except Exception:
            return bool(cfg.provider.supports_tools_override), None

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

        (
            self._detected_tools_support,
            self._detected_context_length,
        ) = await self._detect_active_model_capabilities(
            cfg,
            self._get_provider_api_key(cfg.provider.active_provider),
        )
        await self.agent_registry.detect_all(cfg.agents)

        await self.ipc.start()
        if self.enable_voice_capture:
            await self.voice_input.start()
        self._idle_mood_task = self._track_task(self._idle_mood_loop())
        self._started = True
        logger.info("Bytey AI Companion daemon listening on http://%s:%s", self.ipc.host, self.ipc.port)

    def _reject_pending_confirmations(self) -> int:
        """Resolve all pending tool confirmation futures as rejected."""
        rejected = 0
        for call_id, fut in list(self._pending_confirmations.items()):
            if not fut.done():
                fut.set_result(False)
                rejected += 1
            self._pending_confirmations.pop(call_id, None)
        return rejected

    async def stop(self) -> None:
        """Gracefully shut down all subsystems, subprocesses, and sockets."""
        if not self._started and self._stop_event.is_set():
            return
        self._started = False
        self._stop_event.set()

        self._reject_pending_confirmations()

        self._cancel_emotion_reset()
        if self._idle_mood_task is not None and not self._idle_mood_task.done():
            self._idle_mood_task.cancel()
        self._idle_mood_task = None

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
        await self.agent_runner.cancel()
        if self._owns_mcp_manager:
            await self.mcp_manager.close_all()
        if self._owns_llm_router:
            await self.llm_router.close()
        if self._owns_kokoro:
            await self.kokoro_client.close()
        if self._owns_elevenlabs:
            await self.elevenlabs_client.close()
        if self._owns_piper:
            await self.piper_client.close()
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
            "context_size": cfg.provider.context_size,
            "max_context_size": self._detected_context_length,
            "max_tool_rounds": cfg.provider.max_tool_rounds,
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
            "piper_model": cfg.voice_out.piper_model,
            "piper_models_dir": cfg.voice_out.piper_models_dir,
            "piper_speed": cfg.voice_out.piper_speed,
            "piper_pitch": cfg.voice_out.piper_pitch,
            "piper_noise_scale": cfg.voice_out.piper_noise_scale,
            "piper_speaker_id": cfg.voice_out.piper_speaker_id,
            "elevenlabs_voice_id": cfg.voice_out.elevenlabs_voice_id,
            "elevenlabs_model_id": cfg.voice_out.elevenlabs_model_id,
        }
        data["mcpServers"] = mcp_ui
        data["default_system_prompt"] = DEFAULT_SYSTEM_PROMPT
        return data

    def _normalize_patch(self, raw_patch: dict[str, Any]) -> tuple[dict[str, Any], bool | None]:
        """Translate nested/alias/flat UI patches into canonical ByteyConfig patches."""
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
        if isinstance(raw_patch.get("skills"), dict):
            merge_section("skills", raw_patch["skills"])
        if isinstance(raw_patch.get("workspace"), dict):
            merge_section("workspace", raw_patch["workspace"])
        if isinstance(raw_patch.get("agents"), dict):
            merge_section("agents", raw_patch["agents"])

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
            if "context_size" in llm:
                mapped["context_size"] = llm["context_size"]
            if "max_tool_rounds" in llm:
                mapped["max_tool_rounds"] = llm["max_tool_rounds"]
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
                "piper_model",
                "piper_models_dir",
                "piper_speed",
                "piper_pitch",
                "piper_noise_scale",
                "piper_speaker_id",
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
            "speaking_message_id": self._speaking_message_id,
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

    @staticmethod
    def evaluate_idle_emotion(
        idle_seconds: float,
        local_hour: int,
        *,
        threshold_s: float = 3600.0,
    ) -> str | None:
        """Return idle emotion when inactive > 60m: annoyed (8AM-8PM) or sleepy (8PM-8AM)."""
        if idle_seconds < threshold_s:
            return None
        if 8 <= local_hour < 20:
            return "annoyed"
        return "sleepy"

    def _record_interaction(self, now_wall: float | None = None) -> tuple[float, int]:
        """Record a user interaction timestamp and return (idle_seconds_before, recent_5m_count)."""
        now = time.time() if now_wall is None else float(now_wall)
        idle_before_s = max(0.0, now - self._last_interaction_wall)
        self._last_interaction_wall = now
        self._recent_interaction_walls.append(now)
        cutoff = now - self.love_window_s
        while self._recent_interaction_walls and self._recent_interaction_walls[0] < cutoff:
            self._recent_interaction_walls.popleft()
        return idle_before_s, len(self._recent_interaction_walls)

    async def _idle_mood_loop(self) -> None:
        """Periodically update idle emotion after prolonged user inactivity."""
        try:
            while not self._stop_event.is_set():
                await asyncio.sleep(max(1.0, float(self.idle_check_interval_s)))
                if not self._started or self._chat_lock.locked():
                    continue
                if self.state not in ("IDLE", "MUTED"):
                    continue
                idle_s = max(0.0, time.time() - self._last_interaction_wall)
                target_emotion = self.evaluate_idle_emotion(
                    idle_s,
                    datetime.now().hour,
                    threshold_s=self.idle_annoyed_after_s,
                )
                if target_emotion and self.emotion != target_emotion:
                    await self._set_state(self.state, emotion=target_emotion)
        except asyncio.CancelledError:
            pass

    async def _on_subsystem_state_change(self, state: str) -> None:
        prev_state = self.state
        norm = str(state or "IDLE").upper()
        if norm == "IDLE" and self.config_manager.config.voice_in.mic_muted:
            norm = "MUTED"
        if norm == "SPEAKING":
            await self.ipc.connection_manager.broadcast(
                {
                    "type": "tts_playback",
                    "status": "playing",
                    "message_id": self._speaking_message_id,
                }
            )
        elif prev_state == "SPEAKING" and norm in ("IDLE", "MUTED"):
            self._speaking_message_id = None
            await self.ipc.connection_manager.broadcast(
                {
                    "type": "tts_playback",
                    "status": "idle",
                    "message_id": None,
                }
            )
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

    async def _broadcast_sessions_updated(self) -> None:
        sessions = await self.storage.list_sessions()
        await self.ipc.connection_manager.broadcast(
            {
                "type": "sessions_updated",
                "sessions": sessions,
                "active_session_id": self._active_session_id,
            }
        )

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
        extra_payload: dict[str, Any] | None = None,
    ) -> bool:
        if self._prompt_cancel_event.is_set():
            return False
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[bool] = loop.create_future()
        self._pending_confirmations[call_id] = fut

        await self.storage.update_tool_call(call_id, "awaiting_confirm")
        payload: dict[str, Any] = {
            "type": "tool_call_status",
            "call_id": call_id,
            "message_id": message_id,
            "server": server_name,
            "tool": tool_name,
            "arguments": arguments,
            "status": "awaiting_confirm",
            "summary": "Waiting for user approval...",
        }
        if extra_payload:
            payload.update(extra_payload)
        await self.ipc.connection_manager.broadcast(payload)
        try:
            approved = await asyncio.wait_for(fut, timeout=60.0)
            return bool(approved) and not self._prompt_cancel_event.is_set()
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
            session_obj = await self.storage.get_session(sid)
            needs_auto_title = session_obj is not None and session_obj.get("title") == "New Chat"
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

            ws_parts = text.split(None, 1)
            ws_cmd = ws_parts[0].lower()
            if ws_cmd in ("/god", "/godmode"):
                arg = ws_parts[1].strip().lower() if len(ws_parts) > 1 else ""
                cur_god = bool(self.config_manager.config.agents.god_mode_enabled)
                if not arg:
                    next_god = not cur_god
                elif arg in ("on", "true", "1", "enable"):
                    next_god = True
                elif arg in ("off", "false", "0", "disable"):
                    next_god = False
                else:
                    next_god = cur_god
                prev_muted = self.config_manager.config.voice_in.mic_muted
                updated = self.config_manager.update_config(
                    {"agents": {"god_mode_enabled": next_god}}
                )
                await self._finalize_config_change(
                    updated,
                    prev_muted,
                    refresh_provider=False,
                    broadcast_visuals=False,
                    tools_hint=None,
                )
                reply_emotion = "happy" if next_god else "neutral"
                reply_text = f"God Mode {'enabled' if next_god else 'disabled'}."
                reply_msg = await self.storage.add_message(
                    sid,
                    role="assistant",
                    content=reply_text,
                    input_mode=input_mode,
                    emotion=reply_emotion,
                )
                await self.ipc.connection_manager.broadcast(
                    {
                        "type": "transcript_delta",
                        "session_id": sid,
                        "message_id": reply_msg["id"],
                        "role": "assistant",
                        "source": input_mode,
                        "text": reply_text,
                        "emotion": reply_emotion,
                        "is_final": True,
                    }
                )
                return {
                    "session_id": sid,
                    "user_message": user_msg,
                    "assistant_message": reply_msg,
                }
            if ws_cmd in ("/workspace", "/cd"):
                ws_arg = ws_parts[1].strip() if len(ws_parts) > 1 else ""
                reply_emotion = "happy"
                if not ws_arg and ws_cmd == "/workspace":
                    cur = self._build_workspace_payload()
                    if cur["active_path"]:
                        branch_note = f" (branch: {cur['git_branch']})" if cur["git_branch"] else ""
                        reply_text = f"Active workspace: `{cur['active_path']}`{branch_note}."
                    else:
                        reply_text = "No active workspace is set."
                elif ws_arg.lower() == "clear":
                    await self._set_active_workspace("")
                    reply_text = "Workspace cleared."
                elif not ws_arg:
                    reply_emotion = "confused"
                    reply_text = "Usage: `/cd <path>` or `/workspace <path|clear>`."
                else:
                    try:
                        ws_payload = await self._set_active_workspace(ws_arg)
                        branch_note = (
                            f" (branch: {ws_payload['git_branch']})"
                            if ws_payload.get("git_branch")
                            else ""
                        )
                        reply_text = (
                            f"Active workspace set to `{ws_payload['active_path']}`{branch_note}."
                        )
                    except (ValueError, OSError) as exc:
                        reply_emotion = "error"
                        reply_text = f"Workspace error: {exc}"
                reply_msg = await self.storage.add_message(
                    sid,
                    role="assistant",
                    content=reply_text,
                    input_mode=input_mode,
                    emotion=reply_emotion,
                )
                await self.ipc.connection_manager.broadcast(
                    {
                        "type": "transcript_delta",
                        "session_id": sid,
                        "message_id": reply_msg["id"],
                        "role": "assistant",
                        "source": input_mode,
                        "text": reply_text,
                        "emotion": reply_emotion,
                        "is_final": True,
                    }
                )
                return {
                    "session_id": sid,
                    "user_message": user_msg,
                    "assistant_message": reply_msg,
                }

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
            self._speaking_message_id = assistant_id

            cfg = self.config_manager.config
            api_key = self._get_provider_api_key(cfg.provider.active_provider)

            supports_tools, detected_context_length = await self._detect_active_model_capabilities(
                cfg,
                api_key,
            )
            self._detected_tools_support = supports_tools
            self._detected_context_length = detected_context_length

            effective_context_size = cfg.provider.context_size
            if (
                isinstance(effective_context_size, int)
                and not isinstance(effective_context_size, bool)
                and effective_context_size > 0
                and isinstance(self._detected_context_length, int)
                and not isinstance(self._detected_context_length, bool)
                and self._detected_context_length > 0
                and effective_context_size > self._detected_context_length
            ):
                effective_context_size = self._detected_context_length

            tools: list[dict[str, Any]] = []
            if supports_tools and cfg.mcp_servers:
                try:
                    tools = await self.mcp_manager.list_active_tools(cfg.mcp_servers)
                except Exception as exc:
                    logger.warning("Failed listing active MCP tools: %s", exc)
                    tools = []
            active_ws = (cfg.workspace.active_path or "").strip()
            ws_valid = bool(active_ws and Path(active_ws).is_dir())
            god_mode = bool(cfg.agents.god_mode_enabled)
            if supports_tools and cfg.skills.enabled:
                tools.extend(self._build_skill_tools(cfg))
            if supports_tools and ws_valid:
                ws_tools = self._build_workspace_tools()
                if god_mode:
                    _write_ws_tools = {
                        "bytey_workspace__propose_edit",
                        "bytey_workspace__write_file",
                        "bytey_workspace__append_file",
                        "bytey_workspace__delete_file",
                        "bytey_workspace__rename_file",
                    }
                    ws_tools = [
                        t
                        for t in ws_tools
                        if (t.get("function") or {}).get("name") not in _write_ws_tools
                    ]
                tools.extend(ws_tools)
                tools.extend(self._build_git_tools())
            if supports_tools and god_mode:
                if not self.agent_registry.list_agents():
                    await self.agent_registry.detect_all(cfg.agents)
                else:
                    self.agent_registry.sync_config(cfg.agents)
                tools.extend(self._build_agent_tools())

            chunker = SentenceChunker()
            idle_before_s, recent_count = self._record_interaction()
            now_dt = datetime.now()
            data_volume_chars = len(text) + sum(
                len(str(m.get("content") or "")) for m in llm_messages[-6:]
            )
            initial_emotion = "focus" if data_volume_chars >= self.focus_data_threshold_chars else "neutral"
            if initial_emotion != "neutral":
                await self._set_state("THINKING", emotion=initial_emotion)

            mood_hints: list[str] = [
                f"local_time={now_dt.strftime('%H:%M')}",
                f"interactions_last_5m={recent_count}",
                f"idle_minutes_before_turn={int(idle_before_s // 60)}",
                f"input_chars={data_volume_chars}",
            ]
            if recent_count >= self.love_interaction_threshold:
                mood_hints.append("high_cadence_under_5m=true (consider [emotion:love])")
            if idle_before_s >= self.idle_annoyed_after_s:
                if 8 <= now_dt.hour < 20:
                    mood_hints.append("long_idle_daytime=true (consider [emotion:annoyed])")
                else:
                    mood_hints.append("long_idle_nighttime=true (consider [emotion:sleepy])")
            if data_volume_chars >= self.astonished_data_threshold_chars:
                mood_hints.append("overwhelming_data=true (consider [emotion:astonished])")
            elif data_volume_chars >= self.focus_data_threshold_chars:
                mood_hints.append("heavy_information=true (consider [emotion:focus])")

            runtime_context_note = f"\n\n[Runtime Mood Context: {'; '.join(mood_hints)}]"
            if ws_valid:
                ws_branch = detect_git_branch(Path(active_ws))
                branch_suffix = f" (git branch: {ws_branch})" if ws_branch else ""
                runtime_context_note += f"\n[Active Workspace: {active_ws}{branch_suffix}]"
                if supports_tools:
                    runtime_context_note += (
                        "\n[Workspace Tool Rule: When the user asks to create, write, edit, rename, "
                        "delete, inspect, or run files/scripts in the workspace, ALWAYS invoke "
                        "bytey_workspace__write_file, bytey_workspace__append_file, "
                        "bytey_workspace__propose_edit, bytey_workspace__rename_file, "
                        "bytey_workspace__delete_file, bytey_workspace__list_files, "
                        "bytey_workspace__read_file, or bytey_workspace__run_command instead of "
                        "only pasting code in chat. For existing files, ALWAYS call "
                        "bytey_workspace__read_file first and use bytey_workspace__propose_edit "
                        "for partial edits or bytey_workspace__append_file to append sections; "
                        "use bytey_workspace__write_file only for new files or intentional full "
                        "rewrites (overwrite=true). The user will review and approve writes in the UI.]"
                    )
            if god_mode and supports_tools:
                enabled_agents = [
                    a for a in self.agent_registry.list_agents(cfg.agents) if a.installed and a.enabled
                ]
                agents_desc = (
                    ", ".join(
                        f"{a.id} ({a.display_name}, model={a.model or 'default'})"
                        for a in enabled_agents
                    )
                    if enabled_agents
                    else "none enabled"
                )
                runtime_context_note += (
                    "\n[God Mode Active: Act as Prompt Architect & Orchestrator instead of editing "
                    "workspace files directly. Collaborate with the user to craft a clear, "
                    "self-contained prompt (you may use read-only workspace tools to inspect files). "
                    f"Available enabled external CLIs: {agents_desc}. "
                    "When the user asks to execute or send the prompt to a CLI, invoke "
                    "bytey_agent__run(agent, prompt). "
                    "CRITICAL: Inspect the returned stdout and git_status carefully before reporting. "
                    "If the external CLI asked a question, requested approval, or git_status shows no "
                    "expected file changes, NEVER claim files were created or modified—relay the CLI's "
                    "exact question/blocker to the user instead, and use resume=True if the user confirms. "
                    "Keep your spoken summary concise and TTS-friendly (do not read raw logs/diffs aloud); "
                    "put file paths and details in chat text.]"
                )
            if needs_auto_title:
                runtime_context_note += (
                    "\n[Runtime Title Request: This is a new conversation. Begin your reply with "
                    "[title: <2-5 word topic>] alongside your [emotion:<tag>] tag.]"
                )
            effective_system_prompt = (cfg.general.system_prompt or "") + runtime_context_note
            if ws_valid and cfg.workspace.auto_load_rules:
                ws_rules = load_workspace_rules(Path(active_ws))
                if ws_rules:
                    rules_body = "\n\n".join(
                        f"## {rel}\n{content}" for rel, content in ws_rules
                    )
                    effective_system_prompt += f"\n\n<workspace_rules>\n{rules_body}\n</workspace_rules>"
            if cfg.skills.enabled:
                skill_catalog = self.skill_registry.format_prompt_catalog()
                if skill_catalog:
                    effective_system_prompt += f"\n\n{skill_catalog}"
                matched_skill, effective_user_text = self._match_skill_slash_command(text)
                if matched_skill is not None:
                    if llm_messages and llm_messages[-1].get("role") == "user":
                        llm_messages[-1]["content"] = effective_user_text
                    assets_lines: list[str] = []
                    if matched_skill.scripts:
                        assets_lines.append(f"Scripts: {', '.join(matched_skill.scripts)}")
                    if matched_skill.references:
                        assets_lines.append(f"References: {', '.join(matched_skill.references)}")
                    if matched_skill.resources:
                        assets_lines.append(f"Resources: {', '.join(matched_skill.resources)}")
                    assets_note = ("\n" + "\n".join(assets_lines)) if assets_lines else ""
                    effective_system_prompt += (
                        f'\n\n<active_skill name="{matched_skill.name}">\n'
                        f"{matched_skill.body}{assets_note}\n"
                        f"</active_skill>"
                    )

            final_emotion = "neutral"
            accumulated_parts: list[str] = []
            last_result: dict[str, Any] | None = None

            async def on_emotion(tag: str) -> None:
                nonlocal final_emotion
                final_emotion = tag
                target_state = "SPEAKING" if self.state == "SPEAKING" else "THINKING"
                await self._set_state(target_state, emotion=tag)

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

            self._prompt_cancel_event.clear()
            try:
                max_rounds = max(1, int(cfg.provider.max_tool_rounds))
                last_tc_sig: tuple[tuple[str, str], ...] | None = None
                repeat_sig_count = 0
                hit_round_cap = False
                cancelled = False
                for _round in range(max_rounds):
                    if self._prompt_cancel_event.is_set():
                        cancelled = True
                        break
                    if _round > 0:
                        _compact_older_tool_messages(llm_messages)
                    chat_kwargs: dict[str, Any] = {
                        "messages": llm_messages,
                        "system_prompt": effective_system_prompt,
                        "provider": cfg.provider.active_provider,
                        "model": cfg.provider.model,
                        "base_url": cfg.provider.base_url,
                        "api_key": api_key,
                        "temperature": cfg.provider.temperature,
                        "tools": tools or None,
                        "on_text_chunk": on_text_chunk,
                        "on_emotion": on_emotion,
                    }
                    if _accepts_kwarg(self.llm_router.generate_chat, "context_size"):
                        chat_kwargs["context_size"] = effective_context_size
                    result = await self.llm_router.generate_chat(**chat_kwargs)
                    last_result = result
                    if result.get("emotion"):
                        final_emotion = str(result["emotion"])
                    tool_calls = result.get("tool_calls") or []
                    if not tool_calls:
                        break
                    if _round == max_rounds - 1:
                        hit_round_cap = True

                    llm_messages.append(
                        result.get("raw_assistant_message")
                        or {"role": "assistant", "content": result.get("content", "")}
                    )

                    tc_sig = tuple(
                        (
                            str(tc.get("name") or ""),
                            json.dumps(
                                tc.get("arguments") if isinstance(tc.get("arguments"), dict) else {},
                                sort_keys=True,
                                ensure_ascii=False,
                            ),
                        )
                        for tc in tool_calls
                    )
                    if tc_sig == last_tc_sig:
                        repeat_sig_count += 1
                    else:
                        last_tc_sig = tc_sig
                        repeat_sig_count = 1

                    for tc in tool_calls:
                        if self._prompt_cancel_event.is_set():
                            cancelled = True
                            break
                        ns_name = str(tc.get("name") or "")
                        args = tc.get("arguments") if isinstance(tc.get("arguments"), dict) else {}
                        call_id = str(uuid.uuid4())
                        is_skill_tool = ns_name.startswith("bytey_skill__")
                        is_workspace_tool = ns_name.startswith("bytey_workspace__")
                        is_git_tool = ns_name.startswith("bytey_git__")
                        is_agent_tool = ns_name.startswith("bytey_agent__")
                        if is_skill_tool:
                            srv_name = "bytey_skill"
                            tool_name = ns_name[len("bytey_skill__") :]
                        elif is_workspace_tool:
                            srv_name = "bytey_workspace"
                            tool_name = ns_name[len("bytey_workspace__") :]
                        elif is_git_tool:
                            srv_name = "bytey_git"
                            tool_name = ns_name[len("bytey_git__") :]
                        elif is_agent_tool:
                            srv_name = "bytey_agent"
                            tool_name = ns_name[len("bytey_agent__") :]
                        else:
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
                            c_extra: dict[str, Any] | None = None,
                            _cid: str = call_id,
                        ) -> bool:
                            if self._prompt_cancel_event.is_set():
                                return False
                            approved = await self._request_tool_confirmation(
                                _cid, assistant_id, c_srv, c_tool, c_args, c_extra
                            )
                            if not approved or self._prompt_cancel_event.is_set():
                                return False
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
                            return True

                        if is_skill_tool:
                            tool_res = await self._execute_skill_tool(
                                tool_name,
                                args,
                                cfg,
                                confirm_cb,
                            )
                        elif is_workspace_tool:
                            tool_res = await self._execute_workspace_tool(
                                tool_name,
                                args,
                                cfg,
                                confirm_cb,
                            )
                        elif is_git_tool:
                            tool_res = await self._execute_git_tool(
                                tool_name,
                                args,
                                cfg,
                                confirm_cb,
                            )
                        elif is_agent_tool:
                            tool_res = await self._execute_agent_tool(
                                tool_name,
                                args,
                                cfg,
                                confirm_cb,
                                call_id=call_id,
                                assistant_id=assistant_id,
                            )
                        else:
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
                        data_volume_chars += len(summary)
                        if (
                            data_volume_chars >= self.astonished_data_threshold_chars
                            and final_emotion in ("neutral", "curious", "focus")
                        ):
                            final_emotion = "astonished"
                        elif (
                            (data_volume_chars >= self.focus_data_threshold_chars or len(tool_calls) >= 2 or _round >= 1)
                            and final_emotion == "neutral"
                        ):
                            final_emotion = "focus"
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
                                "summary": summary,
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

                    if cancelled:
                        break
                    if repeat_sig_count >= 3:
                        llm_messages.append(
                            {
                                "role": "user",
                                "content": (
                                    "[Loop Guard: You repeated the exact same tool call multiple "
                                    "times. Try a different approach or summarize what you have "
                                    "completed so far without repeating the call.]"
                                ),
                            }
                        )
                    if hit_round_cap:
                        break

                    await self._set_state("THINKING", emotion=final_emotion)

                if cancelled:
                    accumulated_parts.append("\n\n[Stopped by user.]")
                elif hit_round_cap:
                    await self._set_state("THINKING", emotion=final_emotion)
                    _compact_older_tool_messages(llm_messages)
                    llm_messages.append(
                        {
                            "role": "user",
                            "content": (
                                "[System: Maximum tool-call rounds reached for this turn. "
                                "Summarize what you completed so far and what remains next "
                                "without calling any tools.]"
                            ),
                        }
                    )
                    wrapup_kwargs: dict[str, Any] = {
                        "messages": llm_messages,
                        "system_prompt": effective_system_prompt,
                        "provider": cfg.provider.active_provider,
                        "model": cfg.provider.model,
                        "base_url": cfg.provider.base_url,
                        "api_key": api_key,
                        "temperature": cfg.provider.temperature,
                        "tools": None,
                        "on_text_chunk": on_text_chunk,
                        "on_emotion": on_emotion,
                    }
                    if _accepts_kwarg(self.llm_router.generate_chat, "context_size"):
                        wrapup_kwargs["context_size"] = effective_context_size
                    wrapup_res = await self.llm_router.generate_chat(**wrapup_kwargs)
                    last_result = wrapup_res
                    if wrapup_res.get("emotion"):
                        final_emotion = str(wrapup_res["emotion"])

                if final_emotion == "neutral":
                    if data_volume_chars >= self.astonished_data_threshold_chars:
                        final_emotion = "astonished"
                    elif recent_count >= self.love_interaction_threshold:
                        final_emotion = "love"
                    elif data_volume_chars >= self.focus_data_threshold_chars:
                        final_emotion = "focus"
                    else:
                        final_emotion = "happy"

                if self.state == "SPEAKING" and self.emotion != final_emotion:
                    await self._set_state("SPEAKING", emotion=final_emotion)
                else:
                    self.emotion = final_emotion

                tail = chunker.flush()
                if tail:
                    await self.tts_player.enqueue_sentence(tail)

                raw_content = "".join(accumulated_parts).strip()
                _, parsed_title, clean_reply = parse_reply_tags(raw_content)
                clean_reply = clean_reply.strip()
                updated_msg = await self.storage.update_message(
                    assistant_id, content=clean_reply, emotion=final_emotion
                )
                if needs_auto_title:
                    captured_title = (
                        (last_result.get("title") if last_result else None) or parsed_title or ""
                    ).strip()
                    current_session = await self.storage.get_session(sid)
                    if current_session is not None and current_session.get("title") == "New Chat":
                        new_title = captured_title or _auto_title_from_prompt(text)
                        await self.storage.rename_session(sid, new_title)
                        await self._broadcast_sessions_updated()
                await self.ipc.connection_manager.broadcast(
                    {
                        "type": "transcript_delta",
                        "session_id": sid,
                        "message_id": assistant_id,
                        "role": "assistant",
                        "source": input_mode,
                        "text": clean_reply,
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
                    if self._speaking_message_id == assistant_id:
                        self._speaking_message_id = None
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
                    if self._speaking_message_id == assistant_id:
                        self._speaking_message_id = None
                        await self.ipc.connection_manager.broadcast(
                            {"type": "tts_playback", "status": "idle", "message_id": None}
                        )
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
        self.ipc.register_route("PUT", "/api/v1/config", self._route_put_config)
        self.ipc.register_route("PUT", "/api/v1/secrets", self._route_put_secrets)

        self.ipc.register_route("GET", "/api/v1/providers/{provider}/models", self._route_list_models)

        self.ipc.register_route("GET", "/api/v1/mcp/json", self._route_get_mcp_json)
        self.ipc.register_route("GET", "/api/v1/mcp", self._route_get_mcp_json)
        self.ipc.register_route("PUT", "/api/v1/mcp/json", self._route_put_mcp_json)
        self.ipc.register_route("PUT", "/api/v1/mcp", self._route_put_mcp_json)
        self.ipc.register_route("POST", "/api/v1/mcp/test", self._route_test_mcp)
        self.ipc.register_route("GET", "/api/v1/mcp/host", self._route_get_mcp_host)
        self.ipc.register_route("POST", "/api/v1/mcp/confirm", self._route_confirm_tool)
        self.ipc.register_route("POST", "/api/v1/tools/{call_id}/confirm", self._route_confirm_tool)

        self.ipc.register_route("GET", "/api/v1/skills", self._route_list_skills)
        self.ipc.register_route("GET", "/api/v1/workspace", self._route_get_workspace)
        self.ipc.register_route("PUT", "/api/v1/workspace", self._route_put_workspace)
        self.ipc.register_route("GET", "/api/v1/agents", self._route_list_agents)
        self.ipc.register_route("POST", "/api/v1/agents/detect", self._route_detect_agents)
        self.ipc.register_route("POST", "/api/v1/agents/cancel", self._route_cancel_agent)
        self.ipc.register_route("PATCH", "/api/v1/agents/{agent_id}", self._route_patch_agent)
        self.ipc.register_route("POST", "/api/v1/skills/rescan", self._route_rescan_skills)
        self.ipc.register_route("POST", "/api/v1/skills/import", self._route_import_skill)
        self.ipc.register_route("POST", "/api/v1/skills", self._route_create_skill)
        self.ipc.register_route("GET", "/api/v1/skills/{name}", self._route_get_skill)
        self.ipc.register_route("PUT", "/api/v1/skills/{name}", self._route_update_skill)
        self.ipc.register_route("PATCH", "/api/v1/skills/{name}", self._route_patch_skill)
        self.ipc.register_route("DELETE", "/api/v1/skills/{name}", self._route_delete_skill)

        self.ipc.register_route("GET", "/api/v1/tts/voices", self._route_list_voices)
        self.ipc.register_route(
            "POST", "/api/v1/tts/piper/download", self._route_download_piper_model
        )
        self.ipc.register_route("POST", "/api/v1/tts/replay", self._route_replay_tts)
        self.ipc.register_route("POST", "/api/v1/tts/pause", self._route_pause_tts)
        self.ipc.register_route(
            "POST",
            "/api/v1/sessions/{session_id}/messages/{message_id}/replay",
            self._route_replay_tts,
        )

        self.ipc.register_route("GET", "/api/v1/sessions", self._route_list_sessions)
        self.ipc.register_route("GET", "/api/v1/sessions/active", self._route_active_session)
        self.ipc.register_route("GET", "/api/v1/sessions/{session_id}/messages", self._route_session_messages)
        self.ipc.register_route("POST", "/api/v1/sessions", self._route_post_sessions)
        self.ipc.register_route(
            "POST", "/api/v1/sessions/{session_id}/activate", self._route_activate_session
        )
        self.ipc.register_route("PATCH", "/api/v1/sessions/{session_id}", self._route_patch_session)
        self.ipc.register_route("POST", "/api/v1/sessions/{session_id}/messages", self._route_post_message)
        self.ipc.register_route(
            "DELETE",
            "/api/v1/sessions/{session_id}/messages/{message_id}",
            self._route_delete_message,
        )
        self.ipc.register_route("DELETE", "/api/v1/sessions/{session_id}", self._route_delete_session)
        self.ipc.register_route("POST", "/api/v1/chat", self._route_chat)
        self.ipc.register_route("POST", "/api/v1/prompt/cancel", self._route_cancel_prompt)

        self.ipc.register_route("POST", "/api/v1/voice/trigger", self._route_voice_trigger)
        self.ipc.register_route("POST", "/api/v1/voice/barge-in", self._route_voice_barge_in)
        self.ipc.register_route("POST", "/api/v1/visuals/preview", self._route_visuals_preview)
        self.ipc.register_route("POST", "/api/v1/system/exit", self._route_system_exit)

    async def _route_health(self, _req: IPCRequest) -> dict[str, Any]:
        return {"status": "ok", "state": self.state, "emotion": self.emotion}

    async def _route_get_config(self, _req: IPCRequest) -> dict[str, Any]:
        return self._serialize_config()

    async def _finalize_config_change(
        self,
        updated: Any,
        prev_muted: bool,
        *,
        refresh_provider: bool,
        broadcast_visuals: bool,
        refresh_skills: bool = False,
        tools_hint: bool | None,
    ) -> dict[str, Any]:
        if refresh_provider:
            detected_tools, detected_ctx = await self._detect_active_model_capabilities(
                updated,
                self._get_provider_api_key(updated.provider.active_provider),
            )
            self._detected_tools_support = (
                tools_hint if tools_hint is not None else detected_tools
            )
            self._detected_context_length = detected_ctx
        elif tools_hint is not None:
            self._detected_tools_support = tools_hint

        if updated.voice_in.mic_muted != prev_muted:
            await self.voice_input.set_muted(updated.voice_in.mic_muted)

        self.mcp_manager.set_workspace_path(updated.workspace.active_path)

        if refresh_skills:
            self._rescan_skills()
            await self._broadcast_skills_updated()

        serialized = self._serialize_config()
        await self.ipc.connection_manager.broadcast(
            {"type": "config_updated", "config": serialized}
        )
        if broadcast_visuals:
            await self.ipc.connection_manager.broadcast(
                {"type": "position_updated", "visuals": serialized["visuals"]}
            )
        return serialized

    async def _route_patch_config(self, req: IPCRequest) -> dict[str, Any]:
        raw_patch = req.json()
        prev_muted = self.config_manager.config.voice_in.mic_muted
        canonical_patch, tools_hint = self._normalize_patch(raw_patch)
        updated = self.config_manager.update_config(canonical_patch)
        return await self._finalize_config_change(
            updated,
            prev_muted,
            refresh_provider="provider" in canonical_patch,
            broadcast_visuals="visuals" in canonical_patch,
            refresh_skills="skills" in canonical_patch or "workspace" in canonical_patch,
            tools_hint=tools_hint,
        )

    async def _route_put_config(self, req: IPCRequest) -> dict[str, Any]:
        raw_config = req.json()
        prev_muted = self.config_manager.config.voice_in.mic_muted
        canonical_config, tools_hint = self._normalize_patch(raw_config)
        updated = self.config_manager.replace_config(canonical_config)
        return await self._finalize_config_change(
            updated,
            prev_muted,
            refresh_provider=True,
            broadcast_visuals=True,
            refresh_skills=True,
            tools_hint=tools_hint,
        )

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

    async def _route_get_mcp_host(self, _req: IPCRequest) -> dict[str, Any]:
        """Return built-in Bytey MCP Host metadata and active built-in tools."""
        cfg = self.config_manager.config
        raw_tools: list[dict[str, Any]] = []
        raw_tools.extend(self._build_workspace_tools())
        raw_tools.extend(self._build_git_tools())
        if cfg.skills.enabled:
            raw_tools.extend(self._build_skill_tools(cfg))
        if cfg.agents.god_mode_enabled:
            raw_tools.extend(self._build_agent_tools())
        tools = [
            {
                "name": str((t.get("function") or {}).get("name") or t.get("tool") or ""),
                "description": str((t.get("function") or {}).get("description") or ""),
            }
            for t in raw_tools
        ]
        return {
            "ok": True,
            "title": "Bytey MCP Host",
            "description": "Built-in MCP host providing native Workspace, Git, and Skills tools for Bytey.",
            "tools": tools,
        }

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

    async def _route_list_agents(self, _req: IPCRequest) -> dict[str, Any]:
        """Return all supported CLI agents with installation status and per-agent config."""
        cfg = self.config_manager.config
        if not self.agent_registry.list_agents():
            await self.agent_registry.detect_all(cfg.agents)
        else:
            self.agent_registry.sync_config(cfg.agents)
        agents = [a.to_dict() for a in self.agent_registry.list_agents(cfg.agents)]
        return {
            "ok": True,
            "god_mode_enabled": bool(cfg.agents.god_mode_enabled),
            "agents": agents,
        }

    async def _route_detect_agents(self, _req: IPCRequest) -> dict[str, Any]:
        """Re-scan installed external CLI agents and broadcast agents_updated."""
        cfg = self.config_manager.config
        detected = await self.agent_registry.detect_all(cfg.agents)
        agents = [a.to_dict() for a in detected]
        await self.ipc.connection_manager.broadcast(
            {
                "type": "agents_updated",
                "god_mode_enabled": bool(cfg.agents.god_mode_enabled),
                "agents": agents,
            }
        )
        return {
            "ok": True,
            "god_mode_enabled": bool(cfg.agents.god_mode_enabled),
            "agents": agents,
        }

    async def _route_cancel_agent(self, _req: IPCRequest) -> dict[str, Any]:
        """Cancel any active external CLI subprocess."""
        cancelled = await self.agent_runner.cancel()
        return {"ok": True, "cancelled": cancelled}

    async def _route_patch_agent(self, req: IPCRequest) -> dict[str, Any]:
        """Update per-CLI agent override settings (enabled, model, permission_mode, etc.)."""
        agent_id = str(req.path_params.get("agent_id") or "").strip().lower()
        body = req.json()
        cfg = self.config_manager.config
        if not self.agent_registry.list_agents():
            await self.agent_registry.detect_all(cfg.agents)
        info = self.agent_registry.get_agent(agent_id)
        if info is None:
            return IPCResponse(
                status=404,
                body={"ok": False, "error": f"Unknown agent {agent_id!r}"},
            )
        if "enabled" in body and bool(body["enabled"]) and not info.installed:
            return IPCResponse(
                status=400,
                body={"ok": False, "error": f"Cannot enable '{agent_id}': CLI is not installed"},
            )

        current_ov = cfg.agents.overrides.get(agent_id, ExternalAgentOverride()).model_dump()
        for key in ("enabled", "model", "permission_mode", "custom_path", "extra_args", "allow_autonomous_flags"):
            if key in body:
                current_ov[key] = body[key]

        prev_muted = self.config_manager.config.voice_in.mic_muted
        updated_cfg = self.config_manager.update_config(
            {"agents": {"overrides": {agent_id: current_ov}}}
        )
        await self._finalize_config_change(
            updated_cfg,
            prev_muted,
            refresh_provider=False,
            broadcast_visuals=False,
            tools_hint=None,
        )
        agents = [a.to_dict() for a in self.agent_registry.sync_config(updated_cfg.agents)]
        await self.ipc.connection_manager.broadcast(
            {
                "type": "agents_updated",
                "god_mode_enabled": bool(updated_cfg.agents.god_mode_enabled),
                "agents": agents,
            }
        )
        updated_agent = self.agent_registry.get_agent(agent_id)
        return {
            "ok": True,
            "agent": updated_agent.to_dict() if updated_agent else None,
            "agents": agents,
        }

    async def _route_get_workspace(self, _req: IPCRequest) -> dict[str, Any]:
        return self._build_workspace_payload()

    async def _route_put_workspace(self, req: IPCRequest) -> dict[str, Any]:
        body = req.json()
        if not isinstance(body, dict) or not isinstance(body.get("path"), str):
            return IPCResponse(  # type: ignore[return-value]
                status=400,
                body={"error": "Request body must be a JSON object with string 'path'"},
            )
        try:
            return await self._set_active_workspace(body["path"])
        except (ValueError, OSError) as exc:
            return IPCResponse(  # type: ignore[return-value]
                status=400,
                body={"error": str(exc)},
            )

    async def _route_list_skills(self, _req: IPCRequest) -> dict[str, Any]:
        cfg = self.config_manager.config
        skills = [
            self._serialize_skill(s)
            for s in self.skill_registry.list_skills(include_disabled=True)
        ]
        return {
            "skills": skills,
            "skills_dir": str(resolve_skills_dir(cfg.skills.skills_dir)),
            "default_skills_dir": str(resolve_default_skills_dir()),
            "enabled": cfg.skills.enabled,
            "allow_script_execution": cfg.skills.allow_script_execution,
        }

    async def _route_rescan_skills(self, _req: IPCRequest) -> dict[str, Any]:
        cfg = self.config_manager.config
        self._rescan_skills()
        skills = await self._broadcast_skills_updated()
        return {
            "skills": skills,
            "skills_dir": str(resolve_skills_dir(cfg.skills.skills_dir)),
        }

    async def _route_create_skill(self, req: IPCRequest) -> IPCResponse:
        body = req.json()
        if not isinstance(body, dict):
            raise ValueError("Request body must be a JSON object")
        name = str(body.get("name") or "").strip()
        if not name:
            raise ValueError("Skill 'name' is required")
        raw_content = body.get("content") if "content" in body else body.get("raw_content")
        skill = self.skill_registry.create_or_update_skill(
            name,
            content=str(raw_content) if raw_content is not None else None,
            description=str(body.get("description") or ""),
            body=str(body.get("body") or ""),
            version=str(body.get("version") or "1.0.0"),
            slash_command=str(body.get("slash_command") or ""),
            requires_confirmation=bool(body.get("requires_confirmation", False)),
            allowed_tools=(
                [str(t) for t in body["allowed_tools"]]
                if isinstance(body.get("allowed_tools"), list)
                else None
            ),
        )
        await self._broadcast_skills_updated()
        return IPCResponse(
            status=201,
            body={"skill": self._serialize_skill(skill, include_content=True)},
        )

    async def _route_get_skill(self, req: IPCRequest) -> dict[str, Any] | IPCResponse:
        name = req.path_params["name"]
        skill = self.skill_registry.get_skill(name)
        if skill is None:
            return IPCResponse(status=404, body={"error": f"Unknown skill: {name!r}"})
        return self._serialize_skill(skill, include_content=True)

    async def _route_update_skill(self, req: IPCRequest) -> dict[str, Any] | IPCResponse:
        name = req.path_params["name"]
        existing = self.skill_registry.get_skill(name)
        if existing is None:
            return IPCResponse(status=404, body={"error": f"Unknown skill: {name!r}"})
        if not existing.editable:
            return IPCResponse(
                status=403,
                body={"error": f"Skill {name!r} is not editable ({existing.source})"},
            )

        body = req.json()
        if not isinstance(body, dict):
            raise ValueError("Request body must be a JSON object")
        raw_content = body.get("raw_content") if "raw_content" in body else body.get("content")
        skill = self.skill_registry.create_or_update_skill(
            name,
            content=str(raw_content) if raw_content is not None else None,
            description=str(
                body["description"] if "description" in body else existing.description
            ),
            body=str(body["body"] if "body" in body else existing.body),
            version=str(body["version"] if "version" in body else existing.version),
            slash_command=str(
                body["slash_command"]
                if "slash_command" in body
                else existing.slash_command
            ),
            requires_confirmation=bool(
                body["requires_confirmation"]
                if "requires_confirmation" in body
                else existing.requires_confirmation
            ),
            allowed_tools=(
                [str(t) for t in body["allowed_tools"]]
                if isinstance(body.get("allowed_tools"), list)
                else existing.allowed_tools
            ),
        )
        await self._broadcast_skills_updated()
        return {"skill": self._serialize_skill(skill, include_content=True)}

    async def _route_patch_skill(self, req: IPCRequest) -> dict[str, Any] | IPCResponse:
        name = req.path_params["name"]
        existing = self.skill_registry.get_skill(name)
        if existing is None:
            return IPCResponse(status=404, body={"error": f"Unknown skill: {name!r}"})

        body = req.json()
        if not isinstance(body, dict):
            raise ValueError("Request body must be a JSON object")

        overrides = {
            k: v.model_dump(mode="json")
            for k, v in self.config_manager.config.skills.overrides.items()
        }
        entry = dict(overrides.get(name, {}))
        if "enabled" in body:
            entry["enabled"] = bool(body["enabled"])
        if "require_confirmation" in body:
            req_conf = body["require_confirmation"]
            entry["require_confirmation"] = None if req_conf is None else bool(req_conf)
        overrides[name] = SkillOverrideConfig.model_validate(entry).model_dump(mode="json")

        self.config_manager.update_config({"skills": {"overrides": overrides}})
        self._rescan_skills()
        await self._broadcast_skills_updated()
        updated = self.skill_registry.get_skill(name)
        return {"skill": self._serialize_skill(updated) if updated is not None else None}

    async def _route_import_skill(self, req: IPCRequest) -> dict[str, Any] | IPCResponse:
        body = req.json()
        if not isinstance(body, dict):
            raise ValueError("Request body must be a JSON object")
        path = str(body.get("path") or "").strip()
        if not path:
            raise ValueError("Missing 'path' in request body")
        try:
            skill = self.skill_registry.import_skill(path)
        except FileNotFoundError as exc:
            return IPCResponse(status=404, body={"error": str(exc)})
        await self._broadcast_skills_updated()
        return {"skill": self._serialize_skill(skill, include_content=True)}

    async def _route_delete_skill(self, req: IPCRequest) -> dict[str, Any] | IPCResponse:
        name = req.path_params["name"]
        existing = self.skill_registry.get_skill(name)
        if existing is not None and not existing.editable:
            return IPCResponse(
                status=403,
                body={"error": f"Cannot delete {existing.source} skill {name!r}"},
            )
        try:
            self.skill_registry.delete_user_skill(name)
        except FileNotFoundError as exc:
            return IPCResponse(status=404, body={"error": str(exc)})
        skills = await self._broadcast_skills_updated()
        return {"deleted": name, "skills": skills}

    async def _route_list_voices(self, req: IPCRequest) -> dict[str, Any]:
        cfg = self.config_manager.config
        provider = (req.query.get("provider") or cfg.voice_out.tts_provider).strip().lower()
        if provider == "kokoro":
            base_url = req.query.get("base_url") or cfg.voice_out.kokoro_base_url
            api_key = self.secret_store.get_secret("KOKORO_API_KEY")
            voices = await self.kokoro_client.list_voices(base_url=base_url, api_key=api_key)
            return {"voices": voices}
        if provider == "piper":
            refresh_raw = str(req.query.get("refresh") or "").strip().lower()
            refresh = refresh_raw in ("1", "true", "yes")
            language = str(req.query.get("language") or "").strip()
            models_dir = (
                str(req.query.get("models_dir") or "").strip()
                or cfg.voice_out.piper_models_dir
            )
            voices = await self.piper_client.list_voices(
                models_dir=models_dir,
                refresh=refresh,
                language=language,
            )
            return {
                "voices": voices,
                "models_dir": str(self.piper_client.resolve_models_dir(models_dir)),
                "backend": self.piper_client.detect_backend(),
                "presets": PIPER_VOICE_PRESETS,
            }
        if provider == "elevenlabs":
            api_key = self.secret_store.get_secret("ELEVENLABS_API_KEY") or ""
            voices = await self.elevenlabs_client.list_voices(api_key=api_key)
            return {"voices": voices}
        return {"voices": []}

    async def _route_download_piper_model(self, req: IPCRequest) -> dict[str, Any]:
        body = req.json()
        if not isinstance(body, dict):
            raise ValueError("Request body must be a JSON object")
        model_id = str(
            body.get("model") or body.get("model_id") or body.get("id") or ""
        ).strip()
        if not model_id:
            raise ValueError("Piper model ID is required")
        cfg = self.config_manager.config
        models_dir = (
            str(body.get("models_dir") or cfg.voice_out.piper_models_dir or "").strip()
        )
        return await self.piper_client.download_model(
            model_id,
            models_dir=models_dir or None,
        )

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
        self._speaking_message_id = str(msg_id) if msg_id else None
        if replay_emotion and replay_emotion != "neutral":
            await self._set_state(self.state, emotion=replay_emotion)
        await self.tts_player.speak_text(text)
        return {"ok": True}

    async def _route_cancel_prompt(self, _req: IPCRequest) -> dict[str, Any]:
        """Signal any active multi-round prompt loop, pending approval, and CLI run to stop cleanly."""
        self._prompt_cancel_event.set()
        rejected_confirmations = self._reject_pending_confirmations()
        agent_cancelled = await self.agent_runner.cancel()
        return {
            "ok": True,
            "cancelling": self._chat_lock.locked(),
            "rejected_confirmations": rejected_confirmations,
            "agent_cancelled": agent_cancelled,
        }

    async def _route_pause_tts(self, req: IPCRequest) -> dict[str, Any]:
        body = req.json()
        if isinstance(body, dict) and "paused" in body and body["paused"] is not None:
            if bool(body["paused"]):
                await self.tts_player.pause()
            else:
                await self.tts_player.resume()
        elif self.tts_player.is_paused:
            await self.tts_player.resume()
        else:
            await self.tts_player.pause()

        if self.tts_player.is_paused:
            await self.ipc.connection_manager.broadcast(
                {
                    "type": "tts_playback",
                    "status": "paused",
                    "message_id": self._speaking_message_id,
                }
            )
        else:
            status = "playing" if self.tts_player.is_playing else "idle"
            await self.ipc.connection_manager.broadcast(
                {
                    "type": "tts_playback",
                    "status": status,
                    "message_id": self._speaking_message_id,
                }
            )
        return {
            "ok": True,
            "paused": self.tts_player.is_paused,
            "message_id": self._speaking_message_id,
        }

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
        await self._broadcast_sessions_updated()
        return {**session, "session": session}

    async def _route_activate_session(self, req: IPCRequest) -> dict[str, Any]:
        sid = req.path_params["session_id"]
        session = await self.storage.get_session(sid)
        if session is None:
            return IPCResponse(  # type: ignore[return-value]
                status=404,
                body={"error": f"Session not found: {sid}"},
            )
        self._active_session_id = sid
        session_ws = str(session.get("workspace_path") or "").strip()
        if session_ws:
            try:
                resolved_ws = Path(session_ws).expanduser().resolve()
                if (
                    resolved_ws.is_dir()
                    and str(resolved_ws) != self.config_manager.config.workspace.active_path
                ):
                    await self._set_active_workspace(str(resolved_ws), bind_session=False)
            except OSError:
                pass
        messages = await self.storage.list_messages(sid, limit=100)
        await self.ipc.connection_manager.broadcast(
            {
                "type": "session_activated",
                "session_id": sid,
                "messages": messages,
            }
        )
        return {"active_session_id": sid, "messages": messages}

    async def _route_patch_session(self, req: IPCRequest) -> dict[str, Any]:
        sid = req.path_params["session_id"]
        body = req.json()
        raw_title = body.get("title") if isinstance(body, dict) else None
        if not isinstance(raw_title, str) or not raw_title.strip():
            return IPCResponse(  # type: ignore[return-value]
                status=400,
                body={"error": "title must be a non-empty string"},
            )
        updated = await self.storage.rename_session(sid, raw_title)
        if updated is None:
            return IPCResponse(  # type: ignore[return-value]
                status=404,
                body={"error": f"Session not found: {sid}"},
            )
        await self._broadcast_sessions_updated()
        return {"session": updated}

    async def _route_delete_message(self, req: IPCRequest) -> dict[str, Any]:
        raw_sid = req.path_params.get("session_id")
        sid = (
            await self._ensure_active_session()
            if raw_sid in (None, "", "active")
            else str(raw_sid)
        )
        mid = str(req.path_params["message_id"])
        deleted = await self.storage.delete_message(sid, mid)
        if not deleted:
            return IPCResponse(  # type: ignore[return-value]
                status=404,
                body={"error": f"Message not found: {mid}"},
            )
        await self.ipc.connection_manager.broadcast(
            {"type": "message_deleted", "session_id": sid, "message_id": mid}
        )
        await self._broadcast_sessions_updated()
        return {"deleted": mid, "session_id": sid}

    async def _route_delete_session(self, req: IPCRequest) -> dict[str, Any]:
        sid = req.path_params["session_id"]
        await self.storage.delete_session(sid)
        if self._active_session_id == sid:
            self._active_session_id = None
        new_sid = await self._ensure_active_session()
        await self._broadcast_sessions_updated()
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
        self._speaking_message_id = None
        await self.ipc.connection_manager.broadcast(
            {"type": "tts_playback", "status": "idle", "message_id": None}
        )
        await self._set_state("MUTED" if self.config_manager.config.voice_in.mic_muted else "IDLE")
        return {"ok": True}

    async def _route_visuals_preview(self, req: IPCRequest) -> dict[str, Any]:
        body = req.json()
        valid_states = {"IDLE", "LISTENING", "THINKING", "TOOL_CALLING", "SPEAKING", "MUTED", "ERROR"}
        if body.get("reset"):
            target_state = "MUTED" if self.config_manager.config.voice_in.mic_muted else "IDLE"
            self._cancel_emotion_reset()
            await self._set_state(target_state, emotion="neutral")
            return {"ok": True, "state": self.state, "emotion": self.emotion}

        raw_state = body.get("state")
        target_state = str(raw_state).strip().upper() if raw_state is not None else self.state
        if target_state not in valid_states:
            return IPCResponse(status=400, body={"error": f"Invalid state: {target_state}"})  # type: ignore[return-value]

        raw_emotion = body.get("emotion")
        target_emotion = (
            str(raw_emotion).strip().lower() if raw_emotion is not None else self.emotion
        )
        if target_emotion not in VALID_EMOTIONS:
            return IPCResponse(status=400, body={"error": f"Invalid emotion: {target_emotion}"})  # type: ignore[return-value]

        label = str(body["label"])[:60] if body.get("label") is not None else None
        self._cancel_emotion_reset()
        await self._set_state(target_state, emotion=target_emotion, label=label)
        auto_reset_s = body.get("auto_reset_s")
        if isinstance(auto_reset_s, (int, float)) and float(auto_reset_s) > 0:
            self._schedule_emotion_reset(float(auto_reset_s), reset_error_state=True)
        return {"ok": True, "state": self.state, "emotion": self.emotion}

    async def _route_system_exit(self, _req: IPCRequest) -> dict[str, Any]:
        await self.ipc.connection_manager.broadcast({"type": "shutdown"})

        async def _delayed_stop() -> None:
            await asyncio.sleep(0.05)
            await self.stop()

        self._track_task(_delayed_stop())
        return {"ok": True, "status": "shutting_down"}


async def async_main() -> None:
    logging.basicConfig(
        level=(
            os.environ.get("BYTEY_LOG_LEVEL")
            or os.environ.get("AI_PET_LOG_LEVEL")
            or "INFO"
        ).upper(),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    host = os.environ.get("BYTEY_HOST") or os.environ.get("AI_PET_HOST") or "127.0.0.1"
    port = int(os.environ.get("BYTEY_PORT") or os.environ.get("AI_PET_PORT") or "8765")
    daemon = ByteyDaemon(host=host, port=port)

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
    """CLI entrypoint for `python3 -m bytey_ai_companion.main`."""
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
