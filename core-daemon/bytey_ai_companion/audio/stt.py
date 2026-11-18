"""Speech-to-Text (STT) engine supporting local faster-whisper and HTTP Whisper APIs."""

from __future__ import annotations

import asyncio
import io
import logging
from typing import Any
import wave

import httpx
import numpy as np

logger = logging.getLogger(__name__)

try:
    from faster_whisper import WhisperModel as _WhisperModel  # type: ignore[import-not-found]
except ImportError:
    _WhisperModel = None  # type: ignore[assignment]

DEFAULT_OPENAI_STT_URL = "https://api.openai.com/v1/audio/transcriptions"


def pcm_to_wav_bytes(pcm_bytes: bytes, sample_rate: int = 16000, channels: int = 1) -> bytes:
    """Wrap raw signed 16-bit PCM bytes in a RIFF/WAVE header."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm_bytes)
    return buf.getvalue()


class STTEngine:
    """Async STT router for local faster-whisper and remote Whisper-compatible endpoints."""

    def __init__(self, http_client: httpx.AsyncClient | None = None) -> None:
        self._http_client = http_client
        self._owns_client = http_client is None
        self._whisper_models: dict[tuple[str, str, str], Any] = {}
        self._model_lock = asyncio.Lock()
        self._warned_missing_whisper = False

    @property
    def has_local_whisper(self) -> bool:
        """Return True if local faster-whisper package is available."""
        return _WhisperModel is not None

    def _get_client(self) -> httpx.AsyncClient:
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(timeout=30.0)
            self._owns_client = True
        return self._http_client

    async def close(self) -> None:
        """Close internally owned HTTP client."""
        if self._owns_client and self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None

    async def _get_whisper_model(
        self, model: str, device: str = "cpu", compute_type: str = "int8"
    ) -> Any:
        if _WhisperModel is None:
            if not self._warned_missing_whisper:
                logger.warning("faster_whisper is not installed; local STT disabled")
                self._warned_missing_whisper = True
            return None

        key = (model, device, compute_type)
        if key in self._whisper_models:
            return self._whisper_models[key]

        async with self._model_lock:
            if key in self._whisper_models:
                return self._whisper_models[key]
            try:
                loaded = await asyncio.to_thread(
                    _WhisperModel, model, device=device, compute_type=compute_type
                )
                self._whisper_models[key] = loaded
                return loaded
            except Exception as exc:
                logger.warning("Failed to initialize WhisperModel(%s): %s", model, exc)
                return None

    def _run_local_transcribe(
        self,
        whisper_model: Any,
        pcm_bytes: bytes,
        language: str | None,
        prompt_hint: str | None = None,
        hotwords: str | None = None,
    ) -> str:
        usable_len = len(pcm_bytes) - (len(pcm_bytes) % 2)
        if usable_len < 2:
            return ""
        audio = (
            np.frombuffer(pcm_bytes[:usable_len], dtype=np.int16).astype(np.float32) / 32768.0
        )
        peak = float(np.max(np.abs(audio))) if audio.size > 0 else 0.0
        if peak < 0.015:
            return ""
        if 0.035 <= peak < 0.35:
            gain = min(3.0, 0.65 / peak)
            audio = np.clip(audio * gain, -1.0, 1.0)
        lang_arg = None if not language or language.lower() == "auto" else language
        kwargs: dict[str, Any] = {
            "language": lang_arg,
            "beam_size": 2,
            "vad_filter": False,
            "condition_on_previous_text": False,
            "no_speech_threshold": 0.55,
        }
        if prompt_hint:
            kwargs["initial_prompt"] = prompt_hint
        if hotwords:
            kwargs["hotwords"] = hotwords
        try:
            segments, _info = whisper_model.transcribe(audio, **kwargs)
        except TypeError:
            segments, _info = whisper_model.transcribe(
                audio, language=lang_arg, beam_size=1, vad_filter=False
            )
        valid_texts: list[str] = []
        for seg in segments:
            txt = getattr(seg, "text", "").strip()
            if not txt:
                continue
            if getattr(seg, "no_speech_prob", 0.0) > 0.62:
                continue
            valid_texts.append(txt)
        return " ".join(valid_texts).strip()

    async def transcribe(
        self,
        pcm_bytes: bytes,
        backend: str = "local_whisper",
        model: str = "tiny.en",
        api_url: str = "",
        api_key: str | None = None,
        language: str | None = None,
        sample_rate: int = 16000,
        prompt_hint: str | None = None,
        hotwords: str | None = None,
    ) -> str:
        """Transcribe signed 16-bit mono PCM bytes using configured STT backend."""
        if not pcm_bytes:
            return ""

        if backend == "local_whisper":
            whisper_model = await self._get_whisper_model(model or "tiny.en")
            if whisper_model is None:
                return ""
            try:
                return await asyncio.to_thread(
                    self._run_local_transcribe,
                    whisper_model,
                    pcm_bytes,
                    language,
                    prompt_hint,
                    hotwords,
                )
            except Exception as exc:
                logger.warning("Local Whisper transcription failed: %s", exc)
                return ""

        if backend in ("openai_whisper", "custom_whisper"):
            url = api_url.strip() if api_url else ""
            if not url and backend == "openai_whisper":
                url = DEFAULT_OPENAI_STT_URL
            if not url:
                logger.warning("No api_url configured for STT backend '%s'", backend)
                return ""

            wav_bytes = pcm_to_wav_bytes(pcm_bytes, sample_rate=sample_rate, channels=1)
            headers: dict[str, str] = {}
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"

            data: dict[str, str] = {"model": model or "whisper-1"}
            if language and language.lower() != "auto":
                data["language"] = language
            if prompt_hint:
                data["prompt"] = prompt_hint

            files = {"file": ("audio.wav", wav_bytes, "audio/wav")}
            client = self._get_client()
            try:
                resp = await client.post(url, data=data, files=files, headers=headers)
                resp.raise_for_status()
                content_type = resp.headers.get("content-type", "")
                if "application/json" in content_type or resp.text.lstrip().startswith("{"):
                    return str(resp.json().get("text", "")).strip()
                return resp.text.strip()
            except Exception as exc:
                logger.warning("HTTP STT transcription failed (%s): %s", backend, exc)
                return ""

        logger.warning("Unknown STT backend '%s'", backend)
        return ""

    async def transcribe_wake_window(
        self,
        pcm_bytes: bytes,
        backend: str = "local_whisper",
        model: str = "tiny.en",
        api_url: str = "",
        api_key: str | None = None,
        language: str | None = None,
        sample_rate: int = 16000,
        prompt_hint: str | None = None,
        hotwords: str | None = None,
    ) -> str:
        """Transcribe short wake-word onset audio window."""
        return await self.transcribe(
            pcm_bytes=pcm_bytes,
            backend=backend,
            model=model,
            api_url=api_url,
            api_key=api_key,
            language=language,
            sample_rate=sample_rate,
            prompt_hint=prompt_hint,
            hotwords=hotwords,
        )
