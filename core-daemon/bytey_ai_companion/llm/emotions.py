"""Emotion tag constants, prompt defaults, and streaming prefix parser."""

from __future__ import annotations

import asyncio
import inspect
import logging
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

VALID_EMOTIONS: tuple[str, ...] = (
    "happy",
    "curious",
    "surprised",
    "sleepy",
    "error",
    "neutral",
    "annoyed",
    "love",
    "focus",
    "astonished",
)

_DEFAULT_SYSTEM_PROMPT_PATH = Path(__file__).with_name("default_system_prompt.md")


def load_default_system_prompt() -> str:
    """Load the default system prompt from the packaged Markdown file."""
    return _DEFAULT_SYSTEM_PROMPT_PATH.read_text(encoding="utf-8").strip()


DEFAULT_SYSTEM_PROMPT: str = load_default_system_prompt()

_EMOTION_PREFIX_RE = re.compile(
    r"^\s*\[emotion:\s*([a-zA-Z0-9_-]+)\s*\]\s*",
    re.IGNORECASE,
)
_TITLE_PREFIX_RE = re.compile(
    r"^\s*\[title:\s*([^\]\r\n]+?)\s*\]\s*",
    re.IGNORECASE,
)
_TAG_PREFIXES: tuple[str, ...] = ("[emotion:", "[title:")
_MAX_TAG_BUFFER_CHARS = 128
_MAX_LEADING_WS_CHARS = 64
_MAX_TITLE_LEN = 80


def _clean_title(raw: str) -> str | None:
    """Normalize a captured title to a single line <= 80 chars."""
    cleaned = " ".join((raw or "").split())
    if not cleaned:
        return None
    if len(cleaned) > _MAX_TITLE_LEN:
        cleaned = cleaned[:_MAX_TITLE_LEN].rstrip()
    return cleaned or None


def parse_reply_tags(text: str) -> tuple[str | None, str | None, str]:
    """Parse leading `[emotion:<tag>]` and `[title:<text>]` prefixes in either order."""
    if not text:
        return None, None, ""
    emotion: str | None = None
    title: str | None = None
    rest = text

    for _ in range(2):
        matched = False
        if emotion is None:
            em_match = _EMOTION_PREFIX_RE.match(rest)
            if em_match:
                tag = em_match.group(1).strip().lower()
                if tag in VALID_EMOTIONS:
                    emotion = tag
                    rest = rest[em_match.end():]
                    matched = True
        if not matched and title is None:
            ti_match = _TITLE_PREFIX_RE.match(rest)
            if ti_match:
                title = _clean_title(ti_match.group(1))
                rest = rest[ti_match.end():]
                matched = True
        if not matched:
            break

    return emotion, title, rest


def parse_emotion_prefix(text: str) -> tuple[str | None, str]:
    """Parse leading `[emotion:<tag>]` (and strip any `[title:...]` prefix) and trailing whitespace."""
    emotion, _title, clean_text = parse_reply_tags(text)
    return emotion, clean_text


class EmotionStreamParser:
    """Incrementally detects and strips leading `[emotion:<tag>]` and `[title:<text>]` markers."""

    def __init__(
        self,
        on_emotion: Callable[[str], Any] | None = None,
        on_title: Callable[[str], Any] | None = None,
    ) -> None:
        self._on_emotion = on_emotion
        self._on_title = on_title
        self._buffer: str = ""
        self._resolved: bool = False
        self._strip_leading_ws: bool = False
        self._any_tag_consumed: bool = False
        self._callback_fired: bool = False
        self._title_callback_fired: bool = False
        self._pending_tasks: list[asyncio.Task[Any]] = []
        self.emotion: str | None = None
        self.captured_title: str | None = None

    def _schedule_callback(self, cb: Callable[[str], Any] | None, value: str, label: str) -> None:
        if cb is None:
            return
        result = cb(value)
        if inspect.isawaitable(result):
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                asyncio.run(result)
                return
            task = loop.create_task(result)
            task.add_done_callback(lambda t: self._on_task_done(t, label))
            self._pending_tasks.append(task)

    def _fire_emotion(self, tag: str) -> None:
        if self._callback_fired:
            return
        self._callback_fired = True
        self._schedule_callback(self._on_emotion, tag, "on_emotion")

    def _fire_title(self, title: str) -> None:
        if self._title_callback_fired:
            return
        self._title_callback_fired = True
        self._schedule_callback(self._on_title, title, "on_title")

    @staticmethod
    def _on_task_done(task: asyncio.Task[Any], label: str = "on_emotion") -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.warning("Async %s callback failed: %s", label, exc)

    async def wait_callbacks(self) -> None:
        """Await any scheduled async callback tasks."""
        if not self._pending_tasks:
            return
        tasks = list(self._pending_tasks)
        self._pending_tasks.clear()
        await asyncio.gather(*tasks, return_exceptions=True)

    def _try_consume_one_tag(self) -> bool:
        """Attempt to consume one leading `[emotion:...]` or `[title:...]` tag from `_buffer`."""
        if self.emotion is None:
            em_match = _EMOTION_PREFIX_RE.match(self._buffer)
            if em_match:
                tag = em_match.group(1).strip().lower()
                if tag in VALID_EMOTIONS:
                    self.emotion = tag
                    self._buffer = self._buffer[em_match.end():]
                    self._any_tag_consumed = True
                    self._strip_leading_ws = True
                    self._fire_emotion(tag)
                    return True
        if self.captured_title is None:
            ti_match = _TITLE_PREFIX_RE.match(self._buffer)
            if ti_match:
                title = _clean_title(ti_match.group(1))
                self._buffer = self._buffer[ti_match.end():]
                self._any_tag_consumed = True
                self._strip_leading_ws = True
                if title:
                    self.captured_title = title
                    self._fire_title(title)
                return True
        return False

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

        while not self._resolved:
            stripped = self._buffer.lstrip()
            if not stripped:
                if len(self._buffer) > _MAX_LEADING_WS_CHARS:
                    return self._release_raw_buffer()
                return ""

            lowered = stripped.lower()
            matching_prefix = any(
                p.startswith(lowered) if len(lowered) < len(p) else lowered.startswith(p)
                for p in _TAG_PREFIXES
            )
            if not matching_prefix:
                return self._release_raw_buffer()

            if len(lowered) < min(len(p) for p in _TAG_PREFIXES) or any(
                len(lowered) < len(p) and p.startswith(lowered) for p in _TAG_PREFIXES
            ):
                if "\n" in stripped:
                    return self._release_raw_buffer()
                return ""

            if "]" not in stripped:
                if "\n" in stripped or len(stripped) > _MAX_TAG_BUFFER_CHARS:
                    return self._release_raw_buffer()
                return ""

            if self._try_consume_one_tag():
                if self.emotion is not None and self.captured_title is not None:
                    self._resolved = True
                    out = self._buffer.lstrip() if self._strip_leading_ws else self._buffer
                    self._buffer = ""
                    if out:
                        self._strip_leading_ws = False
                    return out
                continue

            return self._release_raw_buffer()

        return ""

    def _release_raw_buffer(self) -> str:
        self._resolved = True
        out = self._buffer
        self._buffer = ""
        if self._any_tag_consumed and self._strip_leading_ws:
            out = out.lstrip()
            if out:
                self._strip_leading_ws = False
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


EmotionStreamFilter = EmotionStreamParser
