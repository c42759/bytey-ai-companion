"""MCP Client Host supporting JSON-RPC 2.0 over `stdio` and `http`/`sse`."""

from __future__ import annotations

import asyncio
import inspect
import itertools
import json
import logging
import os
import re
from collections import deque
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

from .schema_sync import _to_dict

logger = logging.getLogger(__name__)

MCP_PROTOCOL_VERSION = "2024-11-05"
_CLIENT_INFO = {"name": "bytey-ai-companion", "version": "0.1.0"}
_ENV_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_STDOUT_BUFFER_LIMIT = 4 * 1024 * 1024


class MCPProtocolError(RuntimeError):
    """Raised when an MCP server returns a JSON-RPC 2.0 error response."""

    def __init__(self, error_obj: Any) -> None:
        if isinstance(error_obj, dict):
            msg = str(error_obj.get("message") or error_obj)
            code = error_obj.get("code")
            super().__init__(f"MCP error ({code}): {msg}" if code is not None else msg)
        else:
            super().__init__(str(error_obj))
        self.error_obj = error_obj


async def _resolve_var(var_name: str, secret_resolver: Any) -> str | None:
    if secret_resolver is not None:
        if isinstance(secret_resolver, Mapping):
            val = secret_resolver.get(var_name)
            if val is not None:
                return str(val)
        elif hasattr(secret_resolver, "get_secret") and callable(secret_resolver.get_secret):
            val = secret_resolver.get_secret(var_name)
            if inspect.isawaitable(val):
                val = await val
            if val is not None:
                return str(val)
        elif callable(secret_resolver):
            val = secret_resolver(var_name)
            if inspect.isawaitable(val):
                val = await val
            if val is not None:
                return str(val)
    return os.environ.get(var_name)


async def _expand_string(value: str, secret_resolver: Any) -> str:
    matches = list(_ENV_VAR_RE.finditer(value))
    if not matches:
        return value
    cache: dict[str, str | None] = {}
    for match in matches:
        var_name = match.group(1)
        if var_name not in cache:
            cache[var_name] = await _resolve_var(var_name, secret_resolver)

    def replace(match: re.Match[str]) -> str:
        resolved = cache.get(match.group(1))
        return resolved if resolved is not None else match.group(0)

    return _ENV_VAR_RE.sub(replace, value)


async def _expand_mapping(mapping: Mapping[str, Any] | None, secret_resolver: Any) -> dict[str, str]:
    if not mapping:
        return {}
    expanded: dict[str, str] = {}
    for k, v in mapping.items():
        expanded[str(k)] = await _expand_string(str(v), secret_resolver)
    return expanded


def _infer_transport(cfg: dict[str, Any]) -> str:
    raw = (cfg.get("transport") or "").strip().lower()
    if raw in ("http", "streamable-http", "streamable_http"):
        return "http"
    if raw == "sse":
        return "sse"
    if raw == "stdio" or cfg.get("command"):
        return "stdio"
    url = cfg.get("url")
    if url:
        path = urlparse(str(url)).path.rstrip("/").lower()
        return "sse" if path.endswith("/sse") else "http"
    raise ValueError("MCP server configuration must specify either 'command' or 'url'")


def _config_fingerprint(cfg: dict[str, Any]) -> str:
    transport = _infer_transport(cfg)
    key_obj = {
        "transport": transport,
        "command": cfg.get("command"),
        "args": list(cfg.get("args") or []),
        "env": dict(cfg.get("env") or {}),
        "cwd": cfg.get("cwd"),
        "url": cfg.get("url"),
        "headers": dict(cfg.get("headers") or {}),
    }
    return json.dumps(key_obj, sort_keys=True)


def _extract_tool_result_text(result: Any) -> str:
    if isinstance(result, str):
        return result
    if not isinstance(result, dict):
        return json.dumps(result, ensure_ascii=False)

    blocks = result.get("content")
    if isinstance(blocks, str):
        return blocks
    parts: list[str] = []
    if isinstance(blocks, list):
        for block in blocks:
            if not isinstance(block, dict):
                parts.append(str(block))
                continue
            btype = block.get("type")
            if btype == "text":
                parts.append(str(block.get("text") or ""))
            elif btype == "resource":
                res = block.get("resource")
                if isinstance(res, dict) and "text" in res:
                    parts.append(str(res["text"]))
                else:
                    parts.append(json.dumps(block, ensure_ascii=False))
            else:
                parts.append(json.dumps(block, ensure_ascii=False))
    if not parts and "structuredContent" in result:
        return json.dumps(result["structuredContent"], ensure_ascii=False)
    return "\n".join(parts)


class _BaseMCPClient:
    """Common MCP high-level JSON-RPC methods."""

    def __init__(self, timeout: float = 30.0) -> None:
        self._timeout = timeout
        self._id_counter = itertools.count(1)

    @property
    def is_closed(self) -> bool:
        return True

    async def connect(self) -> None:
        raise NotImplementedError

    async def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        raise NotImplementedError

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        raise NotImplementedError

    async def close(self) -> None:
        raise NotImplementedError

    async def initialize_handshake(self) -> None:
        await self.request(
            "initialize",
            {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": _CLIENT_INFO,
            },
        )
        await self.notify("notifications/initialized")

    async def list_tools(self) -> list[dict[str, Any]]:
        tools: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            params: dict[str, Any] = {"cursor": cursor} if cursor else {}
            result = await self.request("tools/list", params)
            for tool in result.get("tools") or []:
                if not isinstance(tool, dict):
                    continue
                name = str(tool.get("name") or "").strip()
                if not name:
                    continue
                schema = tool.get("inputSchema")
                if not isinstance(schema, dict):
                    schema = {"type": "object", "properties": {}}
                tools.append(
                    {
                        "name": name,
                        "description": str(tool.get("description") or ""),
                        "inputSchema": schema,
                    }
                )
            cursor = result.get("nextCursor")
            if not cursor:
                break
        return tools

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        return await self.request(
            "tools/call",
            {"name": name, "arguments": arguments or {}},
        )


class _StdioMCPClient(_BaseMCPClient):
    """JSON-RPC 2.0 MCP client communicating over subprocess stdin/stdout."""

    def __init__(
        self,
        command: str,
        args: list[str],
        env: dict[str, str],
        cwd: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        super().__init__(timeout=timeout)
        self._command = command
        self._args = args
        self._env = env
        self._cwd = cwd
        self._proc: asyncio.subprocess.Process | None = None
        self._stdout_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._stderr_lines: deque[str] = deque(maxlen=100)
        self._pending: dict[int | str, asyncio.Future[dict[str, Any]]] = {}
        self._write_lock = asyncio.Lock()
        self._closed = False

    @property
    def is_closed(self) -> bool:
        if self._closed or self._proc is None:
            return True
        return self._proc.returncode is not None

    async def connect(self) -> None:
        merged_env = {**os.environ, **self._env}
        self._proc = await asyncio.create_subprocess_exec(
            self._command,
            *self._args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=merged_env,
            cwd=self._cwd or None,
            limit=_STDOUT_BUFFER_LIMIT,
        )
        self._stdout_task = asyncio.create_task(self._read_stdout())
        self._stderr_task = asyncio.create_task(self._read_stderr())
        try:
            await self.initialize_handshake()
        except Exception:
            await self.close()
            raise

    async def _read_stderr(self) -> None:
        assert self._proc is not None and self._proc.stderr is not None
        try:
            while True:
                raw = await self._proc.stderr.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", errors="replace").rstrip()
                if line:
                    self._stderr_lines.append(line)
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

    async def _read_stdout(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        try:
            while True:
                raw = await self._proc.stdout.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    logger.debug("Ignoring non-JSON stdout line from MCP server: %s", line)
                    continue
                if not isinstance(msg, dict):
                    continue
                msg_id = msg.get("id")
                if msg_id is not None and msg_id in self._pending:
                    fut = self._pending.pop(msg_id)
                    if not fut.done():
                        fut.set_result(msg)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            self._fail_pending(RuntimeError(f"MCP stdio read error: {exc}"))
        finally:
            stderr_msg = "\n".join(self._stderr_lines).strip()
            rc = self._proc.returncode if self._proc is not None else None
            detail = f"MCP stdio process exited (code={rc})"
            if stderr_msg:
                detail = f"{detail}: {stderr_msg}"
            self._fail_pending(RuntimeError(detail))

    def _fail_pending(self, exc: BaseException) -> None:
        for _, fut in list(self._pending.items()):
            if not fut.done():
                fut.set_exception(exc)
        self._pending.clear()

    async def _write_message(self, payload: dict[str, Any]) -> None:
        if self.is_closed or self._proc is None or self._proc.stdin is None:
            stderr_msg = "\n".join(self._stderr_lines).strip()
            raise RuntimeError(f"MCP stdio process is not running{': ' + stderr_msg if stderr_msg else ''}")
        data = (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")
        async with self._write_lock:
            self._proc.stdin.write(data)
            await self._proc.stdin.drain()

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        await self._write_message(payload)

    async def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        req_id = next(self._id_counter)
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending[req_id] = fut
        payload: dict[str, Any] = {"jsonrpc": "2.0", "id": req_id, "method": method}
        if params is not None:
            payload["params"] = params
        try:
            await self._write_message(payload)
            response = await asyncio.wait_for(fut, timeout=self._timeout)
        finally:
            self._pending.pop(req_id, None)

        if "error" in response and response["error"] is not None:
            raise MCPProtocolError(response["error"])
        result = response.get("result")
        return result if isinstance(result, dict) else {}

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._fail_pending(RuntimeError("MCP stdio client closed"))

        proc = self._proc
        if proc is not None and proc.stdin is not None and not proc.stdin.is_closing():
            try:
                proc.stdin.close()
            except Exception:
                pass

        for task in (self._stdout_task, self._stderr_task):
            if task is not None and not task.done():
                task.cancel()
        tasks = [t for t in (self._stdout_task, self._stderr_task) if t is not None]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        if proc is not None:
            if proc.returncode is None:
                try:
                    await asyncio.wait_for(proc.wait(), timeout=1.5)
                except asyncio.TimeoutError:
                    proc.terminate()
                    try:
                        await asyncio.wait_for(proc.wait(), timeout=1.5)
                    except asyncio.TimeoutError:
                        proc.kill()
                        await proc.wait()
            else:
                await proc.wait()


class _HttpMCPClient(_BaseMCPClient):
    """JSON-RPC 2.0 MCP client over Streamable HTTP / POST SSE (with legacy GET SSE fallback)."""

    def __init__(
        self,
        url: str,
        headers: dict[str, str],
        transport: str,
        http_client: httpx.AsyncClient,
        timeout: float = 30.0,
    ) -> None:
        super().__init__(timeout=timeout)
        self._url = url
        self._headers = headers
        self._transport = transport
        self._client = http_client
        self._session_id: str | None = None
        self._closed = False
        self._legacy_post_url: str | None = None
        self._legacy_task: asyncio.Task[None] | None = None
        self._legacy_endpoint_ready = asyncio.Event()
        self._legacy_pending: dict[int | str, asyncio.Future[dict[str, Any]]] = {}
        self._legacy_error: Exception | None = None

    @property
    def is_closed(self) -> bool:
        return self._closed

    async def connect(self) -> None:
        try:
            await self.initialize_handshake()
        except Exception:
            await self.close()
            raise

    def _build_headers(self) -> dict[str, str]:
        hdrs = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            **self._headers,
        }
        if self._session_id:
            hdrs["Mcp-Session-Id"] = self._session_id
        return hdrs

    async def _start_legacy_sse(self) -> None:
        if self._legacy_task is not None:
            return
        self._legacy_task = asyncio.create_task(self._run_legacy_sse())
        await asyncio.wait_for(self._legacy_endpoint_ready.wait(), timeout=self._timeout)
        if self._legacy_error is not None:
            raise self._legacy_error

    async def _run_legacy_sse(self) -> None:
        try:
            hdrs = {"Accept": "text/event-stream", **self._headers}
            async with self._client.stream("GET", self._url, headers=hdrs, timeout=None) as resp:
                if resp.status_code >= 400:
                    err_body = (await resp.aread()).decode("utf-8", errors="replace")
                    raise RuntimeError(f"MCP SSE error {resp.status_code}: {err_body}")
                event_name = "message"
                data_lines: list[str] = []
                async for line in resp.aiter_lines():
                    if line == "":
                        if data_lines:
                            raw_data = "\n".join(data_lines)
                            self._handle_legacy_event(event_name, raw_data)
                        event_name = "message"
                        data_lines = []
                    elif line.startswith("event:"):
                        event_name = line[6:].strip()
                    elif line.startswith("data:"):
                        data_lines.append(line[5:].lstrip())
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            self._legacy_error = exc
            self._legacy_endpoint_ready.set()
            for fut in list(self._legacy_pending.values()):
                if not fut.done():
                    fut.set_exception(exc)
            self._legacy_pending.clear()

    def _handle_legacy_event(self, event_name: str, raw_data: str) -> None:
        if event_name == "endpoint":
            self._legacy_post_url = urljoin(self._url, raw_data.strip())
            self._legacy_endpoint_ready.set()
            return
        try:
            msg = json.loads(raw_data)
        except json.JSONDecodeError:
            return
        if isinstance(msg, dict):
            msg_id = msg.get("id")
            if msg_id is not None and msg_id in self._legacy_pending:
                fut = self._legacy_pending.pop(msg_id)
                if not fut.done():
                    fut.set_result(msg)

    @staticmethod
    def _extract_matching_jsonrpc(obj: Any, req_id: int | str) -> dict[str, Any] | None:
        if isinstance(obj, dict):
            if obj.get("id") == req_id or ("id" not in obj and ("result" in obj or "error" in obj)):
                return obj
            if "result" in obj or "error" in obj:
                return obj
        elif isinstance(obj, list):
            for item in obj:
                if isinstance(item, dict) and item.get("id") == req_id:
                    return item
        return None

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        target_url = self._legacy_post_url or self._url
        resp = await self._client.post(
            target_url,
            headers=self._build_headers(),
            json=payload,
            timeout=self._timeout,
        )
        sid = resp.headers.get("mcp-session-id")
        if sid:
            self._session_id = sid
        if resp.status_code >= 400:
            raise RuntimeError(f"MCP HTTP notification error {resp.status_code}: {resp.text}")

    async def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        req_id = next(self._id_counter)
        payload: dict[str, Any] = {"jsonrpc": "2.0", "id": req_id, "method": method}
        if params is not None:
            payload["params"] = params

        if self._legacy_post_url is not None:
            return await self._legacy_request(req_id, payload)

        async with self._client.stream(
            "POST",
            self._url,
            headers=self._build_headers(),
            json=payload,
            timeout=self._timeout,
        ) as resp:
            if resp.status_code == 405 and self._transport == "sse":
                await resp.aclose()
                await self._start_legacy_sse()
                return await self._legacy_request(req_id, payload)

            sid = resp.headers.get("mcp-session-id")
            if sid:
                self._session_id = sid

            if resp.status_code >= 400:
                err_body = (await resp.aread()).decode("utf-8", errors="replace")
                raise RuntimeError(f"MCP HTTP error {resp.status_code}: {err_body}")

            ct = resp.headers.get("content-type", "").lower()
            response_msg: dict[str, Any] | None = None
            if "text/event-stream" in ct:
                data_lines: list[str] = []
                async for line in resp.aiter_lines():
                    if line == "":
                        if data_lines:
                            raw_event = "\n".join(data_lines)
                            data_lines = []
                            try:
                                parsed = json.loads(raw_event)
                            except json.JSONDecodeError:
                                continue
                            matched = self._extract_matching_jsonrpc(parsed, req_id)
                            if matched is not None:
                                response_msg = matched
                                break
                    elif line.startswith("data:"):
                        data_lines.append(line[5:].lstrip())
                if response_msg is None and data_lines:
                    try:
                        parsed = json.loads("\n".join(data_lines))
                        response_msg = self._extract_matching_jsonrpc(parsed, req_id)
                    except json.JSONDecodeError:
                        pass
            else:
                raw_bytes = await resp.aread()
                if raw_bytes:
                    parsed = json.loads(raw_bytes.decode("utf-8"))
                    response_msg = self._extract_matching_jsonrpc(parsed, req_id)

        if response_msg is None:
            raise RuntimeError(f"Empty or invalid JSON-RPC response from {self._url} for {method}")
        if "error" in response_msg and response_msg["error"] is not None:
            raise MCPProtocolError(response_msg["error"])
        result = response_msg.get("result")
        return result if isinstance(result, dict) else {}

    async def _legacy_request(self, req_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        assert self._legacy_post_url is not None
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._legacy_pending[req_id] = fut
        try:
            resp = await self._client.post(
                self._legacy_post_url,
                headers=self._build_headers(),
                json=payload,
                timeout=self._timeout,
            )
            if resp.status_code >= 400:
                raise RuntimeError(f"MCP SSE POST error {resp.status_code}: {resp.text}")
            if resp.content and "application/json" in resp.headers.get("content-type", "").lower():
                parsed = resp.json()
                matched = self._extract_matching_jsonrpc(parsed, req_id)
                if matched is not None:
                    response_msg = matched
                else:
                    response_msg = await asyncio.wait_for(fut, timeout=self._timeout)
            else:
                response_msg = await asyncio.wait_for(fut, timeout=self._timeout)
        finally:
            self._legacy_pending.pop(req_id, None)

        if "error" in response_msg and response_msg["error"] is not None:
            raise MCPProtocolError(response_msg["error"])
        result = response_msg.get("result")
        return result if isinstance(result, dict) else {}

    async def close(self) -> None:
        self._closed = True
        if self._legacy_task is not None and not self._legacy_task.done():
            self._legacy_task.cancel()
            await asyncio.gather(self._legacy_task, return_exceptions=True)
        for fut in list(self._legacy_pending.values()):
            if not fut.done():
                fut.set_exception(RuntimeError("MCP HTTP/SSE client closed"))
        self._legacy_pending.clear()


def _sanitize_fn_segment(raw: str) -> str:
    """Sanitize an MCP server or tool name segment into valid function-name characters."""
    cleaned = re.sub(r"[^a-zA-Z0-9_.:-]+", "_", raw.strip()).strip("_")
    if not cleaned:
        return "tool"
    if not (cleaned[0].isalpha() or cleaned[0] == "_"):
        cleaned = f"t_{cleaned}"
    return cleaned


class MCPClientManager:
    """Manages MCP server connections, tool discovery, confirmation gates, and execution."""

    def __init__(
        self,
        secret_resolver: Any = None,
        http_client: httpx.AsyncClient | None = None,
        request_timeout: float = 30.0,
    ) -> None:
        self._secret_resolver = secret_resolver
        self._http_client = http_client
        self._owns_http_client = http_client is None
        self._timeout = request_timeout
        self._sessions: dict[str, tuple[str, _BaseMCPClient]] = {}
        self._tool_alias_map: dict[str, tuple[str, str]] = {}

    def _get_http_client(self) -> httpx.AsyncClient:
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(
                timeout=httpx.Timeout(self._timeout, connect=10.0)
            )
            self._owns_http_client = True
        return self._http_client

    async def _create_client(self, cfg: dict[str, Any]) -> _BaseMCPClient:
        transport = _infer_transport(cfg)
        if transport == "stdio":
            command = str(cfg.get("command") or "").strip()
            if not command:
                raise ValueError("stdio MCP server requires a non-empty 'command'")
            args = [str(a) for a in (cfg.get("args") or [])]
            env = await _expand_mapping(cfg.get("env"), self._secret_resolver)
            cwd = cfg.get("cwd")
            client: _BaseMCPClient = _StdioMCPClient(
                command=command,
                args=args,
                env=env,
                cwd=str(cwd) if cwd else None,
                timeout=self._timeout,
            )
        else:
            url = str(cfg.get("url") or "").strip()
            if not url:
                raise ValueError(f"{transport} MCP server requires a non-empty 'url'")
            headers = await _expand_mapping(cfg.get("headers"), self._secret_resolver)
            client = _HttpMCPClient(
                url=url,
                headers=headers,
                transport=transport,
                http_client=self._get_http_client(),
                timeout=self._timeout,
            )

        await client.connect()
        return client

    async def _get_or_connect(self, name: str, cfg: dict[str, Any]) -> _BaseMCPClient:
        fingerprint = _config_fingerprint(cfg)
        cached = self._sessions.get(name)
        if cached is not None:
            prev_fp, client = cached
            if prev_fp == fingerprint and not client.is_closed:
                return client
            await client.close()
            self._sessions.pop(name, None)

        client = await self._create_client(cfg)
        self._sessions[name] = (fingerprint, client)
        return client

    async def test_server(self, name: str, cfg: dict[str, Any] | Any) -> dict[str, Any]:
        """Connect temporarily to an MCP server, discover its tools, and close cleanly."""
        client: _BaseMCPClient | None = None
        try:
            cfg_dict = _to_dict(cfg)
            client = await self._create_client(cfg_dict)
            tools = await client.list_tools()
            return {"ok": True, "tools": tools, "error": None}
        except Exception as exc:
            return {"ok": False, "tools": [], "error": str(exc)}
        finally:
            if client is not None:
                await client.close()

    async def list_active_tools(self, servers_cfg: dict[str, Any]) -> list[dict[str, Any]]:
        """Discover enabled tools from all enabled MCP servers in OpenAI tool schema format."""
        normalized_cfgs: dict[str, dict[str, Any]] = {}
        for name, raw_cfg in (servers_cfg or {}).items():
            cfg = _to_dict(raw_cfg)
            if cfg.get("enabled", True):
                normalized_cfgs[str(name)] = cfg

        for cached_name in list(self._sessions.keys()):
            if cached_name not in normalized_cfgs:
                _, old_client = self._sessions.pop(cached_name)
                await old_client.close()

        active_tools: list[dict[str, Any]] = []
        for server_name, cfg in normalized_cfgs.items():
            disabled = set(str(t) for t in (cfg.get("disabled_tools") or []))
            try:
                client = await self._get_or_connect(server_name, cfg)
                raw_tools = await client.list_tools()
            except Exception as exc:
                logger.warning("Failed to list tools from MCP server '%s': %s", server_name, exc)
                stale = self._sessions.pop(server_name, None)
                if stale is not None:
                    await stale[1].close()
                continue

            for tool in raw_tools:
                tool_name = tool["name"]
                if tool_name in disabled:
                    continue
                safe_server = _sanitize_fn_segment(server_name)
                safe_tool = _sanitize_fn_segment(tool_name)
                safe_name = f"{safe_server}__{safe_tool}"[:128]
                self._tool_alias_map[safe_name] = (server_name, tool_name)
                active_tools.append(
                    {
                        "type": "function",
                        "function": {
                            "name": safe_name,
                            "description": tool.get("description", ""),
                            "parameters": tool.get("inputSchema")
                            or {"type": "object", "properties": {}},
                        },
                        "server": server_name,
                        "tool": tool_name,
                    }
                )
        return active_tools

    def _split_namespaced_tool(
        self,
        namespaced_tool: str,
        servers_cfg: dict[str, Any],
    ) -> tuple[str, str] | None:
        alias = self._tool_alias_map.get(namespaced_tool)
        if alias is not None:
            return alias
        if "__" not in namespaced_tool:
            return None
        server_name, tool_name = namespaced_tool.split("__", 1)
        if server_name in servers_cfg:
            return server_name, tool_name
        for candidate in servers_cfg:
            for prefix in (f"{candidate}__", f"{_sanitize_fn_segment(candidate)}__"):
                if namespaced_tool.startswith(prefix):
                    return candidate, namespaced_tool[len(prefix):]
        return server_name, tool_name

    async def execute_tool_call(
        self,
        namespaced_tool: str,
        arguments: dict[str, Any] | None,
        servers_cfg: dict[str, Any],
        confirm_callback: Callable[[str, str, dict[str, Any]], Any] | None = None,
    ) -> dict[str, Any]:
        """Execute a namespaced (`server__tool`) MCP tool call with confirmation gating."""
        split = self._split_namespaced_tool(namespaced_tool, servers_cfg or {})
        if split is None:
            return {
                "ok": False,
                "content": f"Invalid namespaced tool name: {namespaced_tool}",
                "error": "invalid_tool_name",
                "denied": False,
            }
        server_name, tool_name = split
        if server_name not in (servers_cfg or {}):
            return {
                "ok": False,
                "content": f"Unknown MCP server: {server_name}",
                "error": "unknown_server",
                "denied": False,
            }

        cfg = _to_dict(servers_cfg[server_name])
        if not cfg.get("enabled", True):
            return {
                "ok": False,
                "content": f"MCP server '{server_name}' is disabled",
                "error": "server_disabled",
                "denied": False,
            }
        disabled_tools = set(str(t) for t in (cfg.get("disabled_tools") or []))
        if tool_name in disabled_tools:
            return {
                "ok": False,
                "content": f"Tool '{tool_name}' on server '{server_name}' is disabled",
                "error": "tool_disabled",
                "denied": False,
            }

        args = arguments if isinstance(arguments, dict) else {}

        if bool(cfg.get("require_confirmation", False)):
            if confirm_callback is None:
                return {
                    "ok": False,
                    "content": "Denied by user",
                    "error": "denied",
                    "denied": True,
                }
            decision = confirm_callback(server_name, tool_name, args)
            if inspect.isawaitable(decision):
                decision = await decision
            if not decision:
                return {
                    "ok": False,
                    "content": "Denied by user",
                    "error": "denied",
                    "denied": True,
                }

        try:
            client = await self._get_or_connect(server_name, cfg)
            try:
                result = await client.call_tool(tool_name, args)
            except MCPProtocolError:
                raise
            except Exception:
                if client.is_closed:
                    self._sessions.pop(server_name, None)
                    await client.close()
                    client = await self._get_or_connect(server_name, cfg)
                    result = await client.call_tool(tool_name, args)
                else:
                    raise

            text = _extract_tool_result_text(result)
            is_error = bool(result.get("isError", False)) if isinstance(result, dict) else False
            if is_error:
                return {
                    "ok": False,
                    "content": text,
                    "error": text or "Tool execution returned an error",
                    "denied": False,
                }
            return {"ok": True, "content": text, "error": None, "denied": False}
        except Exception as exc:
            msg = str(exc)
            return {"ok": False, "content": msg, "error": msg, "denied": False}

    async def close_all(self) -> None:
        """Terminate all cached stdio subprocesses and close owned HTTP clients."""
        sessions = list(self._sessions.values())
        self._sessions.clear()
        if sessions:
            await asyncio.gather(*(client.close() for _, client in sessions), return_exceptions=True)

        if self._owns_http_client and self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None
