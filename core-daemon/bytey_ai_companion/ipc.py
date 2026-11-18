"""Single-port localhost HTTP/1.1 REST and WebSocket IPC server."""

from __future__ import annotations

import asyncio
import http
import inspect
import json
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qsl, unquote

from pydantic import ValidationError
from websockets.asyncio.server import Server as WsServer
from websockets.asyncio.server import ServerConnection
from websockets.exceptions import ConnectionClosed
from websockets.extensions.permessage_deflate import enable_server_permessage_deflate
from websockets.http11 import Request as WsRequest
from websockets.http11 import Response as WsResponse
from websockets.server import ServerProtocol

logger = logging.getLogger(__name__)

_ALLOWED_HOSTS = {"127.0.0.1", "::1", "localhost"}
_PARAM_RE = re.compile(r"^\{([A-Za-z_][A-Za-z0-9_]*)\}$")
_MAX_HEADER_BYTES = 64 * 1024
_MAX_BODY_BYTES = 16 * 1024 * 1024


@dataclass(slots=True)
class IPCRequest:
    """Normalized HTTP/1.1 request delivered to route handlers."""

    method: str
    path: str
    query: dict[str, str] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)
    body_bytes: bytes = b""
    path_params: dict[str, str] = field(default_factory=dict)

    def json(self) -> Any:
        """Decode request body bytes as JSON (returns {} when empty)."""
        if not self.body_bytes or not self.body_bytes.strip():
            return {}
        return json.loads(self.body_bytes.decode("utf-8"))

    def text(self) -> str:
        """Decode request body bytes as UTF-8 text."""
        return self.body_bytes.decode("utf-8", errors="replace")


@dataclass(slots=True)
class IPCResponse:
    """HTTP response returned by route handlers."""

    status: int = 200
    body: Any = None
    headers: dict[str, str] = field(default_factory=dict)

    def __init__(
        self,
        status: int = 200,
        body: Any = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status = status
        self.body = body
        self.headers = dict(headers) if headers is not None else {}


RouteHandler = Callable[[IPCRequest], Awaitable[Any] | Any]


@dataclass(slots=True)
class _Route:
    method: str
    pattern: str
    regex: re.Pattern[str]
    literal_count: int
    order: int
    handler: RouteHandler


class ConnectionManager:
    """Track active WebSocket clients and broadcast JSON event payloads."""

    def __init__(
        self,
        on_ws_message: Callable[..., Awaitable[None] | None] | None = None,
    ) -> None:
        self.clients: set[ServerConnection] = set()
        self.on_ws_message = on_ws_message

    async def connect(self, websocket: ServerConnection) -> None:
        """Register an open WebSocket connection."""
        self.clients.add(websocket)

    def disconnect(self, websocket: ServerConnection) -> None:
        """Remove a WebSocket connection if present."""
        self.clients.discard(websocket)

    async def _dispatch_message(
        self, websocket: ServerConnection, message: str | bytes
    ) -> None:
        cb = self.on_ws_message
        if cb is None:
            return
        try:
            params = [
                p
                for p in inspect.signature(cb).parameters.values()
                if p.kind
                in (
                    inspect.Parameter.POSITIONAL_ONLY,
                    inspect.Parameter.POSITIONAL_OR_KEYWORD,
                )
            ]
            if len(params) == 1:
                result = cb(message)
            else:
                result = cb(websocket, message)
            if inspect.isawaitable(result):
                await result
        except Exception:
            logger.exception("WebSocket on_ws_message callback raised an error")

    async def handle_connection(self, websocket: ServerConnection) -> None:
        """Manage a WebSocket connection lifecycle until close."""
        await self.connect(websocket)
        try:
            async for message in websocket:
                await self._dispatch_message(websocket, message)
        except ConnectionClosed:
            pass
        finally:
            self.disconnect(websocket)

    async def broadcast(self, event: dict[str, Any]) -> None:
        """Send an event dict as JSON to all connected clients, dropping dead sockets."""
        if not self.clients:
            return
        payload = json.dumps(event)
        snapshot = list(self.clients)

        async def _send_one(ws: ServerConnection) -> None:
            try:
                await ws.send(payload)
            except Exception:
                self.clients.discard(ws)

        await asyncio.gather(*(_send_one(ws) for ws in snapshot))


def _compile_route_pattern(pattern: str) -> tuple[re.Pattern[str], int]:
    """Compile a path pattern with {param} segments into a regex and specificity score."""
    stripped = pattern.strip("/")
    if not stripped:
        return re.compile(r"^/$"), 0

    parts: list[str] = []
    literal_count = 0
    for segment in stripped.split("/"):
        match = _PARAM_RE.match(segment)
        if match:
            param_name = match.group(1)
            parts.append(f"(?P<{param_name}>[^/]+)")
        else:
            literal_count += 1
            parts.append(re.escape(segment))
    return re.compile("^/" + "/".join(parts) + "$"), literal_count


def _parse_http_head(raw_head: bytes) -> tuple[str, str, str, dict[str, str]]:
    """Parse HTTP/1.x request line and headers into normalized components."""
    lines = raw_head.split(b"\r\n")
    if not lines:
        raise ValueError("Empty HTTP request")

    request_line = lines[0].decode("ascii", errors="strict")
    parts = request_line.split(" ")
    if len(parts) != 3:
        raise ValueError(f"Malformed HTTP request line: {request_line!r}")

    method, target, version = parts[0].upper(), parts[1], parts[2].upper()
    if not version.startswith("HTTP/1."):
        raise ValueError(f"Unsupported HTTP version: {version!r}")

    headers: dict[str, str] = {}
    for raw_line in lines[1:]:
        if not raw_line:
            continue
        if b":" not in raw_line:
            raise ValueError("Malformed HTTP header line")
        name_b, val_b = raw_line.split(b":", 1)
        name = name_b.decode("ascii", errors="strict").strip().lower()
        val = val_b.decode("latin-1").strip()
        if name in headers:
            headers[name] = f"{headers[name]}, {val}"
        else:
            headers[name] = val

    return method, target, version, headers


def _is_websocket_upgrade(method: str, headers: dict[str, str]) -> bool:
    if method != "GET":
        return False
    if headers.get("upgrade", "").strip().lower() != "websocket":
        return False
    conn_tokens = {
        tok.strip().lower() for tok in headers.get("connection", "").split(",")
    }
    return "upgrade" in conn_tokens


def _reason_phrase(status: int) -> str:
    try:
        return http.HTTPStatus(status).phrase
    except ValueError:
        return "OK" if status < 400 else "Error"


def _serialize_http_response(
    response: IPCResponse,
    *,
    keep_alive: bool,
    head_only: bool = False,
) -> bytes:
    headers = {str(k): str(v) for k, v in response.headers.items()}
    lower_keys = {k.lower() for k in headers}

    body = response.body
    if body is None:
        payload = b""
    elif isinstance(body, (bytes, bytearray)):
        payload = bytes(body)
        if "content-type" not in lower_keys:
            headers["Content-Type"] = "application/octet-stream"
    elif isinstance(body, str):
        payload = body.encode("utf-8")
        if "content-type" not in lower_keys:
            headers["Content-Type"] = "text/plain; charset=utf-8"
    else:
        payload = json.dumps(body).encode("utf-8")
        if "content-type" not in lower_keys:
            headers["Content-Type"] = "application/json"

    if "content-length" not in lower_keys:
        headers["Content-Length"] = str(len(payload))
    if "connection" not in lower_keys:
        headers["Connection"] = "keep-alive" if keep_alive else "close"

    status_line = f"HTTP/1.1 {response.status} {_reason_phrase(response.status)}\r\n"
    header_lines = "".join(f"{k}: {v}\r\n" for k, v in headers.items())
    head_bytes = (status_line + header_lines + "\r\n").encode("latin-1")
    return head_bytes if head_only else head_bytes + payload


class _DualIPCProtocol(asyncio.Protocol):
    """Demultiplexes incoming TCP streams to WebSocket or HTTP/1.1 handling."""

    def __init__(self, ipc_server: IPCServer) -> None:
        self._ipc = ipc_server
        self._transport: asyncio.Transport | None = None
        self._initial_buf = bytearray()
        self._reader: asyncio.StreamReader | None = None
        self._http_task: asyncio.Task[None] | None = None
        self._handed_off = False
        self._eof = False

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        assert isinstance(transport, asyncio.Transport)
        self._transport = transport
        self._ipc._track_protocol(self)

    def data_received(self, data: bytes) -> None:
        if self._handed_off:
            return
        if self._reader is not None:
            self._reader.feed_data(data)
            return

        self._initial_buf.extend(data)
        hdr_end = self._initial_buf.find(b"\r\n\r\n")
        if hdr_end == -1:
            if len(self._initial_buf) > _MAX_HEADER_BYTES:
                self._send_error_and_close(431, "Request headers too large")
            return

        if hdr_end > _MAX_HEADER_BYTES:
            self._send_error_and_close(431, "Request headers too large")
            return

        raw_head = bytes(self._initial_buf[:hdr_end])
        try:
            method, target, _, headers = _parse_http_head(raw_head)
        except ValueError as exc:
            self._send_error_and_close(400, str(exc))
            return

        if _is_websocket_upgrade(method, headers):
            self._handoff_to_websocket()
            return

        reader = asyncio.StreamReader(limit=_MAX_BODY_BYTES)
        reader.feed_data(bytes(self._initial_buf))
        self._initial_buf.clear()
        if self._eof:
            reader.feed_eof()
        self._reader = reader
        self._http_task = asyncio.get_running_loop().create_task(
            self._run_http_loop(reader)
        )

    def eof_received(self) -> bool | None:
        self._eof = True
        if self._reader is not None:
            self._reader.feed_eof()
            return True
        return False

    def connection_lost(self, exc: Exception | None) -> None:
        if self._reader is not None:
            self._reader.feed_eof()
        self._ipc._untrack_protocol(self)

    def close_transport(self) -> None:
        if self._http_task is not None and not self._http_task.done():
            self._http_task.cancel()
        if self._transport is not None and not self._transport.is_closing():
            self._transport.close()

    def _send_error_and_close(self, status: int, message: str) -> None:
        if self._transport is not None and not self._transport.is_closing():
            raw = _serialize_http_response(
                IPCResponse(status=status, body={"error": message}),
                keep_alive=False,
            )
            self._transport.write(raw)
            self._transport.close()

    def _handoff_to_websocket(self) -> None:
        ws_server = self._ipc._ws_server
        if self._transport is None or ws_server is None:
            self._send_error_and_close(503, "WebSocket server unavailable")
            return

        self._handed_off = True
        self._ipc._untrack_protocol(self)
        protocol = ServerProtocol(
            extensions=enable_server_permessage_deflate(None),
            max_size=2**20,
        )
        ws_conn = ServerConnection(
            protocol,
            ws_server,
            ping_interval=20,
            ping_timeout=20,
            close_timeout=10,
        )
        raw_bytes = bytes(self._initial_buf)
        self._initial_buf.clear()
        self._transport.set_protocol(ws_conn)
        ws_conn.connection_made(self._transport)
        ws_conn.data_received(raw_bytes)

    async def _read_headers(self, reader: asyncio.StreamReader) -> bytes | None:
        buf = bytearray()
        while True:
            try:
                line = await reader.readline()
            except ValueError:
                raise ValueError("HTTP header line too long") from None
            if not line:
                return None if not buf else bytes(buf)
            if line == b"\r\n":
                return bytes(buf).rstrip(b"\r\n")
            buf.extend(line)
            if len(buf) > _MAX_HEADER_BYTES:
                raise ValueError("Request headers too large")

    async def _read_chunked_body(self, reader: asyncio.StreamReader) -> bytes:
        chunks = bytearray()
        while True:
            size_line = await reader.readline()
            if not size_line:
                raise ValueError("Unexpected EOF in chunked request body")
            size_str = size_line.split(b";", 1)[0].strip()
            try:
                chunk_size = int(size_str, 16)
            except ValueError as exc:
                raise ValueError("Invalid chunk size") from exc
            if chunk_size < 0:
                raise ValueError("Negative chunk size")
            if chunk_size == 0:
                while True:
                    trailer = await reader.readline()
                    if not trailer or trailer == b"\r\n":
                        break
                return bytes(chunks)
            if len(chunks) + chunk_size > _MAX_BODY_BYTES:
                raise OverflowError("Request body too large")
            chunks.extend(await reader.readexactly(chunk_size))
            crlf = await reader.readexactly(2)
            if crlf != b"\r\n":
                raise ValueError("Malformed chunk terminator")

    async def _read_body(
        self, reader: asyncio.StreamReader, headers: dict[str, str]
    ) -> bytes:
        te = headers.get("transfer-encoding", "").lower()
        if "chunked" in te:
            return await self._read_chunked_body(reader)
        if "content-length" in headers:
            try:
                length = int(headers["content-length"])
            except ValueError as exc:
                raise ValueError("Invalid Content-Length header") from exc
            if length < 0:
                raise ValueError("Negative Content-Length")
            if length > _MAX_BODY_BYTES:
                raise OverflowError("Request body too large")
            if length == 0:
                return b""
            return await reader.readexactly(length)
        return b""

    async def _run_http_loop(self, reader: asyncio.StreamReader) -> None:
        try:
            while self._transport is not None and not self._transport.is_closing():
                try:
                    raw_head = await self._read_headers(reader)
                except ValueError as exc:
                    self._send_error_and_close(400, str(exc))
                    return

                if raw_head is None:
                    break

                try:
                    method, target, version, headers = _parse_http_head(raw_head)
                except ValueError as exc:
                    self._send_error_and_close(400, str(exc))
                    return

                try:
                    body_bytes = await self._read_body(reader, headers)
                except OverflowError as exc:
                    self._send_error_and_close(413, str(exc))
                    return
                except (ValueError, asyncio.IncompleteReadError) as exc:
                    self._send_error_and_close(400, str(exc))
                    return

                conn_hdr = headers.get("connection", "").lower()
                if version == "HTTP/1.0":
                    keep_alive = "keep-alive" in conn_hdr and not self._ipc._stopping
                else:
                    keep_alive = "close" not in conn_hdr and not self._ipc._stopping

                response = await self._ipc._dispatch_http(
                    method, target, headers, body_bytes
                )
                if self._transport is None or self._transport.is_closing():
                    break

                self._transport.write(
                    _serialize_http_response(
                        response,
                        keep_alive=keep_alive,
                        head_only=(method == "HEAD"),
                    )
                )
                if not keep_alive:
                    self._transport.close()
                    break
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("Unhandled error in HTTP connection loop")
            if self._transport is not None and not self._transport.is_closing():
                self._transport.close()
        finally:
            if self._transport is not None and not self._transport.is_closing():
                self._transport.close()
            self._ipc._untrack_protocol(self)


class IPCServer:
    """Localhost-only single-port HTTP/1.1 REST and WebSocket server."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 8765,
        on_ws_message: Callable[..., Awaitable[None] | None] | None = None,
        ws_path: str = "/ws",
    ) -> None:
        if host not in _ALLOWED_HOSTS:
            raise ValueError(
                f"IPCServer must bind to localhost only ({sorted(_ALLOWED_HOSTS)}), got {host!r}"
            )
        self.host = host
        self.port = port
        self.ws_path = ws_path
        self.connection_manager = ConnectionManager(on_ws_message=on_ws_message)
        self._routes: list[_Route] = []
        self._route_seq = 0
        self._ws_server: WsServer | None = None
        self._tcp_server: asyncio.Server | None = None
        self._http_protocols: set[_DualIPCProtocol] = set()
        self._stopping = False

    def _track_protocol(self, proto: _DualIPCProtocol) -> None:
        self._http_protocols.add(proto)

    def _untrack_protocol(self, proto: _DualIPCProtocol) -> None:
        self._http_protocols.discard(proto)

    def register_route(
        self,
        method: str,
        pattern: str,
        handler: RouteHandler,
    ) -> None:
        """Register an HTTP route handler supporting {param} path segments."""
        regex, literal_count = _compile_route_pattern(pattern)
        self._route_seq += 1
        self._routes.append(
            _Route(
                method=method.upper(),
                pattern=pattern,
                regex=regex,
                literal_count=literal_count,
                order=self._route_seq,
                handler=handler,
            )
        )
        # Prefer routes with more literal path segments over parameterized matches.
        self._routes.sort(key=lambda r: (-r.literal_count, r.order))

    def _match_route(
        self, method: str, path: str
    ) -> tuple[_Route | None, dict[str, str], list[str]]:
        allowed_methods: list[str] = []
        matched_route: _Route | None = None
        matched_params: dict[str, str] = {}

        for route in self._routes:
            match = route.regex.match(path)
            if not match:
                continue
            if route.method not in allowed_methods:
                allowed_methods.append(route.method)
            if route.method == method and matched_route is None:
                matched_route = route
                matched_params = {
                    k: unquote(v) for k, v in match.groupdict().items()
                }
        return matched_route, matched_params, allowed_methods

    async def _dispatch_http(
        self,
        method: str,
        target: str,
        headers: dict[str, str],
        body_bytes: bytes,
    ) -> IPCResponse:
        if "?" in target:
            raw_path, raw_query = target.split("?", 1)
        else:
            raw_path, raw_query = target, ""

        path = unquote(raw_path) or "/"
        query = dict(parse_qsl(raw_query, keep_blank_values=True))

        lookup_method = "GET" if method == "HEAD" else method
        route, path_params, allowed_methods = self._match_route(lookup_method, path)

        if route is None and method == "HEAD":
            route, path_params, allowed_methods = self._match_route("HEAD", path)

        if route is None:
            if method == "OPTIONS" and allowed_methods:
                return IPCResponse(
                    status=204,
                    body=None,
                    headers={"Allow": ", ".join(sorted(set(allowed_methods)))},
                )
            if allowed_methods:
                return IPCResponse(
                    status=405,
                    body={"error": "Method not allowed"},
                    headers={"Allow": ", ".join(sorted(set(allowed_methods)))},
                )
            return IPCResponse(status=404, body={"error": "Not found"})

        request = IPCRequest(
            method=method,
            path=path,
            query=query,
            headers=headers,
            body_bytes=body_bytes,
            path_params=path_params,
        )
        try:
            result = route.handler(request)
            if inspect.isawaitable(result):
                result = await result
            if isinstance(result, IPCResponse):
                return result
            return IPCResponse(status=200, body=result)
        except (json.JSONDecodeError, ValueError) as exc:
            return IPCResponse(status=400, body={"error": str(exc)})
        except ValidationError as exc:
            return IPCResponse(status=422, body={"error": exc.errors()})
        except Exception:
            logger.exception("Unhandled route error for %s %s", method, path)
            return IPCResponse(status=500, body={"error": "Internal server error"})

    def _process_ws_request(
        self, connection: ServerConnection, request: WsRequest
    ) -> WsResponse | None:
        raw_path = request.path.split("?", 1)[0]
        if unquote(raw_path) != self.ws_path:
            return connection.respond(
                http.HTTPStatus.NOT_FOUND,
                "WebSocket endpoint not found\n",
            )
        return None

    async def start(self) -> None:
        """Bind and start serving HTTP/1.1 and WebSocket traffic on a single port."""
        if self._tcp_server is not None:
            return

        self._stopping = False
        loop = asyncio.get_running_loop()
        self._ws_server = WsServer(
            self.connection_manager.handle_connection,
            process_request=self._process_ws_request,
        )
        self._tcp_server = await loop.create_server(
            lambda: _DualIPCProtocol(self),
            self.host,
            self.port,
        )
        self._ws_server.wrap(self._tcp_server)
        if self._tcp_server.sockets:
            self.port = int(self._tcp_server.sockets[0].getsockname()[1])

    async def stop(self) -> None:
        """Close all HTTP/WebSocket connections and stop the server cleanly."""
        if self._ws_server is None and self._tcp_server is None:
            return

        self._stopping = True
        for proto in list(self._http_protocols):
            proto.close_transport()

        ws_server = self._ws_server
        self._ws_server = None
        self._tcp_server = None
        if ws_server is not None:
            ws_server.close()
            await ws_server.wait_closed()
        self._http_protocols.clear()
