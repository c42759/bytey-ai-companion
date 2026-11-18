"""Bidirectional adapter between internal MCP server configs and `mcpServers` JSON."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlparse

from pydantic import BaseModel

_VALID_TRANSPORTS = frozenset({"stdio", "http", "sse", "streamable-http", "streamable_http"})


def _to_dict(item: Any) -> dict[str, Any]:
    if isinstance(item, BaseModel):
        return item.model_dump()
    if isinstance(item, dict):
        return dict(item)
    raise ValueError(f"Expected dict or Pydantic model for server config, got {type(item).__name__}")


def _normalize_transport(raw_transport: Any) -> str | None:
    if raw_transport is None:
        return None
    if not isinstance(raw_transport, str):
        raise ValueError(f"Invalid transport type: {type(raw_transport).__name__}")
    norm = raw_transport.strip().lower()
    if norm not in _VALID_TRANSPORTS:
        raise ValueError(f"Unsupported MCP transport: {raw_transport!r}")
    if norm in ("streamable-http", "streamable_http"):
        return "http"
    return norm


def _validate_str_list(val: Any, field: str, server_name: str) -> list[str]:
    if not isinstance(val, list) or not all(isinstance(x, str) for x in val):
        raise ValueError(f"MCP server '{server_name}' field '{field}' must be a list of strings")
    return list(val)


def _validate_str_dict(val: Any, field: str, server_name: str) -> dict[str, str]:
    if not isinstance(val, dict):
        raise ValueError(f"MCP server '{server_name}' field '{field}' must be an object")
    out: dict[str, str] = {}
    for k, v in val.items():
        if not isinstance(k, str) or not isinstance(v, str):
            raise ValueError(
                f"MCP server '{server_name}' field '{field}' must map strings to strings"
            )
        out[k] = v
    return out


def to_mcp_servers_json(servers: dict[str, Any]) -> str:
    """Serialize internal MCP server configs into pretty-printed `{ "mcpServers": ... }` JSON."""
    serialized: dict[str, dict[str, Any]] = {}
    for name, raw_cfg in servers.items():
        cfg = _to_dict(raw_cfg)
        entry: dict[str, Any] = {}

        command = cfg.get("command")
        url = cfg.get("url")
        transport = _normalize_transport(cfg.get("transport"))

        if command or transport == "stdio":
            if command:
                entry["command"] = str(command)
            args = cfg.get("args")
            if args:
                entry["args"] = list(args)
            env = cfg.get("env")
            if env:
                entry["env"] = dict(env)
            cwd = cfg.get("cwd")
            if cwd:
                entry["cwd"] = str(cwd)
        elif url or transport in ("http", "sse"):
            if url:
                entry["url"] = str(url)
            entry["transport"] = transport or "http"
            headers = cfg.get("headers")
            if headers:
                entry["headers"] = dict(headers)

        if cfg.get("enabled") is False:
            entry["enabled"] = False
        if cfg.get("require_confirmation") is True:
            entry["require_confirmation"] = True
        disabled_tools = cfg.get("disabled_tools")
        if disabled_tools:
            entry["disabled_tools"] = list(disabled_tools)

        serialized[str(name)] = entry

    return json.dumps({"mcpServers": serialized}, indent=2)


def from_mcp_servers_json(
    raw_json: str,
    existing: dict[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    """Parse and validate `mcpServers` JSON into normalized server config dicts."""
    if not isinstance(raw_json, str):
        raise ValueError("MCP configuration JSON must be a string")
    try:
        parsed = json.loads(raw_json)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in MCP configuration: {exc}") from exc

    if not isinstance(parsed, dict) or "mcpServers" not in parsed:
        raise ValueError("MCP configuration must be a JSON object with a 'mcpServers' key")

    servers_raw = parsed["mcpServers"]
    if not isinstance(servers_raw, dict):
        raise ValueError("'mcpServers' must be a JSON object mapping server names to configs")

    existing_map: dict[str, dict[str, Any]] = {}
    if existing:
        for k, v in existing.items():
            existing_map[str(k)] = _to_dict(v)

    result: dict[str, dict[str, Any]] = {}
    for name, entry in servers_raw.items():
        if not isinstance(name, str) or not name.strip():
            raise ValueError("MCP server names must be non-empty strings")
        if not isinstance(entry, dict):
            raise ValueError(f"MCP server '{name}' config must be a JSON object")

        raw_transport = entry.get("transport") if "transport" in entry else entry.get("type")
        explicit_transport = _normalize_transport(raw_transport)

        command = entry.get("command")
        url = entry.get("url")
        has_command = command is not None and command != ""
        has_url = url is not None and url != ""

        if has_command and has_url:
            raise ValueError(f"MCP server '{name}' cannot specify both 'command' and 'url'")
        if not has_command and not has_url:
            raise ValueError(f"MCP server '{name}' must specify either 'command' or 'url'")

        if has_command:
            if not isinstance(command, str) or not command.strip():
                raise ValueError(f"MCP server '{name}' field 'command' must be a non-empty string")
            if explicit_transport is not None and explicit_transport != "stdio":
                raise ValueError(
                    f"MCP server '{name}' specifies 'command' with conflicting transport '{explicit_transport}'"
                )
            transport = "stdio"
            args = _validate_str_list(entry["args"], "args", name) if "args" in entry and entry["args"] is not None else []
            env = _validate_str_dict(entry["env"], "env", name) if "env" in entry and entry["env"] is not None else {}
            cwd_val = entry.get("cwd")
            if cwd_val is not None and not isinstance(cwd_val, str):
                raise ValueError(f"MCP server '{name}' field 'cwd' must be a string")
            cwd = cwd_val or None
            headers: dict[str, str] = {}
            norm_url: str = ""
            norm_command: str = command
        else:
            if not isinstance(url, str) or not url.strip():
                raise ValueError(f"MCP server '{name}' field 'url' must be a non-empty string")
            parsed_url = urlparse(url)
            if parsed_url.scheme.lower() not in ("http", "https") or not parsed_url.netloc:
                raise ValueError(f"MCP server '{name}' has invalid HTTP(S) URL: {url!r}")
            if explicit_transport == "stdio":
                raise ValueError(
                    f"MCP server '{name}' specifies 'url' with conflicting transport 'stdio'"
                )
            if explicit_transport in ("http", "sse"):
                transport = explicit_transport
            else:
                transport = "sse" if parsed_url.path.rstrip("/").lower().endswith("/sse") else "http"
            headers = (
                _validate_str_dict(entry["headers"], "headers", name)
                if "headers" in entry and entry["headers"] is not None
                else {}
            )
            args = []
            env = {}
            cwd = None
            norm_command = ""
            norm_url = url

        prev = existing_map.get(name, {})

        if "enabled" in entry:
            if not isinstance(entry["enabled"], bool):
                raise ValueError(f"MCP server '{name}' field 'enabled' must be a boolean")
            enabled = entry["enabled"]
        else:
            enabled = bool(prev.get("enabled", True))

        if "require_confirmation" in entry:
            if not isinstance(entry["require_confirmation"], bool):
                raise ValueError(
                    f"MCP server '{name}' field 'require_confirmation' must be a boolean"
                )
            require_confirmation = entry["require_confirmation"]
        else:
            require_confirmation = bool(prev.get("require_confirmation", False))

        if "disabled_tools" in entry:
            disabled_tools = (
                _validate_str_list(entry["disabled_tools"], "disabled_tools", name)
                if entry["disabled_tools"] is not None
                else []
            )
        else:
            disabled_tools = list(prev.get("disabled_tools") or [])

        result[name] = {
            "transport": transport,
            "command": norm_command,
            "args": args,
            "env": env,
            "cwd": cwd,
            "url": norm_url,
            "headers": headers,
            "enabled": enabled,
            "require_confirmation": require_confirmation,
            "disabled_tools": disabled_tools,
        }

    return result
