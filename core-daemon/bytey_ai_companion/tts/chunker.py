"""Streaming sentence boundary chunker for low-latency TTS synthesis."""

from __future__ import annotations

import re

_ABBREVIATIONS = frozenset(
    {
        "dr",
        "mr",
        "mrs",
        "ms",
        "prof",
        "sr",
        "jr",
        "vs",
        "etc",
        "eg",
        "ie",
        "e.g",
        "i.e",
        "st",
        "ave",
        "inc",
        "ltd",
        "fig",
        "vol",
        "no",
        "approx",
        "dept",
        "est",
    }
)

_BOUNDARY_RE = re.compile(r"([.!?])(?=\s)|(\n)")
_FENCE_MARKERS = ("```", "'''")


class SentenceChunker:
    """Buffers streamed LLM tokens and emits complete speakable sentences."""

    def __init__(self, min_chars: int = 12) -> None:
        self.min_chars = max(1, int(min_chars))
        self._buffer = ""
        self._in_code_block = False
        self._fence_marker: str | None = None

    def reset(self) -> None:
        """Clear any buffered partial sentence and code-block state."""
        self._buffer = ""
        self._in_code_block = False
        self._fence_marker = None

    @staticmethod
    def _is_non_breaking_period(text: str, dot_idx: int) -> bool:
        """Return True if the period at dot_idx belongs to an abbreviation, decimal, or URL."""
        # Extract the whitespace-delimited token ending at (or containing) dot_idx.
        token_start = dot_idx
        while token_start > 0 and not text[token_start - 1].isspace():
            token_start -= 1
        token = text[token_start : dot_idx + 1]
        token_lower = token.lower()

        # Check URLs or paths (http://..., https://..., example.com/foo).
        if "://" in token_lower or token_lower.startswith("www.") or "/" in token_lower:
            # Allow ending a sentence right after a bare URL only if it ends with a TLD?
            # Keeping URL tokens unbroken avoids reading URLs mid-token.
            if "/" in token_lower and not token_lower.endswith("/"):
                return True

        # Strip leading quotes/parens from token for abbreviation & version checks.
        core = token_lower.lstrip("\"'([{")
        if core.rstrip(".") in _ABBREVIATIONS:
            return True

        # Dotted acronyms like e.g., i.e., U.S., a.m., p.m.
        if re.fullmatch(r"(?:[a-z]\.){2,}", core):
            return True

        # Decimal or version numbers ending in digits before dot (e.g. "v1.2." or "3.").
        before_dot = core[:-1]
        if before_dot.isdigit() or re.fullmatch(r"v?\d+(?:\.\d+)+", before_dot):
            return True

        return False

    @staticmethod
    def _find_earliest_fence(text: str) -> tuple[int, str] | None:
        """Return (index, marker) of the earliest triple-quote or triple-backtick fence."""
        earliest: tuple[int, str] | None = None
        for marker in _FENCE_MARKERS:
            idx = text.find(marker)
            if idx != -1 and (earliest is None or idx < earliest[0]):
                earliest = (idx, marker)
        return earliest

    def _drain_sentences(self, text: str) -> tuple[list[str], str]:
        """Extract complete sentences from non-fenced text, returning (emitted, remainder)."""
        emitted: list[str] = []
        buf = text
        search_pos = 0
        while True:
            match = _BOUNDARY_RE.search(buf, search_pos)
            if match is None:
                break

            punct = match.group(1)
            newline = match.group(2)
            idx = match.start()

            if punct == "." and self._is_non_breaking_period(buf, idx):
                search_pos = match.end()
                continue

            end_idx = idx if newline is not None else match.end()
            candidate = buf[:end_idx].strip()
            if len(candidate) < self.min_chars:
                search_pos = match.end()
                continue

            emitted.append(candidate)
            rest_start = match.end()
            while rest_start < len(buf) and buf[rest_start].isspace():
                rest_start += 1
            buf = buf[rest_start:]
            search_pos = 0

        return emitted, buf

    def feed(self, token: str) -> list[str]:
        """Feed a streamed token and return zero or more complete sentences."""
        if not token:
            return []
        self._buffer += token
        emitted: list[str] = []

        while self._buffer:
            if self._in_code_block:
                assert self._fence_marker is not None
                close_idx = self._buffer.find(self._fence_marker)
                if close_idx == -1:
                    # Keep last 2 chars in case a closing fence is split across tokens.
                    if len(self._buffer) > 2:
                        self._buffer = self._buffer[-2:]
                    break
                self._buffer = self._buffer[close_idx + 3 :]
                self._in_code_block = False
                self._fence_marker = None
                continue

            fence = self._find_earliest_fence(self._buffer)
            if fence is not None:
                fence_idx, marker = fence
                prose_before = self._buffer[:fence_idx]
                sentences, tail = self._drain_sentences(prose_before)
                emitted.extend(sentences)
                tail_clean = tail.strip()
                if tail_clean:
                    emitted.append(tail_clean)
                self._buffer = self._buffer[fence_idx + 3 :]
                self._in_code_block = True
                self._fence_marker = marker
                continue

            sentences, remainder = self._drain_sentences(self._buffer)
            emitted.extend(sentences)
            self._buffer = remainder
            break

        return emitted

    def flush(self) -> str | None:
        """Flush and return any remaining buffered text at end of stream."""
        if self._in_code_block:
            self._buffer = ""
            self._in_code_block = False
            self._fence_marker = None
            return None
        remaining = self._buffer.strip()
        self._buffer = ""
        return remaining if remaining else None
