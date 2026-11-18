"""Open-vocabulary wake-word matcher with phonetic and fuzzy text similarity."""

from __future__ import annotations

from difflib import SequenceMatcher
import re

try:
    from rapidfuzz import fuzz as _rapidfuzz_fuzz  # type: ignore[import-not-found]
except ImportError:
    _rapidfuzz_fuzz = None  # type: ignore[assignment]

_FILLER_TOKENS = frozenset(
    {"uh", "um", "ah", "er", "erm", "hmm", "oh", "so", "well", "please", "actually"}
)


def _ratio(a: str, b: str) -> float:
    """Return normalized similarity score in [0.0, 1.0]."""
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    if _rapidfuzz_fuzz is not None:
        return float(_rapidfuzz_fuzz.ratio(a, b)) / 100.0
    return SequenceMatcher(None, a, b).ratio()


class WakeWordMatcher:
    """Matches arbitrary user-configured wake phrases using phonetic + fuzzy text similarity."""

    @staticmethod
    def normalize(text: str) -> str:
        """Lowercase, strip punctuation, and collapse whitespace."""
        cleaned = re.sub(r"['’]", "", text.lower())
        cleaned = re.sub(r"[^\w\s]", " ", cleaned)
        return " ".join(cleaned.split())

    @staticmethod
    def phonetic_key(word: str) -> str:
        """Compute a lightweight Metaphone-like phonetic key for English tokens."""
        w = re.sub(r"[^a-z]", "", word.lower())
        if not w:
            return ""

        # Collapse consecutive identical letters first (e.g. "pett" -> "pet").
        deduped: list[str] = []
        for ch in w:
            if not deduped or deduped[-1] != ch:
                deduped.append(ch)
        w = "".join(deduped)

        # Silent leading pairs.
        for prefix, repl in (
            ("kn", "n"),
            ("gn", "n"),
            ("pn", "n"),
            ("wr", "r"),
            ("wh", "w"),
            ("ps", "s"),
        ):
            if w.startswith(prefix):
                w = repl + w[len(prefix) :]
                break

        out: list[str] = []
        i = 0
        n = len(w)
        while i < n:
            ch = w[i]
            nxt = w[i + 1] if i + 1 < n else ""
            is_start = i == 0

            if ch == "p" and nxt == "h":
                out.append("F")
                i += 2
                continue
            if ch == "c" and nxt == "k":
                out.append("K")
                i += 2
                continue
            if ch == "q" and nxt == "u":
                out.append("KW")
                i += 2
                continue
            if ch == "s" and nxt == "c" and i + 2 < n and w[i + 2] == "h":
                out.append("X")
                i += 3
                continue
            if ch in ("s", "c") and nxt == "h":
                out.append("X")
                i += 2
                continue
            if ch == "t" and nxt == "h":
                out.append("0")
                i += 2
                continue
            if ch == "g" and nxt == "h":
                if is_start:
                    out.append("G")
                i += 2
                continue

            if ch in ("a", "e", "i", "o", "u", "y"):
                if is_start:
                    out.append(ch.upper())
            elif ch == "h":
                if is_start:
                    out.append("H")
            elif ch == "c":
                out.append("S" if nxt in ("e", "i", "y") else "K")
            elif ch == "g":
                out.append("J" if nxt in ("e", "i", "y") else "G")
            elif ch == "q":
                out.append("K")
            elif ch == "x":
                out.append("KS")
            elif ch == "z":
                out.append("S")
            else:
                out.append(ch.upper())
            i += 1

        # Collapse adjacent identical phonetic symbols.
        result: list[str] = []
        for sym in "".join(out):
            if not result or result[-1] != sym:
                result.append(sym)
        return "".join(result)

    @classmethod
    def _score_phrase(cls, cand_tokens: list[str], wake_tokens: list[str]) -> float:
        cand_str = " ".join(cand_tokens)
        wake_str = " ".join(wake_tokens)
        text_sim = max(
            _ratio(cand_str, wake_str),
            _ratio(cand_str.replace(" ", ""), wake_str.replace(" ", "")),
        )

        cand_phon = " ".join(cls.phonetic_key(t) for t in cand_tokens)
        wake_phon = " ".join(cls.phonetic_key(t) for t in wake_tokens)
        phon_sim = max(
            _ratio(cand_phon, wake_phon),
            _ratio(cand_phon.replace(" ", ""), wake_phon.replace(" ", "")),
        )

        return 0.5 * text_sim + 0.5 * phon_sim

    @classmethod
    def match_wake_phrase(
        cls,
        transcript: str,
        wake_phrase: str,
        sensitivity: float = 0.72,
    ) -> tuple[bool, str]:
        """Match wake phrase at transcript start (or after brief filler) and return remainder."""
        wake_norm = cls.normalize(wake_phrase)
        if not transcript or not wake_norm:
            return False, ""
        wake_tokens = wake_norm.split()
        w_len = len(wake_tokens)

        raw_spans: list[tuple[int, int, str]] = []
        for match in re.finditer(r"[\w'’]+", transcript):
            norm_tok = cls.normalize(match.group(0))
            if norm_tok:
                raw_spans.append((match.start(), match.end(), norm_tok))

        if not raw_spans:
            return False, ""

        # Allow wake phrase at index 0, or after at most 2 brief filler tokens.
        max_start = min(3, len(raw_spans))
        best_score = 0.0
        best_end_char = 0

        for start_idx in range(max_start):
            if start_idx > 0:
                prefix = [raw_spans[k][2] for k in range(start_idx)]
                if not all(tok in _FILLER_TOKENS or len(tok) <= 2 for tok in prefix):
                    break

            remaining_tokens = len(raw_spans) - start_idx
            span_lengths = {
                length
                for length in (max(1, w_len - 1), w_len, min(remaining_tokens, w_len + 1))
                if 1 <= length <= remaining_tokens
            }
            for span_len in sorted(span_lengths):
                cand_tokens = [raw_spans[start_idx + k][2] for k in range(span_len)]
                score = cls._score_phrase(cand_tokens, wake_tokens)
                if score > best_score:
                    best_score = score
                    best_end_char = raw_spans[start_idx + span_len - 1][1]

        if best_score >= sensitivity:
            remainder = transcript[best_end_char:].lstrip(" \t\r\n,.:;!?-–—")
            return True, remainder
        return False, ""
