"""External AI CLI agent detection, registry, and non-interactive runner."""

from .registry import AgentInfo, AgentRegistry, CLIAgentSpec
from .runner import AgentRunResult, AgentRunner

__all__ = [
    "AgentInfo",
    "AgentRegistry",
    "AgentRunResult",
    "AgentRunner",
    "CLIAgentSpec",
]
