"""Emotion tag constants, prompt defaults, and streaming prefix parser."""

from __future__ import annotations

import asyncio
import inspect
import logging
import re
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

VALID_EMOTIONS: tuple[str, ...] = (
    "happy",
    "curious",
    "surprised",
    "sleepy",
    "error",
    "neutral",
)

DEFAULT_SYSTEM_PROMPT: str = (
    "You are Bytey, a fast, warm, and capable desktop AI companion.\n"
    "You are both a friendly digital pet and a sharp, practical assistant.\n"
    "\n"
    "Personality:\n"
    "- Warm, slightly playful, and supportive — like a clever pet that actually helps.\n"
    "- Speak naturally, as if talking out loud to a friend.\n"
    "- Stay concise (1–3 sentences) unless the user asks for more detail.\n"
    "\n"
    "Core behavior:\n"
    "- Always begin every reply with exactly one mood tag: [emotion:happy], [emotion:curious], [emotion:surprised], [emotion:sleepy], [emotion:error], or [emotion:neutral].\n"
    "- Prefer clear, spoken-friendly language. Avoid markdown, tables, bullet lists, code blocks, or any visual clutter in replies that will be spoken.\n"
    "- Be helpful and proactive. If you lack information that a tool can provide, use the tool instead of guessing or saying you don’t know.\n"
    "- When you use tools, call them cleanly and give a short, natural spoken summary of the result.\n"
    "- If something is outside your reach even with tools, say so simply and offer the next useful step.\n"
    "\n"
    "Stay in character as Bytey at all times."
)

_EMOTION_PREFIX_RE = re.compile(
    r"^\s*\[emotion:\s*([a-zA-Z0-9_-]+)\s*\]\s*",
    re.IGNORECASE,
)
_TAG_PREFIX = "[emotion:"
_MAX_TAG_BUFFER_CHARS = 48
_MAX_LEADING_WS_CHARS = 64


def parse_emotion_prefix(text: str) -> tuple[str | None, str]:
    """Parse a leading `[emotion:<tag>]` prefix (case-insensitive) and strip trailing whitespace."""
    if not text:
        return None, ""
    match = _EMOTION_PREFIX_RE.match(text)
    if not match:
        return None, text
    tag = match.group(1).strip().lower()
    if tag not in VALID_EMOTIONS:
        return None, text
    return tag, text[match.end():]


class EmotionStreamParser:
    """Incrementally detects and strips leading `[emotion:<tag>]` markers from streamed text."""

    def __init__(self, on_emotion: Callable[[str], Any] | None = None) -> None:
        self._on_emotion = on_emotion
        self._buffer: str = ""
        self._resolved: bool = False
        self._strip_leading_ws: bool = False
        self._callback_fired: bool = False
        self._pending_tasks: list[asyncio.Task[Any]] = []
        self.emotion: str | None = None

    def _fire_emotion(self, tag: str) -> None:
        if self._callback_fired:
            return
        self._callback_fired = True
        if self._on_emotion is None:
            return
        result = self._on_emotion(tag)
        if inspect.isawaitable(result):
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                asyncio.run(result)
                return
            task = loop.create_task(result)
            task.add_done_callback(self._on_task_done)
            self._pending_tasks.append(task)

    @staticmethod
    def _on_task_done(task: asyncio.Task[Any]) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.warning("Async on_emotion callback failed: %s", exc)

    async def wait_callbacks(self) -> None:
        """Await any scheduled async `on_emotion` callback tasks."""
        if not self._pending_tasks:
            return
        tasks = list(self._pending_tasks)
        self._pending_tasks.clear()
        await asyncio.gather(*tasks, return_exceptions=True)

    def feed(self, chunk: str) -> str:
        """Feed a streamed token chunk and return clean text safe to emit downstream."""
        if not chunk:
            return ""

        if self._resolved:
            if self._strip_leading_ws:
                cleaned = chunk.lstrip()
                if not cleaned:
                    return ""
                self._strip_leading_ws = False
                return cleaned
            return chunk

        self._buffer += chunk
        stripped = self._buffer.lstrip()

        if not stripped:
            if len(self._buffer) > _MAX_LEADING_WS_CHARS:
                return self._release_raw_buffer()
            return ""

        lowered = stripped.lower()
        if len(lowered) < len(_TAG_PREFIX):
            if _TAG_PREFIX.startswith(lowered):
                return ""
            return self._release_raw_buffer()

        if not lowered.startswith(_TAG_PREFIX):
            return self._release_raw_buffer()

        if "]" not in stripped:
            if "\n" in stripped or len(stripped) > _MAX_TAG_BUFFER_CHARS:
                return self._release_raw_buffer()
            return ""

        tag, rest = parse_emotion_prefix(self._buffer)
        if tag is not None:
            self.emotion = tag
            self._resolved = True
            self._buffer = ""
            self._fire_emotion(tag)
            if rest:
                self._strip_leading_ws = False
                return rest
            self._strip_leading_ws = True
            return ""

        return self._release_raw_buffer()

    def _release_raw_buffer(self) -> str:
        self._resolved = True
        out = self._buffer
        self._buffer = ""
        return out

    def flush(self) -> str:
        """Flush any remaining buffered characters at end-of-stream."""
        if not self._buffer:
            return ""
        if not self._resolved:
            return self._release_raw_buffer()
        out = self._buffer
        self._buffer = ""
        if self._strip_leading_ws:
            out = out.lstrip()
            if out:
                self._strip_leading_ws = False
        return out
