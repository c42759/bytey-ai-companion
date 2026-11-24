"""Sandboxed workspace Git toolchain using safe subprocess exec (no shell)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

MAX_GIT_BYTES = 32 * 1024
LOCAL_TIMEOUT_S = 30.0
REMOTE_TIMEOUT_S = 60.0

READ_ONLY_GIT_TOOLS: frozenset[str] = frozenset(
    {
        "status",
        "diff",
        "log",
        "show",
        "blame",
        "branch_list",
        "tag_list",
        "stash_list",
        "remote_list",
    }
)

ALWAYS_CONFIRM_GIT_TOOLS: frozenset[str] = frozenset(
    {
        "branch_delete",
        "merge",
        "rebase",
        "cherry_pick",
        "restore",
        "reset",
        "fetch",
        "pull",
        "push",
    }
)


def _cap_output(raw: bytes, truncated: bool = False) -> str:
    if truncated or len(raw) > MAX_GIT_BYTES:
        return raw[:MAX_GIT_BYTES].decode("utf-8", errors="replace") + "\n[Truncated at 32 KiB]"
    return raw.decode("utf-8", errors="replace")


async def _read_bounded_stream(
    stream: asyncio.StreamReader | None, max_bytes: int
) -> tuple[bytes, bool]:
    """Drain stream to EOF while keeping at most max_bytes in memory."""
    if stream is None:
        return b"", False
    buf = bytearray()
    truncated = False
    while True:
        chunk = await stream.read(4096)
        if not chunk:
            break
        if not truncated:
            remaining = max_bytes - len(buf)
            if remaining > 0:
                buf.extend(chunk[:remaining])
            if len(chunk) > remaining:
                truncated = True
    return bytes(buf), truncated


class GitToolchain:
    """Path-contained async Git operations scoped to an active workspace root."""

    def __init__(self, root_path: str | Path) -> None:
        resolved = Path(root_path).expanduser().resolve()
        if not resolved.is_dir():
            raise ValueError(f"Workspace root is not an existing directory: {root_path!r}")
        self.root: Path = resolved

    def _resolve_rel_path(self, rel_path: str) -> str:
        """Validate a relative path stays inside self.root and return normalized POSIX string."""
        cleaned = (rel_path or "").strip()
        if not cleaned or cleaned == ".":
            return "."
        candidate = Path(cleaned)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise ValueError(f"Path must be relative inside workspace: {rel_path!r}")
        resolved = (self.root / candidate).resolve(strict=False)
        if not resolved.is_relative_to(self.root):
            raise ValueError(f"Path escapes workspace root: {rel_path!r}")
        return resolved.relative_to(self.root).as_posix() or "."

    def _resolve_paths(self, paths: list[str] | None) -> list[str]:
        if not paths:
            return []
        return [self._resolve_rel_path(str(p)) for p in paths if str(p).strip()]

    async def _run_git(
        self,
        args: list[str],
        *,
        timeout_s: float = LOCAL_TIMEOUT_S,
        require_repo: bool = True,
    ) -> dict[str, Any]:
        """Execute `git -C <root> <args>` safely without shell interpolation."""
        if require_repo and not await self.is_repo():
            return {
                "ok": False,
                "returncode": 128,
                "stdout": "",
                "stderr": "Not a git repository (run bytey_git__init first).",
                "timed_out": False,
            }
        proc = await asyncio.create_subprocess_exec(
            "git",
            "-C",
            str(self.root),
            *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            (stdout_b, stdout_trunc), (stderr_b, stderr_trunc), _ = await asyncio.wait_for(
                asyncio.gather(
                    _read_bounded_stream(proc.stdout, MAX_GIT_BYTES),
                    _read_bounded_stream(proc.stderr, MAX_GIT_BYTES),
                    proc.wait(),
                ),
                timeout=timeout_s,
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return {
                "ok": False,
                "returncode": -1,
                "stdout": "",
                "stderr": f"Git command timed out after {timeout_s}s",
                "timed_out": True,
            }
        return {
            "ok": proc.returncode == 0,
            "returncode": proc.returncode,
            "stdout": _cap_output(stdout_b, stdout_trunc),
            "stderr": _cap_output(stderr_b, stderr_trunc),
            "timed_out": False,
        }

    async def is_repo(self) -> bool:
        """Return True only when self.root is the Git worktree root."""
        res = await self._run_git(
            ["rev-parse", "--show-toplevel"],
            timeout_s=5.0,
            require_repo=False,
        )
        if not res["ok"]:
            return False
        toplevel = res["stdout"].strip()
        if not toplevel:
            return False
        try:
            return Path(toplevel).resolve() == self.root.resolve()
        except OSError:
            return False

    @staticmethod
    def format_result(res: dict[str, Any], empty_msg: str = "(ok)") -> dict[str, Any]:
        """Convert _run_git dict into standard tool response payload."""
        stdout = (res.get("stdout") or "").rstrip()
        stderr = (res.get("stderr") or "").rstrip()
        if res.get("ok"):
            content = stdout if stdout else (stderr if stderr else empty_msg)
            return {"ok": True, "content": content}
        err = stderr or stdout or f"git exited with code {res.get('returncode')}"
        return {"ok": False, "error": err, "content": err}

    # --- Read-only tools ---

    async def status(self, short: bool = True) -> dict[str, Any]:
        args = ["status", "--short", "--branch"] if short else ["status"]
        return self.format_result(await self._run_git(args), empty_msg="(clean working tree)")

    async def diff(
        self,
        *,
        staged: bool = False,
        ref: str = "",
        target_ref: str = "",
        rel_path: str = "",
        stat_only: bool = False,
    ) -> dict[str, Any]:
        args = ["diff"]
        if staged:
            args.append("--staged")
        if stat_only:
            args.append("--stat")
        if ref.strip():
            args.append(ref.strip())
        if target_ref.strip():
            args.append(target_ref.strip())
        if rel_path.strip():
            args.extend(["--", self._resolve_rel_path(rel_path)])
        return self.format_result(await self._run_git(args), empty_msg="(no diff)")

    async def log(
        self,
        *,
        limit: int = 20,
        ref: str = "",
        rel_path: str = "",
        oneline: bool = True,
        author: str = "",
        since: str = "",
    ) -> dict[str, Any]:
        clamped = max(1, min(200, int(limit)))
        args = ["log", f"-n{clamped}"]
        if oneline:
            args.append("--oneline")
        if author.strip():
            args.append(f"--author={author.strip()}")
        if since.strip():
            args.append(f"--since={since.strip()}")
        if ref.strip():
            args.append(ref.strip())
        if rel_path.strip():
            args.extend(["--", self._resolve_rel_path(rel_path)])
        return self.format_result(await self._run_git(args), empty_msg="(no commits)")

    async def show(
        self, ref: str = "HEAD", stat_only: bool = False, rel_path: str = ""
    ) -> dict[str, Any]:
        args = ["show"]
        if stat_only:
            args.append("--stat")
        args.append((ref or "HEAD").strip())
        if rel_path.strip():
            args.extend(["--", self._resolve_rel_path(rel_path)])
        return self.format_result(await self._run_git(args))

    async def blame(
        self, rel_path: str, start_line: int = 1, end_line: int = 200
    ) -> dict[str, Any]:
        safe_path = self._resolve_rel_path(rel_path)
        s = max(1, int(start_line))
        e = max(s, int(end_line))
        args = ["blame", f"-L{s},{e}", "--", safe_path]
        return self.format_result(await self._run_git(args))

    async def branch_list(self, include_remote: bool = True) -> dict[str, Any]:
        args = ["branch", "-vv"]
        if include_remote:
            args.append("-a")
        return self.format_result(await self._run_git(args), empty_msg="(no branches)")

    async def tag_list(self, pattern: str = "") -> dict[str, Any]:
        args = ["tag", "-l"]
        if pattern.strip():
            args.append(pattern.strip())
        return self.format_result(await self._run_git(args), empty_msg="(no tags)")

    async def stash_list(self) -> dict[str, Any]:
        return self.format_result(await self._run_git(["stash", "list"]), empty_msg="(no stashes)")

    async def remote_list(self) -> dict[str, Any]:
        return self.format_result(await self._run_git(["remote", "-v"]), empty_msg="(no remotes)")

    # --- Local write / mutating tools ---

    async def init(self, default_branch: str = "") -> dict[str, Any]:
        args = ["init"]
        if default_branch.strip():
            args.extend(["-b", default_branch.strip()])
        return self.format_result(await self._run_git(args, require_repo=False))

    async def stage(self, paths: list[str] | None = None, all_files: bool = False) -> dict[str, Any]:
        if all_files:
            args = ["add", "-A"]
        else:
            safe_paths = self._resolve_paths(paths)
            if not safe_paths:
                return {"ok": False, "error": "Provide paths or set all=true"}
            args = ["add", "--", *safe_paths]
        return self.format_result(await self._run_git(args), empty_msg="Staged changes.")

    async def unstage(self, paths: list[str] | None = None) -> dict[str, Any]:
        safe_paths = self._resolve_paths(paths)
        args = ["restore", "--staged", "--", *(safe_paths or ["."])]
        return self.format_result(await self._run_git(args), empty_msg="Unstaged changes.")

    async def commit(
        self, message: str, *, amend: bool = False, all_tracked: bool = False
    ) -> dict[str, Any]:
        msg = (message or "").strip()
        if not msg and not amend:
            return {"ok": False, "error": "Commit message must not be empty"}
        args = ["commit"]
        if all_tracked:
            args.append("-a")
        if amend:
            args.append("--amend")
        if msg:
            args.extend(["-m", msg])
        else:
            args.append("--no-edit")
        return self.format_result(await self._run_git(args))

    async def branch_create(
        self, name: str, start_point: str = "", checkout: bool = True
    ) -> dict[str, Any]:
        branch = (name or "").strip()
        if not branch:
            return {"ok": False, "error": "Branch name must not be empty"}
        args = ["checkout", "-b", branch] if checkout else ["branch", branch]
        if start_point.strip():
            args.append(start_point.strip())
        return self.format_result(await self._run_git(args))

    async def checkout(self, ref: str) -> dict[str, Any]:
        target = (ref or "").strip()
        if not target:
            return {"ok": False, "error": "Target branch or ref must not be empty"}
        return self.format_result(await self._run_git(["checkout", target]))

    async def branch_delete(self, name: str, force: bool = False) -> dict[str, Any]:
        branch = (name or "").strip()
        if not branch:
            return {"ok": False, "error": "Branch name must not be empty"}
        flag = "-D" if force else "-d"
        return self.format_result(await self._run_git(["branch", flag, branch]))

    async def merge(
        self, branch: str = "", *, no_ff: bool = False, abort: bool = False
    ) -> dict[str, Any]:
        if abort:
            return self.format_result(await self._run_git(["merge", "--abort"]))
        target = (branch or "").strip()
        if not target:
            return {"ok": False, "error": "Branch name must not be empty"}
        args = ["merge"]
        if no_ff:
            args.append("--no-ff")
        args.append(target)
        return self.format_result(await self._run_git(args))

    async def rebase(
        self,
        onto: str = "",
        *,
        abort: bool = False,
        continue_rebase: bool = False,
    ) -> dict[str, Any]:
        if abort:
            return self.format_result(await self._run_git(["rebase", "--abort"]))
        if continue_rebase:
            return self.format_result(await self._run_git(["rebase", "--continue"]))
        target = (onto or "").strip()
        if not target:
            return {"ok": False, "error": "Upstream branch/ref must not be empty"}
        return self.format_result(await self._run_git(["rebase", target]))

    async def cherry_pick(
        self, commit: str = "", *, abort: bool = False, continue_cp: bool = False
    ) -> dict[str, Any]:
        if abort:
            return self.format_result(await self._run_git(["cherry-pick", "--abort"]))
        if continue_cp:
            return self.format_result(await self._run_git(["cherry-pick", "--continue"]))
        target = (commit or "").strip()
        if not target:
            return {"ok": False, "error": "Commit ref must not be empty"}
        return self.format_result(await self._run_git(["cherry-pick", target]))

    async def stash(
        self,
        action: str = "push",
        *,
        message: str = "",
        ref: str = "",
        include_untracked: bool = False,
    ) -> dict[str, Any]:
        act = (action or "push").strip().lower()
        if act == "push":
            args = ["stash", "push"]
            if include_untracked:
                args.append("-u")
            if message.strip():
                args.extend(["-m", message.strip()])
        elif act in ("pop", "apply", "drop"):
            args = ["stash", act]
            if ref.strip():
                args.append(ref.strip())
        else:
            return {"ok": False, "error": f"Unsupported stash action: {action!r}"}
        return self.format_result(await self._run_git(args))

    async def tag_create(self, name: str, message: str = "", ref: str = "") -> dict[str, Any]:
        tag_name = (name or "").strip()
        if not tag_name:
            return {"ok": False, "error": "Tag name must not be empty"}
        args = ["tag"]
        if message.strip():
            args.extend(["-a", tag_name, "-m", message.strip()])
        else:
            args.append(tag_name)
        if ref.strip():
            args.append(ref.strip())
        return self.format_result(await self._run_git(args), empty_msg=f"Created tag {tag_name}")

    async def restore(self, paths: list[str], source: str = "") -> dict[str, Any]:
        safe_paths = self._resolve_paths(paths)
        if not safe_paths:
            return {"ok": False, "error": "Provide paths to restore"}
        args = ["restore"]
        if source.strip():
            args.extend(["--source", source.strip()])
        args.extend(["--", *safe_paths])
        return self.format_result(await self._run_git(args), empty_msg="Restored paths.")

    async def reset(self, ref: str = "HEAD", mode: str = "mixed") -> dict[str, Any]:
        m = (mode or "mixed").strip().lower()
        if m not in ("soft", "mixed", "hard"):
            return {"ok": False, "error": f"Invalid reset mode: {mode!r}"}
        target = (ref or "HEAD").strip()
        return self.format_result(await self._run_git(["reset", f"--{m}", target]))

    # --- Remote / network tools ---

    async def fetch(
        self, remote: str = "origin", prune: bool = False, all_remotes: bool = False
    ) -> dict[str, Any]:
        args = ["fetch"]
        if all_remotes:
            args.append("--all")
        elif remote.strip():
            args.append(remote.strip())
        if prune:
            args.append("--prune")
        return self.format_result(await self._run_git(args, timeout_s=REMOTE_TIMEOUT_S))

    async def pull(
        self, remote: str = "", branch: str = "", rebase: bool = False
    ) -> dict[str, Any]:
        args = ["pull"]
        if rebase:
            args.append("--rebase")
        if remote.strip():
            args.append(remote.strip())
            if branch.strip():
                args.append(branch.strip())
        return self.format_result(await self._run_git(args, timeout_s=REMOTE_TIMEOUT_S))

    async def push(
        self,
        remote: str = "origin",
        branch: str = "",
        *,
        set_upstream: bool = False,
        force_with_lease: bool = False,
    ) -> dict[str, Any]:
        args = ["push"]
        if set_upstream:
            args.append("-u")
        if force_with_lease:
            args.append("--force-with-lease")
        if remote.strip():
            args.append(remote.strip())
        if branch.strip():
            args.append(branch.strip())
        return self.format_result(await self._run_git(args, timeout_s=REMOTE_TIMEOUT_S))
