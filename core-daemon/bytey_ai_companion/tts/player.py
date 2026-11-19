"""Sequential async TTS queue and PipeWire player with RMS metering and barge-in."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import inspect
import io
import logging
import os
from typing import Any
import wave

import numpy as np

from .chunker import SentenceChunker
from .piper_provider import PiperTTSClient
from .providers import ElevenLabsTTSClient, KokoroTTSClient

logger = logging.getLogger(__name__)

PW_PLAY_BIN = "/usr/bin/pw-play"


def _cfg_get(cfg: Any, paths: tuple[tuple[str, ...], ...], default: Any = None) -> Any:
    """Resolve a setting from nested dicts, attribute objects, or flat keys."""
    if cfg is None:
        return default
    for path in paths:
        cur = cfg
        found = True
        for key in path:
            if isinstance(cur, dict) and key in cur:
                cur = cur[key]
            elif hasattr(cur, key):
                cur = getattr(cur, key)
            else:
                found = False
                break
        if found and cur is not None:
            return cur
    return default


async def _invoke(cb: Callable[..., Any] | None, *args: Any, **kwargs: Any) -> Any:
    """Invoke a sync or async callback safely, adapting argument count if needed."""
    if cb is None:
        return None
    try:
        sig = inspect.signature(cb)
        params = list(sig.parameters.values())
        has_var_pos = any(p.kind == inspect.Parameter.VAR_POSITIONAL for p in params)
        has_var_kw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params)
        if not has_var_pos:
            pos_params = [
                p
                for p in params
                if p.kind
                in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
            ]
            args = args[: len(pos_params)]
        if not has_var_kw:
            kw_names = {
                p.name
                for p in params
                if p.kind
                in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
            }
            kwargs = {k: v for k, v in kwargs.items() if k in kw_names}
    except (TypeError, ValueError):
        pass

    result = cb(*args, **kwargs)
    if inspect.isawaitable(result):
        return await result
    return result


def _wrap_pcm_24000_as_wav(pcm_bytes: bytes) -> bytes:
    """Wrap raw 24kHz 16-bit mono PCM bytes into an in-memory WAV container."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(24000)
        wf.writeframes(pcm_bytes)
    return buf.getvalue()


def _extract_wav_pcm(wav_bytes: bytes) -> tuple[bytes, int, int, int]:
    """Return (pcm_frames, sample_rate, channels, sampwidth) from WAV bytes."""
    try:
        with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
            return (
                wf.readframes(wf.getnframes()),
                wf.getframerate(),
                wf.getnchannels(),
                wf.getsampwidth(),
            )
    except Exception:
        return wav_bytes, 24000, 1, 2


def _compute_pcm_rms(chunk: bytes, sampwidth: int = 2) -> float:
    """Compute normalized RMS amplitude in [0.0, 1.0] from PCM bytes."""
    if len(chunk) < sampwidth:
        return 0.0
    if sampwidth == 2:
        usable = len(chunk) - (len(chunk) % 2)
        samples = np.frombuffer(chunk[:usable], dtype=np.int16).astype(np.float32) / 32768.0
    elif sampwidth == 1:
        samples = (np.frombuffer(chunk, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    elif sampwidth == 4:
        usable = len(chunk) - (len(chunk) % 4)
        samples = (
            np.frombuffer(chunk[:usable], dtype=np.int32).astype(np.float32) / 2147483648.0
        )
    else:
        return 0.0
    if samples.size == 0:
        return 0.0
    rms = float(np.sqrt(np.mean(samples * samples)))
    return min(1.0, max(0.0, rms))


class StreamingTTSPlayer:
    """Plays queued sentences sequentially via pw-play while emitting RMS levels."""

    def __init__(
        self,
        get_config: Callable[[], Any],
        get_secret: Callable[[str], Any],
        on_audio_level: Callable[..., Any] | None = None,
        on_state_change: Callable[..., Any] | None = None,
        kokoro_client: KokoroTTSClient | None = None,
        elevenlabs_client: ElevenLabsTTSClient | None = None,
        piper_client: PiperTTSClient | None = None,
    ) -> None:
        self._get_config = get_config
        self._get_secret = get_secret
        self._on_audio_level = on_audio_level
        self._on_state_change = on_state_change

        self._kokoro = kokoro_client or KokoroTTSClient()
        self._owns_kokoro = kokoro_client is None
        self._elevenlabs = elevenlabs_client or ElevenLabsTTSClient()
        self._owns_elevenlabs = elevenlabs_client is None
        self._piper = piper_client or PiperTTSClient()
        self._owns_piper = piper_client is None

        self._queue: asyncio.Queue[tuple[int, str]] = asyncio.Queue()
        self._worker_task: asyncio.Task[None] | None = None
        self._active_proc: asyncio.subprocess.Process | None = None
        self._active_synth_task: asyncio.Task[tuple[bytes, str]] | None = None
        self._idle_event = asyncio.Event()
        self._idle_event.set()
        self._generation = 0
        self._speaking = False
        self._stopped = False

    def _ensure_worker(self) -> None:
        if self._worker_task is None or self._worker_task.done():
            self._stopped = False
            self._worker_task = asyncio.create_task(self._worker_loop(), name="tts-player-worker")

    def _read_settings(self) -> dict[str, Any]:
        cfg = self._get_config() if self._get_config else {}
        return {
            "muted": bool(
                _cfg_get(
                    cfg,
                    (
                        ("voice_out", "tts_muted"),
                        ("voice_out", "muted"),
                        ("voice_output", "tts_muted"),
                        ("voice_output", "muted"),
                        ("tts", "muted"),
                        ("tts_muted",),
                    ),
                    False,
                )
            ),
            "provider": str(
                _cfg_get(
                    cfg,
                    (
                        ("voice_out", "tts_provider"),
                        ("voice_out", "provider"),
                        ("voice_output", "provider"),
                        ("voice_output", "tts_provider"),
                        ("tts", "provider"),
                        ("tts_provider",),
                    ),
                    "kokoro",
                )
            ).lower(),
            "kokoro_base_url": str(
                _cfg_get(
                    cfg,
                    (
                        ("voice_out", "kokoro_base_url"),
                        ("voice_output", "kokoro_base_url"),
                        ("voice_output", "kokoro_url"),
                        ("tts", "kokoro_base_url"),
                        ("kokoro_base_url",),
                    ),
                    "http://localhost:8880/v1",
                )
            ),
            "kokoro_voice": str(
                _cfg_get(
                    cfg,
                    (
                        ("voice_out", "kokoro_voice"),
                        ("voice_output", "kokoro_voice"),
                        ("voice_output", "voice"),
                        ("tts", "kokoro_voice"),
                        ("kokoro_voice",),
                    ),
                    "af_heart",
                )
            ),
            "kokoro_speed": float(
                _cfg_get(
                    cfg,
                    (
                        ("voice_out", "kokoro_speed"),
                        ("voice_output", "kokoro_speed"),
                        ("voice_output", "speed"),
                        ("tts", "kokoro_speed"),
                        ("kokoro_speed",),
                    ),
                    1.0,
                )
            ),
            "piper_model": str(
                _cfg_get(
                    cfg,
                    (
                        ("voice_out", "piper_model"),
                        ("voice_output", "piper_model"),
                        ("tts", "piper_model"),
                        ("piper_model",),
                    ),
                    "en_US-lessac-medium",
                )
            ),
            "piper_models_dir": str(
                _cfg_get(
                    cfg,
                    (
                        ("voice_out", "piper_models_dir"),
                        ("voice_output", "piper_models_dir"),
                        ("tts", "piper_models_dir"),
                        ("piper_models_dir",),
                    ),
                    "",
                )
            ),
            "piper_speed": float(
                _cfg_get(
                    cfg,
                    (
                        ("voice_out", "piper_speed"),
                        ("voice_output", "piper_speed"),
                        ("tts", "piper_speed"),
                        ("piper_speed",),
                    ),
                    1.0,
                )
            ),
            "piper_pitch": float(
                _cfg_get(
                    cfg,
                    (
                        ("voice_out", "piper_pitch"),
                        ("voice_output", "piper_pitch"),
                        ("tts", "piper_pitch"),
                        ("piper_pitch",),
                    ),
                    0.0,
                )
            ),
            "piper_noise_scale": float(
                _cfg_get(
                    cfg,
                    (
                        ("voice_out", "piper_noise_scale"),
                        ("voice_output", "piper_noise_scale"),
                        ("tts", "piper_noise_scale"),
                        ("piper_noise_scale",),
                    ),
                    0.667,
                )
            ),
            "piper_speaker_id": _cfg_get(
                cfg,
                (
                    ("voice_out", "piper_speaker_id"),
                    ("voice_output", "piper_speaker_id"),
                    ("tts", "piper_speaker_id"),
                    ("piper_speaker_id",),
                ),
                None,
            ),
            "elevenlabs_voice_id": str(
                _cfg_get(
                    cfg,
                    (
                        ("voice_out", "elevenlabs_voice_id"),
                        ("voice_output", "elevenlabs_voice_id"),
                        ("voice_output", "voice_id"),
                        ("tts", "elevenlabs_voice_id"),
                        ("elevenlabs_voice_id",),
                    ),
                    "",
                )
            ),
            "elevenlabs_model_id": str(
                _cfg_get(
                    cfg,
                    (
                        ("voice_out", "elevenlabs_model_id"),
                        ("voice_output", "elevenlabs_model_id"),
                        ("voice_output", "model_id"),
                        ("tts", "elevenlabs_model_id"),
                        ("elevenlabs_model_id",),
                    ),
                    "eleven_flash_v2_5",
                )
            ),
            "elevenlabs_stability": _cfg_get(
                cfg,
                (
                    ("voice_out", "elevenlabs_stability"),
                    ("voice_output", "elevenlabs_stability"),
                    ("elevenlabs_stability",),
                ),
                None,
            ),
            "elevenlabs_similarity": _cfg_get(
                cfg,
                (
                    ("voice_out", "elevenlabs_similarity"),
                    ("voice_output", "elevenlabs_similarity"),
                    ("elevenlabs_similarity",),
                ),
                None,
            ),
        }

    async def _get_secret_value(self, *names: str) -> str:
        if self._get_secret is None:
            return ""
        for name in names:
            val = await _invoke(self._get_secret, name)
            if val:
                return str(val)
        return ""

    async def enqueue_sentence(self, text: str) -> None:
        """Enqueue a sentence for sequential TTS synthesis and playback."""
        cleaned = (text or "").strip()
        if not cleaned:
            return
        settings = self._read_settings()
        if settings["muted"] or settings["provider"] in ("disabled", "none", "muted", ""):
            return
        self._ensure_worker()
        self._idle_event.clear()
        await self._queue.put((self._generation, cleaned))

    async def speak_text(self, text: str) -> None:
        """Chunk full text into sentences and enqueue each for playback."""
        cleaned = (text or "").strip()
        if not cleaned:
            return
        chunker = SentenceChunker()
        for sentence in chunker.feed(cleaned):
            await self.enqueue_sentence(sentence)
        tail = chunker.flush()
        if tail:
            await self.enqueue_sentence(tail)

    async def wait_idle(self) -> None:
        """Wait until all queued TTS sentences finish playing (or are interrupted)."""
        await self._idle_event.wait()

    async def _terminate_active_proc(self) -> None:
        proc = self._active_proc
        self._active_proc = None
        if proc is None:
            return
        if proc.returncode is None:
            try:
                proc.terminate()
                await asyncio.wait_for(proc.wait(), timeout=0.5)
            except (asyncio.TimeoutError, ProcessLookupError):
                try:
                    proc.kill()
                    await proc.wait()
                except ProcessLookupError:
                    pass

    async def barge_in(self) -> None:
        """Immediately cancel queued sentences, kill active playback, and emit 0.0 level."""
        self._generation += 1
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
                self._queue.task_done()
            except asyncio.QueueEmpty:
                break

        if self._active_synth_task is not None and not self._active_synth_task.done():
            self._active_synth_task.cancel()
        await self._terminate_active_proc()
        self._speaking = False
        self._idle_event.set()
        await _invoke(self._on_audio_level, "tts", 0.0)

    async def stop(self) -> None:
        """Interrupt active playback and shut down the TTS worker and HTTP clients."""
        self._stopped = True
        await self.barge_in()
        if self._worker_task is not None:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
            self._worker_task = None
        if self._owns_kokoro:
            await self._kokoro.close()
        if self._owns_elevenlabs:
            await self._elevenlabs.close()
        if self._owns_piper:
            await self._piper.close()

    async def _synthesize(self, text: str, settings: dict[str, Any]) -> tuple[bytes, str] | None:
        provider = settings["provider"]
        if settings["muted"] or provider in ("disabled", "none", "muted", ""):
            return None

        if provider == "kokoro":
            api_key = await self._get_secret_value("KOKORO_API_KEY")
            return await self._kokoro.synthesize(
                text=text,
                base_url=settings["kokoro_base_url"],
                voice=settings["kokoro_voice"],
                speed=settings["kokoro_speed"],
                api_key=api_key or None,
            )

        if provider == "piper":
            raw_sid = settings.get("piper_speaker_id")
            if raw_sid is None or isinstance(raw_sid, bool) or raw_sid == "":
                speaker_id: int | None = None
            else:
                try:
                    speaker_id = max(0, int(raw_sid))
                except (TypeError, ValueError):
                    speaker_id = None
            return await self._piper.synthesize(
                text=text,
                model=settings["piper_model"],
                models_dir=settings["piper_models_dir"],
                speed=settings["piper_speed"],
                pitch=settings["piper_pitch"],
                noise_scale=settings["piper_noise_scale"],
                speaker_id=speaker_id,
            )

        if provider == "elevenlabs":
            api_key = await self._get_secret_value("ELEVENLABS_API_KEY")
            return await self._elevenlabs.synthesize(
                text=text,
                voice_id=settings["elevenlabs_voice_id"],
                model_id=settings["elevenlabs_model_id"],
                api_key=api_key,
                stability=settings["elevenlabs_stability"],
                similarity_boost=settings["elevenlabs_similarity"],
            )

        logger.warning("Unknown TTS provider '%s'", provider)
        return None

    async def _emit_rms_frames(
        self, pcm_frames: bytes, sample_rate: int, channels: int, sampwidth: int, gen: int
    ) -> None:
        bytes_per_sec = max(1, sample_rate * channels * sampwidth)
        chunk_ms = 0.05
        chunk_bytes = max(sampwidth * channels, int(bytes_per_sec * chunk_ms))
        offset = 0
        total = len(pcm_frames)

        while offset < total and gen == self._generation and not self._stopped:
            end = min(total, offset + chunk_bytes)
            slice_bytes = pcm_frames[offset:end]
            rms = _compute_pcm_rms(slice_bytes, sampwidth=sampwidth)
            await _invoke(self._on_audio_level, "tts", rms)
            duration = len(slice_bytes) / bytes_per_sec
            await asyncio.sleep(max(0.01, duration))
            offset = end

    async def _play_audio(self, audio_bytes: bytes, fmt: str, gen: int) -> None:
        if not audio_bytes or gen != self._generation or self._stopped:
            return

        if fmt == "pcm_24000":
            wav_bytes = _wrap_pcm_24000_as_wav(audio_bytes)
            pcm_frames, sample_rate, channels, sampwidth = audio_bytes, 24000, 1, 2
        else:
            wav_bytes = audio_bytes
            pcm_frames, sample_rate, channels, sampwidth = _extract_wav_pcm(wav_bytes)

        if not os.path.exists(PW_PLAY_BIN):
            logger.warning("%s not found; skipping TTS playback", PW_PLAY_BIN)
            return

        proc = await asyncio.create_subprocess_exec(
            PW_PLAY_BIN,
            "-",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        self._active_proc = proc

        rms_task = asyncio.create_task(
            self._emit_rms_frames(pcm_frames, sample_rate, channels, sampwidth, gen)
        )
        try:
            if proc.stdin is not None:
                try:
                    proc.stdin.write(wav_bytes)
                    await proc.stdin.drain()
                    proc.stdin.close()
                except (BrokenPipeError, ConnectionResetError):
                    pass
            await proc.wait()
        finally:
            if not rms_task.done():
                rms_task.cancel()
            try:
                await rms_task
            except asyncio.CancelledError:
                pass
            if self._active_proc is proc:
                self._active_proc = None

    async def _worker_loop(self) -> None:
        while not self._stopped:
            try:
                gen, sentence = await self._queue.get()
            except asyncio.CancelledError:
                break

            try:
                if gen != self._generation:
                    continue

                settings = self._read_settings()
                if settings["muted"] or settings["provider"] in ("disabled", "none", "muted", ""):
                    continue

                if not self._speaking:
                    self._speaking = True
                    await _invoke(self._on_state_change, "SPEAKING")

                self._active_synth_task = asyncio.create_task(
                    self._synthesize(sentence, settings)
                )
                try:
                    synth_result = await self._active_synth_task
                except asyncio.CancelledError:
                    continue
                finally:
                    self._active_synth_task = None

                if synth_result is None or gen != self._generation:
                    continue

                audio_bytes, fmt = synth_result
                await self._play_audio(audio_bytes, fmt, gen)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning("TTS playback error: %s", exc)
            finally:
                self._queue.task_done()
                if self._queue.empty() and self._active_proc is None:
                    was_speaking = self._speaking
                    self._speaking = False
                    await _invoke(self._on_audio_level, "tts", 0.0)
                    if was_speaking and not self._stopped:
                        await _invoke(self._on_state_change, "IDLE")
                    self._idle_event.set()
