"""External AI CLI adapter registry and PATH/version detection."""

from __future__ import annotations

import asyncio
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import AgentsConfig, ExternalAgentOverride


@dataclass(frozen=True)
class CLIAgentSpec:
    """Descriptor for an external AI CLI tool supporting non-interactive invocation."""

    id: str
    display_name: str
    binary: str
    non_interactive_args: tuple[str, ...]
    model_flag: str = "--model"
    default_models: tuple[str, ...] = ()
    accept_edits_args: tuple[str, ...] = ()
    autonomous_args: tuple[str, ...] = ()
    resume_args: tuple[str, ...] = ()
    version_args: tuple[str, ...] = ("--version",)


BUILTIN_CLI_AGENTS: tuple[CLIAgentSpec, ...] = (
    CLIAgentSpec(
        id="claude",
        display_name="Claude Code",
        binary="claude",
        non_interactive_args=("-p", "{prompt}", "--output-format", "text"),
        model_flag="--model",
        default_models=("sonnet", "opus", "haiku"),
        accept_edits_args=("--permission-mode", "acceptEdits"),
        autonomous_args=("--dangerously-skip-permissions",),
        resume_args=("--resume", "{session_id}"),
    ),
    CLIAgentSpec(
        id="copilot",
        display_name="GitHub Copilot CLI",
        binary="copilot",
        non_interactive_args=("-p", "{prompt}"),
        model_flag="--model",
        default_models=("claude-sonnet-4.5", "gpt-5", "o4-mini"),
        accept_edits_args=(),
        autonomous_args=("--allow-all-tools",),
        resume_args=("--resume", "{session_id}"),
    ),
    CLIAgentSpec(
        id="agy",
        display_name="Antigravity CLI",
        binary="agy",
        non_interactive_args=("-p", "{prompt}", "--output-format", "text"),
        model_flag="--model",
        default_models=(),
        accept_edits_args=("--mode", "accept-edits"),
        autonomous_args=("--mode", "accept-edits", "--dangerously-skip-permissions"),
        resume_args=("--conversation", "{session_id}"),
    ),
    CLIAgentSpec(
        id="gemini",
        display_name="Gemini CLI",
        binary="gemini",
        non_interactive_args=("-p", "{prompt}"),
        model_flag="-m",
        default_models=("gemini-2.5-pro", "gemini-2.5-flash"),
    ),
    CLIAgentSpec(
        id="codex",
        display_name="OpenAI Codex CLI",
        binary="codex",
        non_interactive_args=("exec", "{prompt}"),
        model_flag="-m",
        default_models=("gpt-5", "o4-mini", "o3"),
    ),
    CLIAgentSpec(
        id="opencode",
        display_name="OpenCode",
        binary="opencode",
        non_interactive_args=("run", "{prompt}"),
        model_flag="-m",
        default_models=(),
    ),
    CLIAgentSpec(
        id="aider",
        display_name="Aider",
        binary="aider",
        non_interactive_args=("--message", "{prompt}", "--no-auto-commits"),
        model_flag="--model",
        default_models=(),
        accept_edits_args=("--yes-always",),
        autonomous_args=("--yes-always",),
    ),
)


@dataclass
class AgentInfo:
    """Runtime detection and configuration state for a single CLI agent."""

    id: str
    display_name: str
    binary: str
    installed: bool
    resolved_path: str
    version: str
    enabled: bool
    model: str
    model_flag: str
    default_models: list[str] = field(default_factory=list)
    permission_mode: str = "accept_edits"
    custom_path: str = ""
    extra_args: list[str] = field(default_factory=list)
    allow_autonomous_flags: bool = False
    is_custom: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Serialize agent info for REST/WS payloads."""
        return {
            "id": self.id,
            "display_name": self.display_name,
            "binary": self.binary,
            "installed": self.installed,
            "resolved_path": self.resolved_path,
            "version": self.version,
            "enabled": self.enabled,
            "model": self.model,
            "model_flag": self.model_flag,
            "default_models": list(self.default_models),
            "permission_mode": self.permission_mode,
            "custom_path": self.custom_path,
            "extra_args": list(self.extra_args),
            "allow_autonomous_flags": self.allow_autonomous_flags,
            "is_custom": self.is_custom,
        }


def _augmented_search_path() -> str:
    """Build PATH augmented with common user CLI install directories."""
    existing = os.environ.get("PATH", "")
    parts = [p for p in existing.split(os.pathsep) if p]
    home = Path.home()
    candidates = [
        str(home / ".local" / "bin"),
        str(home / ".npm-global" / "bin"),
        str(home / ".local" / "share" / "pnpm"),
        "/usr/local/bin",
    ]
    for cand in candidates:
        if cand not in parts:
            parts.append(cand)
    return os.pathsep.join(parts)


def _resolve_binary(binary: str, custom_path: str = "") -> str:
    """Resolve executable path from custom_path or augmented PATH."""
    if custom_path:
        expanded = str(Path(custom_path).expanduser())
        if os.path.isfile(expanded) and os.access(expanded, os.X_OK):
            return expanded
        found_custom = shutil.which(expanded, path=_augmented_search_path())
        if found_custom:
            return found_custom
        return ""
    if not binary:
        return ""
    return shutil.which(binary, path=_augmented_search_path()) or ""


async def _terminate_and_reap(proc: asyncio.subprocess.Process | None) -> None:
    """Terminate/kill and reap a subprocess if still running."""
    if proc is None or proc.returncode is not None:
        return
    try:
        proc.kill()
    except ProcessLookupError:
        pass
    except Exception:
        pass
    try:
        await proc.wait()
    except Exception:
        pass


async def _probe_version(binary_path: str, version_args: tuple[str, ...]) -> str:
    """Run a quick non-blocking version probe with 2s timeout."""
    if not binary_path:
        return ""
    proc: asyncio.subprocess.Process | None = None
    try:
        proc = await asyncio.create_subprocess_exec(
            binary_path,
            *version_args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=2.0)
        raw = (stdout or stderr or b"").decode("utf-8", errors="replace").strip()
        if not raw:
            return ""
        first_line = raw.splitlines()[0].strip()
        return first_line[:120]
    except asyncio.TimeoutError:
        await _terminate_and_reap(proc)
        return ""
    except asyncio.CancelledError:
        await _terminate_and_reap(proc)
        raise
    except Exception:
        await _terminate_and_reap(proc)
        return ""


class AgentRegistry:
    """Maintains built-in + custom CLI specs and caches detection results."""

    def __init__(self) -> None:
        self._detected: dict[str, AgentInfo] = {}
        self._specs: dict[str, CLIAgentSpec] = {spec.id: spec for spec in BUILTIN_CLI_AGENTS}

    def _all_specs(self, cfg: AgentsConfig) -> list[tuple[CLIAgentSpec, bool]]:
        """Return (spec, is_custom) pairs for built-in and custom agents."""
        items: list[tuple[CLIAgentSpec, bool]] = [(spec, False) for spec in BUILTIN_CLI_AGENTS]
        seen = {spec.id for spec in BUILTIN_CLI_AGENTS}
        for custom in cfg.custom_agents:
            cid = custom.id.strip().lower()
            if not cid or cid in seen:
                continue
            seen.add(cid)
            items.append(
                (
                    CLIAgentSpec(
                        id=cid,
                        display_name=custom.display_name.strip() or cid,
                        binary=custom.command.strip(),
                        non_interactive_args=tuple(custom.non_interactive_args or ["-p", "{prompt}"]),
                        model_flag=custom.model_flag.strip() or "--model",
                    ),
                    True,
                )
            )
        return items

    async def detect_all(self, cfg: AgentsConfig) -> list[AgentInfo]:
        """Detect all supported + custom CLI tools and refresh cached metadata."""
        specs = self._all_specs(cfg)
        self._specs = {spec.id: spec for spec, _ in specs}

        async def _inspect(spec: CLIAgentSpec, is_custom: bool) -> AgentInfo:
            ov = cfg.overrides.get(spec.id, ExternalAgentOverride())
            resolved = _resolve_binary(spec.binary, ov.custom_path)
            installed = bool(resolved)
            version = await _probe_version(resolved, spec.version_args) if installed else ""
            enabled = bool(ov.enabled) if installed else False
            return AgentInfo(
                id=spec.id,
                display_name=spec.display_name,
                binary=spec.binary,
                installed=installed,
                resolved_path=resolved,
                version=version,
                enabled=enabled,
                model=ov.model.strip(),
                model_flag=spec.model_flag,
                default_models=list(spec.default_models),
                permission_mode=ov.permission_mode,
                custom_path=ov.custom_path.strip(),
                extra_args=list(ov.extra_args),
                allow_autonomous_flags=bool(ov.allow_autonomous_flags),
                is_custom=is_custom,
            )

        results = await asyncio.gather(*(_inspect(spec, is_c) for spec, is_c in specs))
        self._detected = {info.id: info for info in results}
        return list(results)

    def sync_config(self, cfg: AgentsConfig) -> list[AgentInfo]:
        """Re-apply config overrides onto cached detection without re-probing --version."""
        if not self._detected:
            return []
        updated: dict[str, AgentInfo] = {}
        for spec, is_custom in self._all_specs(cfg):
            self._specs[spec.id] = spec
            prev = self._detected.get(spec.id)
            ov = cfg.overrides.get(spec.id, ExternalAgentOverride())
            new_custom_path = ov.custom_path.strip()
            if not prev or new_custom_path != prev.custom_path or spec.binary != prev.binary:
                resolved = _resolve_binary(spec.binary, new_custom_path)
            else:
                resolved = prev.resolved_path
            installed = bool(resolved)
            version = prev.version if prev and installed and resolved == prev.resolved_path else ""
            updated[spec.id] = AgentInfo(
                id=spec.id,
                display_name=spec.display_name,
                binary=spec.binary,
                installed=installed,
                resolved_path=resolved,
                version=version,
                enabled=bool(ov.enabled) if installed else False,
                model=ov.model.strip(),
                model_flag=spec.model_flag,
                default_models=list(spec.default_models),
                permission_mode=ov.permission_mode,
                custom_path=ov.custom_path.strip(),
                extra_args=list(ov.extra_args),
                allow_autonomous_flags=bool(ov.allow_autonomous_flags),
                is_custom=is_custom,
            )
        self._detected = updated
        return list(updated.values())

    def list_agents(self, cfg: AgentsConfig | None = None) -> list[AgentInfo]:
        """Return cached AgentInfo list, optionally syncing latest config."""
        if cfg is not None and self._detected:
            return self.sync_config(cfg)
        return list(self._detected.values())

    def get_agent(self, agent_id: str) -> AgentInfo | None:
        """Return cached AgentInfo for agent_id."""
        return self._detected.get(agent_id.strip().lower())

    def get_spec(self, agent_id: str) -> CLIAgentSpec | None:
        """Return CLIAgentSpec for agent_id."""
        return self._specs.get(agent_id.strip().lower())

    def build_argv(
        self,
        agent_id: str,
        prompt: str,
        cfg: AgentsConfig,
        *,
        model_override: str = "",
        resume_session_id: str = "",
    ) -> list[str]:
        """Construct safe subprocess argv for non-interactive CLI execution."""
        key = agent_id.strip().lower()
        info = self.get_agent(key)
        spec = self.get_spec(key)
        if info is None or spec is None or not info.installed or not info.resolved_path:
            raise ValueError(f"Agent '{agent_id}' is not installed")
        if not info.enabled:
            raise ValueError(f"Agent '{agent_id}' is disabled")

        ov = cfg.overrides.get(key, ExternalAgentOverride())
        argv: list[str] = [info.resolved_path]

        if resume_session_id and spec.resume_args:
            for arg in spec.resume_args:
                argv.append(arg.replace("{session_id}", resume_session_id))

        for arg in spec.non_interactive_args:
            argv.append(arg.replace("{prompt}", prompt))

        effective_model = (model_override or ov.model or "").strip()
        if effective_model and spec.model_flag:
            argv.extend([spec.model_flag, effective_model])

        if ov.permission_mode == "autonomous":
            if not ov.allow_autonomous_flags:
                raise ValueError(
                    f"Agent '{agent_id}' permission_mode='autonomous' requires allow_autonomous_flags=True"
                )
            argv.extend(spec.autonomous_args)
        elif ov.permission_mode == "accept_edits":
            argv.extend(spec.accept_edits_args)

        if ov.extra_args:
            argv.extend(ov.extra_args)

        return argv
