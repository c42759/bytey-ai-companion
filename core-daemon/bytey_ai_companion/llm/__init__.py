"""LLM provider router and emotion parsing package."""

from .emotions import (
    DEFAULT_SYSTEM_PROMPT,
    VALID_EMOTIONS,
    EmotionStreamParser,
    parse_emotion_prefix,
)
from .router import (
    ANTHROPIC_FALLBACK_MODELS,
    SUPPORTED_PROVIDERS,
    LLMRouter,
)

__all__ = [
    "ANTHROPIC_FALLBACK_MODELS",
    "DEFAULT_SYSTEM_PROMPT",
    "EmotionStreamParser",
    "LLMRouter",
    "SUPPORTED_PROVIDERS",
    "VALID_EMOTIONS",
    "parse_emotion_prefix",
]
