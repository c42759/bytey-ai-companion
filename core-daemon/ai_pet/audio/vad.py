"""Voice Activity Detection (VAD) processor with optional Silero VAD and numpy RMS fallback."""

from __future__ import annotations

from collections import deque
import logging
import math
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)

try:
    import torch  # type: ignore[import-not-found]
    from silero_vad import load_silero_vad  # type: ignore[import-not-found]

    _HAS_SILERO = True
except ImportError:
    torch = None  # type: ignore[assignment]
    load_silero_vad = None  # type: ignore[assignment]
    _HAS_SILERO = False


class VADProcessor:
    """Tracks rolling pre-roll audio, speech onset, and trailing silence timeouts."""

    def __init__(
        self,
        sample_rate: int = 16000,
        frame_ms: int = 30,
        vad_threshold: float = 0.015,
        silence_timeout_ms: int = 1300,
        pre_roll_ms: int = 1500,
        max_utterance_s: float = 30.0,
        onset_window_ms: int = 1200,
    ) -> None:
        self.sample_rate = sample_rate
        self.frame_ms = frame_ms
        self.vad_threshold = vad_threshold
        self.silence_timeout_ms = silence_timeout_ms
        self.pre_roll_ms = pre_roll_ms
        self.max_utterance_s = max_utterance_s
        self.onset_window_ms = onset_window_ms

        self.frame_samples = max(1, int(self.sample_rate * self.frame_ms / 1000))
        self.frame_bytes = self.frame_samples * 2
        pre_roll_frames = max(1, math.ceil(self.pre_roll_ms / max(1, self.frame_ms)))
        self._pre_roll: deque[bytes] = deque(maxlen=pre_roll_frames)

        self._utterance_frames: list[bytes] = []
        self._active: bool = False
        self._onset_emitted: bool = False
        self._manual_mode: bool = False
        self._post_onset_ms: float = 0.0
        self._utterance_ms: float = 0.0
        self._silence_ms: float = 0.0

        self._silero_model: Any = None
        self._silero_win = 512 if self.sample_rate >= 16000 else 256
        self._silero_buffer = np.zeros(0, dtype=np.float32)
        if _HAS_SILERO and load_silero_vad is not None:
            try:
                self._silero_model = load_silero_vad()
            except Exception as exc:  # pragma: no cover - environment dependent
                logger.warning("Failed to load silero_vad, falling back to RMS: %s", exc)
                self._silero_model = None

    @staticmethod
    def compute_rms(pcm_bytes: bytes) -> float:
        """Compute normalized RMS amplitude in [0.0, 1.0] from signed 16-bit PCM bytes."""
        usable_len = len(pcm_bytes) - (len(pcm_bytes) % 2)
        if usable_len < 2:
            return 0.0
        samples = np.frombuffer(pcm_bytes[:usable_len], dtype=np.int16).astype(np.float32) / 32768.0
        rms = float(np.sqrt(np.mean(samples * samples)))
        return min(1.0, max(0.0, rms))

    def _detect_speech(self, pcm_bytes: bytes, rms: float) -> bool:
        """Detect speech using Silero VAD when available, otherwise RMS threshold."""
        if self._silero_model is not None and torch is not None:
            usable_len = len(pcm_bytes) - (len(pcm_bytes) % 2)
            if usable_len >= 2:
                chunk = (
                    np.frombuffer(pcm_bytes[:usable_len], dtype=np.int16).astype(np.float32)
                    / 32768.0
                )
                self._silero_buffer = np.concatenate((self._silero_buffer, chunk))
                if len(self._silero_buffer) > self._silero_win * 2:
                    self._silero_buffer = self._silero_buffer[-self._silero_win :]
                if len(self._silero_buffer) >= self._silero_win:
                    window = self._silero_buffer[-self._silero_win :]
                    try:
                        tensor = torch.from_numpy(window)
                        prob = float(self._silero_model(tensor, self.sample_rate).item())
                        return prob >= 0.5
                    except Exception:
                        pass
        return rms >= self.vad_threshold

    def _reset_active(self) -> None:
        self._utterance_frames.clear()
        self._active = False
        self._onset_emitted = False
        self._manual_mode = False
        self._post_onset_ms = 0.0
        self._utterance_ms = 0.0
        self._silence_ms = 0.0

    def force_start_listening(self) -> None:
        """Immediately enter active listening mode (manual trigger / push-to-talk)."""
        self._manual_mode = True
        if not self._active:
            self._active = True
            self._utterance_frames = list(self._pre_roll)
            self._pre_roll.clear()
            self._post_onset_ms = 0.0
            self._utterance_ms = 0.0
            self._silence_ms = 0.0
            self._onset_emitted = False

    def force_stop_listening(self) -> bytes | None:
        """Force-end active listening and return buffered utterance PCM if present."""
        if not self._active or not self._utterance_frames:
            self._reset_active()
            return None
        audio = b"".join(self._utterance_frames)
        self._reset_active()
        return audio

    def reset(self) -> None:
        """Clear pre-roll ring buffer and reset all VAD state to idle."""
        self._pre_roll.clear()
        self._silero_buffer = np.zeros(0, dtype=np.float32)
        self._reset_active()

    def process_frame(self, pcm_bytes: bytes) -> dict[str, Any]:
        """Process one PCM frame and return VAD metrics, state, and emitted audio slices."""
        rms = self.compute_rms(pcm_bytes)
        is_speech = self._detect_speech(pcm_bytes, rms)
        num_samples = len(pcm_bytes) // 2
        frame_dur_ms = (
            (num_samples / self.sample_rate) * 1000.0
            if num_samples > 0
            else float(self.frame_ms)
        )

        if not self._active:
            if not is_speech:
                if pcm_bytes:
                    self._pre_roll.append(pcm_bytes)
                return {
                    "rms": rms,
                    "is_speech": False,
                    "state": "idle",
                    "onset_audio": None,
                    "utterance_audio": None,
                }

            self._active = True
            self._utterance_frames = list(self._pre_roll)
            if pcm_bytes:
                self._utterance_frames.append(pcm_bytes)
            self._pre_roll.clear()
            self._post_onset_ms = frame_dur_ms
            self._utterance_ms = frame_dur_ms
            self._silence_ms = 0.0
            self._onset_emitted = False

            if self._manual_mode or self._post_onset_ms >= self.onset_window_ms:
                self._onset_emitted = True
                return {
                    "rms": rms,
                    "is_speech": True,
                    "state": "onset",
                    "onset_audio": b"".join(self._utterance_frames),
                    "utterance_audio": None,
                }

            return {
                "rms": rms,
                "is_speech": True,
                "state": "speaking",
                "onset_audio": None,
                "utterance_audio": None,
            }

        if pcm_bytes:
            self._utterance_frames.append(pcm_bytes)
        self._utterance_ms += frame_dur_ms
        if is_speech:
            self._post_onset_ms += frame_dur_ms
            self._silence_ms = 0.0
        else:
            self._silence_ms += frame_dur_ms

        if (
            self._silence_ms >= self.silence_timeout_ms
            or self._utterance_ms >= self.max_utterance_s * 1000.0
        ):
            utterance_audio = b"".join(self._utterance_frames)
            self._reset_active()
            return {
                "rms": rms,
                "is_speech": is_speech,
                "state": "utterance_end",
                "onset_audio": None,
                "utterance_audio": utterance_audio,
            }

        if not self._onset_emitted and (
            self._manual_mode or self._post_onset_ms >= self.onset_window_ms
        ):
            self._onset_emitted = True
            return {
                "rms": rms,
                "is_speech": is_speech,
                "state": "onset",
                "onset_audio": b"".join(self._utterance_frames),
                "utterance_audio": None,
            }

        return {
            "rms": rms,
            "is_speech": is_speech,
            "state": "speaking",
            "onset_audio": None,
            "utterance_audio": None,
        }
