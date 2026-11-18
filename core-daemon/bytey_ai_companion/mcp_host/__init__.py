"""MCP Client Host and `mcpServers` schema synchronization package."""

from .manager import MCPClientManager, MCPProtocolError
from .schema_sync import from_mcp_servers_json, to_mcp_servers_json

__all__ = [
    "MCPClientManager",
    "MCPProtocolError",
    "from_mcp_servers_json",
    "to_mcp_servers_json",
]
