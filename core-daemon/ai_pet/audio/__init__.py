"""Voice input, VAD, wake-word matching, and STT pipeline for AI Pet."""

from .capture import VoiceInputService
from .stt import STTEngine, pcm_to_wav_bytes
from .vad import VADProcessor
from .wakeword import WakeWordMatcher

__all__ = [
    "STTEngine",
    "VADProcessor",
    "VoiceInputService",
    "WakeWordMatcher",
    "pcm_to_wav_bytes",
]
