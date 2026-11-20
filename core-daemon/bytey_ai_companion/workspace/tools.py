"""Sandboxed workspace file, search, edit, write, and command execution tools."""

from __future__ import annotations

import asyncio
import difflib
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

IGNORED_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        "node_modules",
        "__pycache__",
        ".venv",
        ".mypy_cache",
        ".pytest_cache",
    }
)
MAX_LIST_ENTRIES = 300
MAX_GREP_MATCHES = 100
MAX_READ_LINES = 200
MAX_READ_BYTES = 32 * 1024
MAX_CMD_BYTES = 32 * 1024

_RULE_CANDIDATES: tuple[str, ...] = (
    "AGENTS.md",
    ".agents/GEMINI.md",
    ".agents/CLAUDE.md",
    "CLAUDE.md",
    "agents/CLAUDE.md",
)


def detect_git_branch(root: Path) -> str | None:
    """Detect the current git branch for a workspace directory, or return None."""
    try:
        root_resolved = Path(root).expanduser().resolve()
    except OSError:
        return None
    if not root_resolved.is_dir():
        return None

    head_file = root_resolved / ".git" / "HEAD"
    if head_file.is_file():
        try:
            content = head_file.read_text(encoding="utf-8", errors="replace").strip()
            if content.startswith("ref: "):
                ref = content[5:].strip()
                prefix = "refs/heads/"
                return ref[len(prefix) :] if ref.startswith(prefix) else ref
            if content:
                return content[:12]
        except OSError:
            pass

    try:
        proc = subprocess.run(
            ["git", "-C", str(root_resolved), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            timeout=2.0,
            check=False,
        )
        if proc.returncode == 0:
            branch = proc.stdout.strip()
            return branch or None
    except Exception:
        pass
    return None


def load_workspace_rules(root: Path, max_chars: int = 6000) -> list[tuple[str, str]]:
    """Load workspace instruction/rule files up to a total character budget."""
    try:
        root_resolved = Path(root).expanduser().resolve()
    except OSError:
        return []
    if not root_resolved.is_dir() or max_chars <= 0:
        return []

    results: list[tuple[str, str]] = []
    remaining = max_chars
    for rel_path in _RULE_CANDIDATES:
        if remaining <= 0:
            break
        candidate = root_resolved / rel_path
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if not resolved.is_relative_to(root_resolved) or not resolved.is_file():
            continue
        try:
            text = resolved.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            continue
        if not text:
            continue
        if len(text) > remaining:
            suffix = "\n...[truncated]"
            cut = max(0, remaining - len(suffix))
            text = text[:cut] + suffix
            remaining = 0
        else:
            remaining -= len(text)
        results.append((rel_path, text))
    return results


def _atomic_write_text(target: Path, content: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=".bytey.", suffix=".tmp", dir=str(target.parent)
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.replace(tmp_path, target)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def _unified_diff(
    original: str, updated: str, rel_path: str
) -> str:
    return "".join(
        difflib.unified_diff(
            original.splitlines(keepends=True),
            updated.splitlines(keepends=True),
            fromfile=f"a/{rel_path}",
            tofile=f"b/{rel_path}",
        )
    )


class WorkspaceToolchain:
    """Path-contained workspace tools for listing, searching, reading, editing, writing, and running commands."""

    def __init__(self, root_path: str | Path) -> None:
        resolved = Path(root_path).expanduser().resolve()
        if not resolved.is_dir():
            raise ValueError(f"Workspace root is not an existing directory: {root_path!r}")
        self.root: Path = resolved

    def _resolve_in_workspace(self, rel_path: str) -> Path:
        """Resolve rel_path strictly inside self.root, rejecting traversal and escaping symlinks."""
        if not isinstance(rel_path, str):
            raise ValueError(f"Invalid relative path: {rel_path!r}")
        cleaned = rel_path.strip() or "."
        candidate = Path(cleaned)
        if ".." in candidate.parts:
            raise ValueError(f"Path traversal ('..') is not allowed: {rel_path!r}")
        if candidate.is_absolute():
            resolved = candidate.resolve()
        else:
            resolved = (self.root / candidate).resolve()
        if not resolved.is_relative_to(self.root):
            raise ValueError(f"Path escapes workspace root: {rel_path!r}")
        return resolved

    def list_files(self, rel_path: str = ".", max_depth: int = 3) -> list[str]:
        """List files under rel_path inside the workspace, skipping ignored directories."""
        target = self._resolve_in_workspace(rel_path)
        if not target.is_dir():
            raise NotADirectoryError(f"Not a directory: {rel_path!r}")

        results: list[str] = []
        base_depth = len(target.parts)
        for current_root, dirs, files in os.walk(target, followlinks=False):
            current_path = Path(current_root)
            depth = len(current_path.parts) - base_depth
            dirs[:] = sorted(
                d
                for d in dirs
                if d not in IGNORED_DIRS
                and not (current_path / d).is_symlink()
                and depth < max_depth
            )
            for fname in sorted(files):
                fpath = current_path / fname
                try:
                    resolved_file = fpath.resolve()
                except OSError:
                    continue
                if not resolved_file.is_relative_to(self.root) or not resolved_file.is_file():
                    continue
                results.append(resolved_file.relative_to(self.root).as_posix())
                if len(results) >= MAX_LIST_ENTRIES:
                    return results
        return results

    def grep_search(self, pattern: str, rel_path: str = ".") -> list[dict[str, Any]]:
        """Search workspace files for regex pattern, returning up to 100 matches."""
        if not pattern:
            raise ValueError("Search pattern must not be empty")
        target = self._resolve_in_workspace(rel_path)
        if not target.exists():
            raise FileNotFoundError(f"Path not found: {rel_path!r}")

        rg_bin = shutil.which("rg")
        if rg_bin:
            return self._grep_with_rg(rg_bin, pattern, target)
        return self._grep_python(pattern, target)

    def _grep_with_rg(
        self, rg_bin: str, pattern: str, target: Path
    ) -> list[dict[str, Any]]:
        cmd = [rg_bin, "--json", "--line-number", "--no-heading"]
        for ignored in sorted(IGNORED_DIRS):
            cmd.extend(["--glob", f"!{ignored}/**", "--glob", f"!{ignored}"])
        cmd.extend(["--", pattern, str(target)])
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=15.0,
            check=False,
        )
        matches: list[dict[str, Any]] = []
        for line in proc.stdout.splitlines():
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") != "match":
                continue
            data = event.get("data") or {}
            raw_path = ((data.get("path") or {}).get("text")) or ""
            try:
                resolved = Path(raw_path).resolve()
                if not resolved.is_relative_to(self.root):
                    continue
                rel = resolved.relative_to(self.root).as_posix()
            except (OSError, ValueError):
                continue
            line_num = int(data.get("line_number") or 0)
            text = (((data.get("lines") or {}).get("text")) or "").rstrip("\r\n")
            matches.append({"path": rel, "line": line_num, "text": text})
            if len(matches) >= MAX_GREP_MATCHES:
                break
        return matches

    def _grep_python(self, pattern: str, target: Path) -> list[dict[str, Any]]:
        compiled = re.compile(pattern)
        matches: list[dict[str, Any]] = []

        def _scan_file(fpath: Path) -> bool:
            try:
                resolved = fpath.resolve()
                if not resolved.is_relative_to(self.root) or not resolved.is_file():
                    return False
                rel = resolved.relative_to(self.root).as_posix()
                with resolved.open("r", encoding="utf-8", errors="replace") as handle:
                    for idx, line in enumerate(handle, start=1):
                        if compiled.search(line):
                            matches.append(
                                {
                                    "path": rel,
                                    "line": idx,
                                    "text": line.rstrip("\r\n"),
                                }
                            )
                            if len(matches) >= MAX_GREP_MATCHES:
                                return True
            except OSError:
                return False
            return False

        if target.is_file():
            _scan_file(target)
            return matches

        for current_root, dirs, files in os.walk(target, followlinks=False):
            current_path = Path(current_root)
            dirs[:] = sorted(
                d
                for d in dirs
                if d not in IGNORED_DIRS and not (current_path / d).is_symlink()
            )
            for fname in sorted(files):
                if _scan_file(current_path / fname):
                    return matches
        return matches

    def read_file(
        self, rel_path: str, start_line: int = 1, end_line: int = 200
    ) -> str:
        """Read numbered lines from a workspace file, capped at 200 lines and 32 KiB."""
        target = self._resolve_in_workspace(rel_path)
        if not target.is_file():
            raise FileNotFoundError(f"File not found: {rel_path!r}")

        start = max(1, int(start_line))
        end = max(start, min(int(end_line), start + MAX_READ_LINES - 1))

        out_lines: list[str] = []
        total_bytes = 0
        truncated = False
        with target.open("r", encoding="utf-8", errors="replace") as handle:
            for lineno, line in enumerate(handle, start=1):
                if lineno < start:
                    continue
                if lineno > end:
                    break
                formatted = f"{lineno}: {line.rstrip(chr(10))}"
                encoded_len = len(formatted.encode("utf-8")) + 1
                if total_bytes + encoded_len > MAX_READ_BYTES:
                    truncated = True
                    break
                out_lines.append(formatted)
                total_bytes += encoded_len

        result = "\n".join(out_lines)
        if truncated:
            result += "\n[Truncated at 32 KiB]"
        return result

    def preview_edit(
        self, rel_path: str, target: str, replacement: str
    ) -> dict[str, Any]:
        """Verify exact target match and compute unified diff without modifying disk."""
        if not target:
            return {
                "ok": False,
                "error": "Target text must not be empty",
                "diff": "",
                "rel_path": rel_path,
                "target": target,
                "replacement": replacement,
            }
        path = self._resolve_in_workspace(rel_path)
        if not path.is_file():
            return {
                "ok": False,
                "error": f"File not found: {rel_path}",
                "diff": "",
                "rel_path": rel_path,
                "target": target,
                "replacement": replacement,
            }
        original = path.read_text(encoding="utf-8")
        if target not in original:
            return {
                "ok": False,
                "error": "Target text not found in file",
                "diff": "",
                "rel_path": rel_path,
                "target": target,
                "replacement": replacement,
            }
        updated = original.replace(target, replacement, 1)
        diff = _unified_diff(original, updated, rel_path)
        return {
            "ok": True,
            "diff": diff,
            "rel_path": rel_path,
            "target": target,
            "replacement": replacement,
        }

    def apply_edit(
        self, rel_path: str, target: str, replacement: str
    ) -> dict[str, Any]:
        """Verify exact target match and atomically write the replacement to disk."""
        preview = self.preview_edit(rel_path, target, replacement)
        if not preview["ok"]:
            return {"ok": False, "error": preview["error"], "diff": "", "rel_path": rel_path}
        path = self._resolve_in_workspace(rel_path)
        original = path.read_text(encoding="utf-8")
        updated = original.replace(target, replacement, 1)
        _atomic_write_text(path, updated)
        return {"ok": True, "diff": preview["diff"], "rel_path": rel_path}

    def preview_write(self, rel_path: str, content: str) -> dict[str, Any]:
        """Compute unified diff for writing content to rel_path without modifying disk."""
        path = self._resolve_in_workspace(rel_path)
        is_new = not path.exists()
        original = "" if is_new else path.read_text(encoding="utf-8")
        diff = _unified_diff(original, content, rel_path)
        return {"ok": True, "diff": diff, "rel_path": rel_path, "is_new": is_new}

    def apply_write(self, rel_path: str, content: str) -> dict[str, Any]:
        """Create parent directories if needed and atomically write content to rel_path."""
        path = self._resolve_in_workspace(rel_path)
        is_new = not path.exists()
        original = "" if is_new else path.read_text(encoding="utf-8")
        _atomic_write_text(path, content)
        diff = _unified_diff(original, content, rel_path)
        return {"ok": True, "diff": diff, "rel_path": rel_path, "is_new": is_new}

    async def run_command(
        self, command: str, timeout_s: float = 30.0
    ) -> dict[str, Any]:
        """Execute a shell command inside the workspace root with timeout and output caps."""
        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=str(self.root),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(), timeout=timeout_s
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return {
                "ok": False,
                "returncode": -1,
                "stdout": "",
                "stderr": f"Command timed out after {timeout_s}s",
                "timed_out": True,
            }

        def _cap(raw: bytes) -> str:
            if len(raw) > MAX_CMD_BYTES:
                return raw[:MAX_CMD_BYTES].decode("utf-8", errors="replace") + "\n[Truncated at 32 KiB]"
            return raw.decode("utf-8", errors="replace")

        return {
            "ok": proc.returncode == 0,
            "returncode": proc.returncode,
            "stdout": _cap(stdout_b),
            "stderr": _cap(stderr_b),
            "timed_out": False,
        }
