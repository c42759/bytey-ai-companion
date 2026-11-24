"""Non-interactive subprocess runner for external AI CLI agents."""

from __future__ import annotations

import asyncio
import os
import re
import signal
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

_SESSION_ID_RE = re.compile(r'"session_id"\s*:\s*"([^"]+)"')
_AUTH_ERROR_MARKERS = (
    "not logged in",
    "please log in",
    "please login",
    "authentication required",
    "unauthorized",
    "invalid api key",
    "missing api key",
    "run `claude login`",
    "copilot auth",
)


@dataclass
class AgentRunResult:
    """Outcome of a non-interactive external CLI agent invocation."""

    agent: str
    exit_code: int
    duration_ms: int
    stdout: str
    stderr: str
    timed_out: bool = False
    cancelled: bool = False
    session_id: str = ""
    git_status: str = ""
    auth_hint: str = ""


def _detect_auth_hint(agent_id: str, stderr_text: str, stdout_text: str) -> str:
    """Return a clear login hint if CLI output indicates missing authentication."""
    combined = f"{stderr_text}\n{stdout_text}".lower()
    if any(marker in combined for marker in _AUTH_ERROR_MARKERS):
        return f"External CLI '{agent_id}' appears unauthenticated. Run '{agent_id} login' in a terminal."
    return ""


async def _collect_git_status(cwd: Path) -> str:
    """Return short git status if cwd is inside a git repository."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            "status",
            "--short",
            cwd=str(cwd),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=3.0)
        if proc.returncode == 0:
            return stdout.decode("utf-8", errors="replace").strip()[:4000]
    except Exception:
        pass
    return ""


class AgentRunner:
    """Runs external AI CLI commands non-interactively with streaming and cancellation."""

    def __init__(self) -> None:
        self._active_proc: asyncio.subprocess.Process | None = None
        self._cancelled: bool = False

    @property
    def is_running(self) -> bool:
        """Whether an external CLI subprocess is currently active."""
        return self._active_proc is not None and self._active_proc.returncode is None

    async def cancel(self) -> bool:
        """Terminate the active CLI process group cleanly."""
        proc = self._active_proc
        if proc is None or proc.returncode is not None:
            return False
        self._cancelled = True
        try:
            pgid = os.getpgid(proc.pid)
            os.killpg(pgid, signal.SIGTERM)
        except ProcessLookupError:
            return False
        except Exception:
            try:
                proc.terminate()
            except Exception:
                pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=2.0)
        except asyncio.TimeoutError:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
            try:
                await proc.wait()
            except Exception:
                pass
        return True

    async def run(
        self,
        agent_id: str,
        argv: list[str],
        cwd: Path,
        *,
        timeout_seconds: float = 300,
        max_output_chars: int = 32768,
        on_output_tail: Callable[[str], Awaitable[None]] | None = None,
    ) -> AgentRunResult:
        """Execute argv in cwd with inactivity timeout, output capture, and process-group cleanup."""
        started = time.monotonic()
        last_activity = started
        inactivity_limit = max(0.2, float(timeout_seconds))
        self._cancelled = False
        timed_out = False

        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(cwd),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        self._active_proc = proc

        stdout_chunks: list[str] = []
        stderr_chunks: list[str] = []
        last_emit = 0.0
        capture_limit = max(0, int(max_output_chars))

        async def _read_stream(
            stream: asyncio.StreamReader | None,
            sink: list[str],
            stream_name: str,
        ) -> None:
            nonlocal last_emit, last_activity
            if stream is None:
                return
            captured = 0
            truncated = False
            rolling_tail = ""
            while True:
                chunk = await stream.read(4096)
                if not chunk:
                    break
                last_activity = time.monotonic()
                text = chunk.decode("utf-8", errors="replace")
                if not truncated:
                    remaining = capture_limit - captured
                    if remaining > 0:
                        piece = text[:remaining]
                        sink.append(piece)
                        captured += len(piece)
                    if len(text) > remaining:
                        sink.append(f"\n... [{stream_name} truncated]")
                        truncated = True
                if on_output_tail is not None:
                    rolling_tail = (rolling_tail + text)[-300:]
                    now = time.monotonic()
                    if now - last_emit >= 0.6:
                        last_emit = now
                        tail = rolling_tail.strip()
                        if tail:
                            try:
                                await on_output_tail(tail)
                            except Exception:
                                pass

        async def _inactivity_watchdog() -> None:
            nonlocal timed_out
            poll_interval = min(1.0, max(0.05, inactivity_limit / 4.0))
            while proc.returncode is None:
                await asyncio.sleep(poll_interval)
                if proc.returncode is not None:
                    return
                if (time.monotonic() - last_activity) >= inactivity_limit:
                    timed_out = True
                    await self.cancel()
                    return

        watchdog_task = asyncio.create_task(_inactivity_watchdog())
        try:
            await asyncio.gather(
                _read_stream(proc.stdout, stdout_chunks, "stdout"),
                _read_stream(proc.stderr, stderr_chunks, "stderr"),
                proc.wait(),
            )
        except asyncio.CancelledError:
            await self.cancel()
            raise
        finally:
            if not watchdog_task.done():
                watchdog_task.cancel()
                try:
                    await watchdog_task
                except asyncio.CancelledError:
                    pass
            self._active_proc = None

        duration_ms = int((time.monotonic() - started) * 1000)
        stdout_text = "".join(stdout_chunks)
        stderr_text = "".join(stderr_chunks)

        session_match = _SESSION_ID_RE.search(stdout_text)
        session_id = session_match.group(1) if session_match else ""
        auth_hint = _detect_auth_hint(agent_id, stderr_text, stdout_text)
        git_status = await _collect_git_status(cwd)
        exit_code = proc.returncode if proc.returncode is not None else -1

        return AgentRunResult(
            agent=agent_id,
            exit_code=exit_code,
            duration_ms=duration_ms,
            stdout=stdout_text.strip(),
            stderr=stderr_text.strip(),
            timed_out=timed_out,
            cancelled=self._cancelled,
            session_id=session_id,
            git_status=git_status,
            auth_hint=auth_hint,
        )
