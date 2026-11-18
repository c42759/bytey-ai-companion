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


class SentenceChunker:
    """Buffers streamed LLM tokens and emits complete speakable sentences."""

    def __init__(self, min_chars: int = 12) -> None:
        self.min_chars = max(1, int(min_chars))
        self._buffer = ""

    def reset(self) -> None:
        """Clear any buffered partial sentence."""
        self._buffer = ""

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

    def feed(self, token: str) -> list[str]:
        """Feed a streamed token and return zero or more complete sentences."""
        if not token:
            return []
        self._buffer += token
        emitted: list[str] = []

        search_pos = 0
        while True:
            match = _BOUNDARY_RE.search(self._buffer, search_pos)
            if match is None:
                break

            punct = match.group(1)
            newline = match.group(2)
            idx = match.start()

            if punct == "." and self._is_non_breaking_period(self._buffer, idx):
                search_pos = match.end()
                continue

            end_idx = idx if newline is not None else match.end()
            candidate = self._buffer[:end_idx].strip()
            if len(candidate) < self.min_chars:
                search_pos = match.end()
                continue

            emitted.append(candidate)
            rest_start = match.end()
            while rest_start < len(self._buffer) and self._buffer[rest_start].isspace():
                rest_start += 1
            self._buffer = self._buffer[rest_start:]
            search_pos = 0

        return emitted

    def flush(self) -> str | None:
        """Flush and return any remaining buffered text at end of stream."""
        remaining = self._buffer.strip()
        self._buffer = ""
        return remaining if remaining else None
