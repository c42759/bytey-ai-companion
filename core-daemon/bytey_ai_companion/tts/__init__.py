"""Sentence-chunked streaming TTS providers and PipeWire audio player."""

from .chunker import SentenceChunker
from .piper_provider import (
    CURATED_PIPER_VOICES,
    PIPER_VOICE_PRESETS,
    PiperTTSClient,
    apply_pitch_shift_wav,
)
from .player import StreamingTTSPlayer
from .providers import (
    DEFAULT_KOKORO_VOICES,
    ElevenLabsTTSClient,
    KokoroTTSClient,
)

__all__ = [
    "CURATED_PIPER_VOICES",
    "DEFAULT_KOKORO_VOICES",
    "ElevenLabsTTSClient",
    "KokoroTTSClient",
    "PIPER_VOICE_PRESETS",
    "PiperTTSClient",
    "SentenceChunker",
    "StreamingTTSPlayer",
    "apply_pitch_shift_wav",
]
