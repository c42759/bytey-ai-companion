"""Sandboxed subprocess runner for skill scripts."""

from __future__ import annotations

import asyncio
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from .loader import SkillDefinition

MAX_SCRIPT_OUTPUT_BYTES = 32 * 1024  # 32 KiB
SAFE_ENV_KEYS: tuple[str, ...] = ("PATH", "HOME", "LANG", "LC_ALL", "TERM")


@dataclass(slots=True)
class SkillScriptResult:
    """Result of executing a skill helper script."""

    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False
    truncated: bool = False


class SkillScriptRunner:
    """Execute scripts strictly inside a skill's scripts/ directory with minimal env."""

    def __init__(
        self,
        *,
        default_timeout_s: float = 30.0,
        max_output_bytes: int = MAX_SCRIPT_OUTPUT_BYTES,
    ) -> None:
        self.default_timeout_s = default_timeout_s
        self.max_output_bytes = max_output_bytes

    @staticmethod
    def build_sanitized_env() -> dict[str, str]:
        """Return a minimal environment without SecretStore or API key variables."""
        env: dict[str, str] = {}
        for key in SAFE_ENV_KEYS:
            val = os.environ.get(key)
            if val is not None:
                env[key] = val
        env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
        env.setdefault("LANG", "C.UTF-8")
        return env

    @staticmethod
    def resolve_script_path(skill: SkillDefinition, script: str) -> Path:
        """Validate and resolve script inside skill.path/scripts without escaping."""
        cleaned = (script or "").strip()
        if not cleaned:
            raise ValueError("Script path must not be empty")

        rel = Path(cleaned)
        if rel.is_absolute() or ".." in rel.parts:
            raise ValueError(f"Invalid script path: {script!r}")
        if rel.parts and rel.parts[0] == "scripts":
            rel = Path(*rel.parts[1:])
        if not rel.parts:
            raise ValueError(f"Invalid script path: {script!r}")

        scripts_root = (skill.path / "scripts").resolve()
        if not scripts_root.is_dir():
            raise FileNotFoundError(f"Skill {skill.name!r} has no scripts/ directory")

        target = (scripts_root / rel).resolve()
        if not target.is_relative_to(scripts_root):
            raise ValueError(f"Script path escapes scripts/ directory: {script!r}")
        if not target.is_file():
            raise FileNotFoundError(f"Script not found: {script!r}")
        return target

    @staticmethod
    def _build_command(target: Path, args: list[str] | None) -> list[str]:
        extra_args = [str(a) for a in (args or [])]
        suffix = target.suffix.lower()
        if suffix == ".py":
            return [sys.executable, str(target), *extra_args]
        if suffix == ".sh":
            return ["bash", str(target), *extra_args]
        if os.access(target, os.X_OK):
            return [str(target), *extra_args]
        raise PermissionError(
            f"Script {target.name!r} is not executable and not a .py/.sh file"
        )

    def _cap_output(self, data: bytes) -> tuple[str, bool]:
        if len(data) > self.max_output_bytes:
            text = data[: self.max_output_bytes].decode("utf-8", errors="replace")
            return text + "\n[Truncated at 32 KiB]", True
        return data.decode("utf-8", errors="replace"), False

    async def run(
        self,
        skill: SkillDefinition,
        script: str,
        args: list[str] | None = None,
        timeout_s: float | None = None,
    ) -> SkillScriptResult:
        """Run a script inside skill_dir/scripts/ with timeout and output caps."""
        target = self.resolve_script_path(skill, script)
        cmd = self._build_command(target, args)
        effective_timeout = (
            self.default_timeout_s if timeout_s is None else float(timeout_s)
        )

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(skill.path),
            env=self.build_sanitized_env(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        timed_out = False
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(), timeout=effective_timeout
            )
        except asyncio.TimeoutError:
            timed_out = True
            proc.kill()
            stdout_bytes, stderr_bytes = await proc.communicate()

        stdout, out_trunc = self._cap_output(stdout_bytes or b"")
        stderr, err_trunc = self._cap_output(stderr_bytes or b"")

        return SkillScriptResult(
            returncode=-1 if timed_out else (proc.returncode or 0),
            stdout=stdout,
            stderr=stderr,
            timed_out=timed_out,
            truncated=out_trunc or err_trunc,
        )
