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

EMOTION_ALIASES: dict[str, str] = {
    "sorry": "neutral",
    "apologetic": "neutral",
    "sad": "neutral",
    "calm": "neutral",
    "excited": "happy",
    "joy": "happy",
    "glad": "happy",
    "cheerful": "happy",
    "thinking": "focus",
    "thoughtful": "focus",
    "focused": "focus",
    "concentrating": "focus",
    "angry": "annoyed",
    "frustrated": "annoyed",
    "irritated": "annoyed",
    "confused": "curious",
    "wondering": "curious",
    "inquisitive": "curious",
    "amazed": "astonished",
    "shocked": "astonished",
    "stunned": "astonished",
    "affectionate": "love",
    "caring": "love",
    "warm": "love",
    "tired": "sleepy",
    "drowsy": "sleepy",
    "failed": "error",
    "broken": "error",
}


def normalize_emotion_tag(raw_tag: str) -> str | None:
    """Normalize a parsed emotion tag against VALID_EMOTIONS and EMOTION_ALIASES."""
    tag = (raw_tag or "").strip().lower()
    if tag in VALID_EMOTIONS:
        return tag
    return EMOTION_ALIASES.get(tag)

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
_EMOTION_AT_START_RE = re.compile(
    r"^\[emotion:\s*([a-zA-Z0-9_-]+)\s*\]",
    re.IGNORECASE,
)
_TITLE_AT_START_RE = re.compile(
    r"^\[title:\s*([^\]\r\n]+?)\s*\]",
    re.IGNORECASE,
)
_EMOTION_INLINE_RE = re.compile(
    r"[ \t]*\[emotion:\s*([a-zA-Z0-9_-]+)\s*\][ \t]*",
    re.IGNORECASE,
)
_TITLE_INLINE_RE = re.compile(
    r"[ \t]*\[title:\s*([^\]\r\n]+?)\s*\][ \t]*",
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


def _strip_inline_tag(text: str, pattern: re.Pattern[str], on_match: Callable[[str], None]) -> str:
    """Strip inline tag occurrences while preserving surrounding spacing cleanly."""
    parts: list[str] = []
    pos = 0
    for m in pattern.finditer(text):
        on_match(m.group(1))
        before = text[pos:m.start()]
        after = text[m.end():]
        parts.append(before)
        prev_char = text[m.start() - 1] if m.start() > 0 else ""
        next_char = after[0] if after else ""
        if (
            prev_char
            and next_char
            and prev_char not in "\n\r"
            and next_char not in "\n\r"
        ):
            parts.append(" ")
        pos = m.end()
    parts.append(text[pos:])
    return "".join(parts)


def parse_reply_tags(text: str) -> tuple[str | None, str | None, str]:
    """Parse `[emotion:<tag>]` and `[title:<text>]` prefixes or inline markers in either order."""
    if not text:
        return None, None, ""
    emotion: str | None = None
    title: str | None = None
    rest = text

    for _ in range(2):
        matched = False
        em_match = _EMOTION_PREFIX_RE.match(rest)
        if em_match:
            norm = normalize_emotion_tag(em_match.group(1))
            if emotion is None and norm is not None:
                emotion = norm
            rest = rest[em_match.end():]
            matched = True
        if not matched:
            ti_match = _TITLE_PREFIX_RE.match(rest)
            if ti_match:
                if title is None:
                    title = _clean_title(ti_match.group(1))
                rest = rest[ti_match.end():]
                matched = True
        if not matched:
            break

    def _capture_emotion(raw_tag: str) -> None:
        nonlocal emotion
        if emotion is None:
            norm = normalize_emotion_tag(raw_tag)
            if norm is not None:
                emotion = norm

    def _capture_title(raw_title: str) -> None:
        nonlocal title
        if title is None:
            title = _clean_title(raw_title)

    rest = _strip_inline_tag(rest, _EMOTION_INLINE_RE, _capture_emotion)
    rest = _strip_inline_tag(rest, _TITLE_INLINE_RE, _capture_title)
    return emotion, title, rest


def parse_emotion_prefix(text: str) -> tuple[str | None, str]:
    """Parse `[emotion:<tag>]` (and strip any `[title:...]` markers) and trailing whitespace."""
    emotion, _title, clean_text = parse_reply_tags(text)
    return emotion, clean_text


class EmotionStreamParser:
    """Incrementally detects and strips `[emotion:<tag>]` and `[title:<text>]` markers anywhere in stream."""

    def __init__(
        self,
        on_emotion: Callable[[str], Any] | None = None,
        on_title: Callable[[str], Any] | None = None,
    ) -> None:
        self._on_emotion = on_emotion
        self._on_title = on_title
        self._buffer: str = ""
        self._pending_leading_ws: str = ""
        self._has_emitted: bool = False
        self._strip_leading_ws: bool = False
        self._strip_post_tag_ws: bool = False
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
        """Attempt to consume one `[emotion:...]` or `[title:...]` tag at index 0 of `_buffer`."""
        em_match = _EMOTION_AT_START_RE.match(self._buffer)
        if em_match:
            norm = normalize_emotion_tag(em_match.group(1))
            self._buffer = self._buffer[em_match.end():]
            if not self._has_emitted:
                self._strip_leading_ws = True
            else:
                self._strip_post_tag_ws = True
                self._buffer = self._buffer.lstrip(" \t")
                if self._buffer:
                    self._strip_post_tag_ws = False
            if norm is not None and self.emotion is None:
                self.emotion = norm
                self._fire_emotion(norm)
            return True

        ti_match = _TITLE_AT_START_RE.match(self._buffer)
        if ti_match:
            title = _clean_title(ti_match.group(1))
            self._buffer = self._buffer[ti_match.end():]
            if not self._has_emitted:
                self._strip_leading_ws = True
            else:
                self._strip_post_tag_ws = True
                self._buffer = self._buffer.lstrip(" \t")
                if self._buffer:
                    self._strip_post_tag_ws = False
            if title and self.captured_title is None:
                self.captured_title = title
                self._fire_title(title)
            return True

        return False

    def feed(self, chunk: str) -> str:
        """Feed a streamed token chunk and return clean text safe to emit downstream."""
        if not chunk:
            return ""

        self._buffer += chunk
        out_parts: list[str] = []

        while self._buffer:
            if not self._has_emitted and self._strip_leading_ws:
                self._buffer = self._buffer.lstrip()
                if not self._buffer:
                    break
            elif self._strip_post_tag_ws:
                self._buffer = self._buffer.lstrip(" \t")
                if not self._buffer:
                    break
                self._strip_post_tag_ws = False

            idx = self._buffer.find("[")
            if idx == -1:
                if not self._has_emitted and not self._buffer.strip():
                    if len(self._pending_leading_ws) + len(self._buffer) <= _MAX_LEADING_WS_CHARS:
                        self._pending_leading_ws += self._buffer
                        self._buffer = ""
                        break
                text_out = (self._pending_leading_ws if not self._has_emitted else "") + self._buffer
                self._pending_leading_ws = ""
                self._buffer = ""
                if text_out:
                    self._has_emitted = True
                    self._strip_leading_ws = False
                    out_parts.append(text_out)
                break

            if idx > 0:
                prefix = self._buffer[:idx]
                self._buffer = self._buffer[idx:]
                if (
                    not self._has_emitted
                    and not prefix.strip()
                    and len(self._pending_leading_ws) + len(prefix) <= _MAX_LEADING_WS_CHARS
                ):
                    self._pending_leading_ws += prefix
                else:
                    text_out = (self._pending_leading_ws if not self._has_emitted else "") + prefix
                    self._pending_leading_ws = ""
                    if text_out:
                        self._has_emitted = True
                        self._strip_leading_ws = False
                        out_parts.append(text_out)
                continue

            # self._buffer[0] == '['
            lowered = self._buffer.lower()
            could_be_tag = any(
                p.startswith(lowered) if len(lowered) < len(p) else lowered.startswith(p)
                for p in _TAG_PREFIXES
            )
            if not could_be_tag:
                text_out = (self._pending_leading_ws if not self._has_emitted else "") + "["
                self._pending_leading_ws = ""
                self._buffer = self._buffer[1:]
                self._has_emitted = True
                self._strip_leading_ws = False
                out_parts.append(text_out)
                continue

            if "]" not in self._buffer:
                if "\n" in self._buffer or len(self._buffer) > _MAX_TAG_BUFFER_CHARS:
                    text_out = (self._pending_leading_ws if not self._has_emitted else "") + "["
                    self._pending_leading_ws = ""
                    self._buffer = self._buffer[1:]
                    self._has_emitted = True
                    self._strip_leading_ws = False
                    out_parts.append(text_out)
                    continue
                break

            if self._try_consume_one_tag():
                self._pending_leading_ws = ""
                continue

            text_out = (self._pending_leading_ws if not self._has_emitted else "") + "["
            self._pending_leading_ws = ""
            self._buffer = self._buffer[1:]
            self._has_emitted = True
            self._strip_leading_ws = False
            out_parts.append(text_out)

        return "".join(out_parts)

    def flush(self) -> str:
        """Flush any remaining buffered characters at end-of-stream."""
        out = (self._pending_leading_ws if not self._has_emitted else "") + self._buffer
        self._pending_leading_ws = ""
        self._buffer = ""
        if not self._has_emitted and self._strip_leading_ws:
            out = out.lstrip()
        elif self._strip_post_tag_ws:
            out = out.lstrip(" \t")
        self._strip_post_tag_ws = False
        if out:
            self._has_emitted = True
            self._strip_leading_ws = False
        return out


EmotionStreamFilter = EmotionStreamParser
