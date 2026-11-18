"""Async microphone capture service with PipeWire/sounddevice, VAD, wake-word, and STT."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import inspect
import logging
import os
from typing import Any

from .stt import STTEngine
from .vad import VADProcessor
from .wakeword import WakeWordMatcher

logger = logging.getLogger(__name__)

try:
    import sounddevice as sd  # type: ignore[import-not-found]
except ImportError:
    sd = None  # type: ignore[assignment]

PW_RECORD_BIN = "/usr/bin/pw-record"


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


class VoiceInputService:
    """Continuous 16kHz mono PCM capture driving VAD, wake-word detection, and STT."""

    def __init__(
        self,
        get_config: Callable[[], Any],
        get_secret: Callable[[str], Any],
        on_audio_level: Callable[..., Any] | None = None,
        on_state_change: Callable[..., Any] | None = None,
        on_prompt_transcribed: Callable[..., Any] | None = None,
        on_barge_in: Callable[..., Any] | None = None,
        stt_engine: STTEngine | None = None,
        vad: VADProcessor | None = None,
        wake_matcher: WakeWordMatcher | None = None,
    ) -> None:
        self._get_config = get_config
        self._get_secret = get_secret
        self._on_audio_level = on_audio_level
        self._on_state_change = on_state_change
        self._on_prompt_transcribed = on_prompt_transcribed
        self._on_barge_in = on_barge_in

        self._stt = stt_engine or STTEngine()
        self._owns_stt = stt_engine is None
        self._vad = vad or VADProcessor()
        self._wake_matcher = wake_matcher or WakeWordMatcher()

        self._running = False
        self._muted = False
        self._task: asyncio.Task[None] | None = None
        self._proc: asyncio.subprocess.Process | None = None
        self._wake_task: asyncio.Task[bool] | None = None

        self._awake = False
        self._awaiting_followup = False
        self._manual_triggered = False
        self._onset_remainder = ""
        self._followup_idle_ms = 0.0
        self._followup_timeout_ms = 12000.0
        self._last_level_emit = 0.0

    def _read_settings(self) -> dict[str, Any]:
        cfg = self._get_config() if self._get_config else {}
        return {
            "muted": bool(
                _cfg_get(
                    cfg,
                    (
                        ("voice_in", "mic_muted"),
                        ("voice_input", "mic_muted"),
                        ("voice_input", "muted"),
                        ("audio", "mic_muted"),
                        ("mic_muted",),
                    ),
                    False,
                )
            ),
            "wake_enabled": bool(
                _cfg_get(
                    cfg,
                    (
                        ("voice_in", "wake_word_enabled"),
                        ("voice_in", "wake_enabled"),
                        ("voice_input", "wake_word_enabled"),
                        ("voice_input", "wake_enabled"),
                        ("wake_word_enabled",),
                        ("wake_enabled",),
                    ),
                    True,
                )
            ),
            "wake_phrase": str(
                _cfg_get(
                    cfg,
                    (
                        ("general", "wake_phrase"),
                        ("voice_in", "wake_phrase"),
                        ("voice_input", "wake_phrase"),
                        ("audio", "wake_phrase"),
                        ("wake_phrase",),
                    ),
                    "Hey Nova",
                )
            ),
            "wake_sensitivity": float(
                _cfg_get(
                    cfg,
                    (
                        ("voice_in", "wake_sensitivity"),
                        ("voice_input", "wake_sensitivity"),
                        ("wake_sensitivity",),
                    ),
                    0.72,
                )
            ),
            "wake_model": str(
                _cfg_get(
                    cfg,
                    (
                        ("voice_in", "wake_model"),
                        ("voice_in", "whisper_model"),
                        ("voice_input", "wake_model"),
                        ("wake_model",),
                    ),
                    "tiny.en",
                )
            ),
            "vad_threshold": float(
                _cfg_get(
                    cfg,
                    (
                        ("voice_in", "vad_threshold"),
                        ("voice_input", "vad_threshold"),
                        ("vad_threshold",),
                    ),
                    0.015,
                )
            ),
            "silence_timeout_ms": int(
                _cfg_get(
                    cfg,
                    (
                        ("voice_in", "silence_timeout_ms"),
                        ("voice_input", "silence_timeout_ms"),
                        ("silence_timeout_ms",),
                    ),
                    1300,
                )
            ),
            "max_utterance_s": float(
                _cfg_get(
                    cfg,
                    (
                        ("voice_in", "max_utterance_s"),
                        ("voice_input", "max_utterance_s"),
                        ("max_utterance_s",),
                    ),
                    30.0,
                )
            ),
            "stt_backend": str(
                _cfg_get(
                    cfg,
                    (
                        ("voice_in", "stt_backend"),
                        ("voice_input", "stt_backend"),
                        ("stt_backend",),
                    ),
                    "local_whisper",
                )
            ),
            "stt_model": str(
                _cfg_get(
                    cfg,
                    (
                        ("voice_in", "whisper_model"),
                        ("voice_in", "stt_model"),
                        ("voice_input", "stt_model"),
                        ("stt_model",),
                    ),
                    "tiny.en",
                )
            ),
            "stt_api_url": str(
                _cfg_get(
                    cfg,
                    (
                        ("voice_in", "stt_api_url"),
                        ("voice_input", "stt_api_url"),
                        ("voice_input", "whisper_api_url"),
                        ("stt_api_url",),
                    ),
                    "",
                )
            ),
            "stt_language": str(
                _cfg_get(
                    cfg,
                    (
                        ("voice_in", "stt_language"),
                        ("voice_input", "stt_language"),
                        ("stt_language",),
                    ),
                    "auto",
                )
            ),
        }

    async def _resolve_stt_secret(self, backend: str) -> str | None:
        if self._get_secret is None:
            return None
        key_names = (
            ("OPENAI_API_KEY", "STT_API_KEY")
            if backend == "openai_whisper"
            else ("STT_API_KEY", "CUSTOM_WHISPER_API_KEY", "OPENAI_API_KEY")
        )
        for name in key_names:
            val = await _invoke(self._get_secret, name)
            if val:
                return str(val)
        return None

    async def _emit_level(self, rms: float, force: bool = False) -> None:
        if self._on_audio_level is None:
            return
        now = asyncio.get_running_loop().time()
        if force or (now - self._last_level_emit) >= 0.05:
            self._last_level_emit = now
            await _invoke(self._on_audio_level, "mic", float(rms))

    async def start(self) -> None:
        """Start the background microphone capture task."""
        if self._running:
            return
        self._running = True
        settings = self._read_settings()
        self._muted = settings["muted"]
        if self._muted:
            await _invoke(self._on_state_change, "MUTED")
        self._task = asyncio.create_task(self._capture_loop(), name="voice-input-capture")

    async def stop(self) -> None:
        """Stop microphone capture and clean up subprocesses/tasks."""
        self._running = False
        if self._wake_task and not self._wake_task.done():
            self._wake_task.cancel()
        await self._terminate_proc()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self._owns_stt:
            await self._stt.close()
        self._vad.reset()
        await self._emit_level(0.0, force=True)

    async def set_muted(self, muted: bool) -> None:
        """Mute or unmute microphone processing."""
        self._muted = bool(muted)
        self._awake = False
        self._awaiting_followup = False
        self._manual_triggered = False
        self._onset_remainder = ""
        self._vad.reset()
        if self._muted:
            await self._emit_level(0.0, force=True)
            await _invoke(self._on_state_change, "MUTED")
        else:
            await _invoke(self._on_state_change, "IDLE")

    async def trigger_manual_listen(self) -> None:
        """Force active listening immediately and barge in on any active TTS playback."""
        self._muted = False
        self._awake = True
        self._awaiting_followup = True
        self._manual_triggered = True
        self._onset_remainder = ""
        self._followup_idle_ms = 0.0
        if self._wake_task and not self._wake_task.done():
            self._wake_task.cancel()
            self._wake_task = None
        await _invoke(self._on_barge_in)
        self._vad.force_start_listening()
        await _invoke(self._on_state_change, "LISTENING")

    async def _terminate_proc(self) -> None:
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        if proc.returncode is None:
            try:
                proc.terminate()
                await asyncio.wait_for(proc.wait(), timeout=1.5)
            except (asyncio.TimeoutError, ProcessLookupError):
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass

    async def _capture_loop(self) -> None:
        backoff = 1.0
        while self._running:
            try:
                if os.path.exists(PW_RECORD_BIN):
                    await self._run_pw_record_stream()
                elif sd is not None:
                    await self._run_sounddevice_stream()
                else:
                    logger.warning(
                        "No audio capture backend found (%s missing and sounddevice unavailable)",
                        PW_RECORD_BIN,
                    )
                    await _invoke(self._on_state_change, "IDLE")
                    await asyncio.sleep(5.0)
                    continue
                backoff = 1.0
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning("Audio capture error: %s", exc)
                await _invoke(self._on_state_change, "MUTED" if self._muted else "IDLE")
                await asyncio.sleep(backoff)
                backoff = min(10.0, backoff * 2.0)

    async def _run_pw_record_stream(self) -> None:
        proc = await asyncio.create_subprocess_exec(
            PW_RECORD_BIN,
            "--format=s16",
            "--rate=16000",
            "--channels=1",
            "-",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._proc = proc
        assert proc.stdout is not None
        frame_bytes = self._vad.frame_bytes

        try:
            while self._running:
                try:
                    chunk = await proc.stdout.readexactly(frame_bytes)
                except asyncio.IncompleteReadError:
                    stderr_bytes = b""
                    if proc.stderr is not None:
                        stderr_bytes = await proc.stderr.read()
                    err_msg = stderr_bytes.decode("utf-8", errors="replace").strip()
                    if err_msg:
                        logger.warning("pw-record stream ended: %s", err_msg)
                    await asyncio.sleep(1.0)
                    break
                await self._process_pcm_frame(chunk)
        finally:
            await self._terminate_proc()

    async def _run_sounddevice_stream(self) -> None:
        if sd is None:
            return
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=64)

        def _callback(indata: Any, _frames: int, _time_info: Any, status: Any) -> None:
            if status:
                logger.debug("sounddevice status: %s", status)
            data = bytes(indata)
            try:
                loop.call_soon_threadsafe(queue.put_nowait, data)
            except asyncio.QueueFull:
                pass

        stream = sd.RawInputStream(
            samplerate=self._vad.sample_rate,
            blocksize=self._vad.frame_samples,
            channels=1,
            dtype="int16",
            callback=_callback,
        )
        with stream:
            while self._running:
                chunk = await queue.get()
                await self._process_pcm_frame(chunk)

    async def _process_pcm_frame(self, pcm_bytes: bytes) -> None:
        settings = self._read_settings()
        if settings["muted"] != self._muted and not self._manual_triggered:
            self._muted = settings["muted"]
            if self._muted:
                self._vad.reset()
                await self._emit_level(0.0, force=True)
                await _invoke(self._on_state_change, "MUTED")
                return

        if self._muted:
            return

        self._vad.vad_threshold = settings["vad_threshold"]
        self._vad.silence_timeout_ms = settings["silence_timeout_ms"]
        self._vad.max_utterance_s = settings["max_utterance_s"]

        event = self._vad.process_frame(pcm_bytes)
        await self._emit_level(event["rms"])
        state = event["state"]

        if self._awaiting_followup and state == "idle":
            self._followup_idle_ms += self._vad.frame_ms
            if self._followup_idle_ms >= self._followup_timeout_ms:
                self._awake = False
                self._awaiting_followup = False
                self._manual_triggered = False
                await _invoke(self._on_state_change, "IDLE")
            return

        if state in ("speaking", "onset"):
            self._followup_idle_ms = 0.0

        if state == "onset" and not self._awake and settings["wake_enabled"]:
            onset_audio = event["onset_audio"]
            if onset_audio:
                self._wake_task = asyncio.create_task(
                    self._check_wake_onset(onset_audio, settings)
                )
            return

        if state == "utterance_end":
            utterance_audio = event["utterance_audio"] or b""
            await self._handle_utterance_end(utterance_audio, settings)

    async def _check_wake_onset(self, onset_audio: bytes, settings: dict[str, Any]) -> bool:
        try:
            text = await self._stt.transcribe_wake_window(
                onset_audio,
                backend="local_whisper",
                model=settings["wake_model"],
                sample_rate=self._vad.sample_rate,
            )
            matched, remainder = self._wake_matcher.match_wake_phrase(
                text, settings["wake_phrase"], settings["wake_sensitivity"]
            )
            if matched:
                self._awake = True
                self._onset_remainder = remainder
                await _invoke(self._on_barge_in)
                await _invoke(self._on_state_change, "LISTENING")
                return True
            return False
        except asyncio.CancelledError:
            return False
        except Exception as exc:
            logger.warning("Wake onset check error: %s", exc)
            return False

    async def _transcribe_prompt(self, pcm_bytes: bytes, settings: dict[str, Any]) -> str:
        backend = settings["stt_backend"]
        api_key = (
            await self._resolve_stt_secret(backend)
            if backend in ("openai_whisper", "custom_whisper")
            else None
        )
        return await self._stt.transcribe(
            pcm_bytes,
            backend=backend,
            model=settings["stt_model"],
            api_url=settings["stt_api_url"],
            api_key=api_key,
            language=settings["stt_language"],
            sample_rate=self._vad.sample_rate,
        )

    async def _handle_utterance_end(
        self, utterance_audio: bytes, settings: dict[str, Any]
    ) -> None:
        wake_checked_at_onset = False
        if self._wake_task is not None:
            wake_checked_at_onset = True
            if not self._wake_task.done():
                await self._wake_task
            self._wake_task = None

        if not utterance_audio:
            if self._awake:
                self._awake = False
                self._awaiting_followup = False
                self._manual_triggered = False
                await _invoke(self._on_state_change, "IDLE")
            return

        # Direct prompt capture when already awake via manual trigger or wake-only follow-up.
        if self._awake and (self._awaiting_followup or self._manual_triggered):
            await _invoke(self._on_state_change, "THINKING")
            prompt_text = await self._transcribe_prompt(utterance_audio, settings)
            self._awake = False
            self._awaiting_followup = False
            self._manual_triggered = False
            self._onset_remainder = ""
            if prompt_text:
                await _invoke(self._on_prompt_transcribed, prompt_text)
            else:
                await _invoke(self._on_state_change, "IDLE")
            return

        # Short utterance (< 1.2s) ended before onset fired: check wake word on full clip.
        if not self._awake and not wake_checked_at_onset:
            if not settings["wake_enabled"]:
                return
            wake_text = await self._stt.transcribe_wake_window(
                utterance_audio,
                backend="local_whisper",
                model=settings["wake_model"],
                sample_rate=self._vad.sample_rate,
            )
            matched, remainder = self._wake_matcher.match_wake_phrase(
                wake_text, settings["wake_phrase"], settings["wake_sensitivity"]
            )
            if not matched:
                return
            await _invoke(self._on_barge_in)
            if not remainder:
                self._awake = True
                self._awaiting_followup = True
                self._followup_idle_ms = 0.0
                await _invoke(self._on_state_change, "LISTENING")
                return
            await _invoke(self._on_state_change, "THINKING")
            if settings["stt_backend"] != "local_whisper" or settings["stt_model"] != settings["wake_model"]:
                full_text = await self._transcribe_prompt(utterance_audio, settings)
                matched2, rem2 = self._wake_matcher.match_wake_phrase(
                    full_text, settings["wake_phrase"], settings["wake_sensitivity"]
                )
                prompt_text = rem2 if matched2 else (full_text or remainder)
            else:
                prompt_text = remainder
            if prompt_text:
                await _invoke(self._on_prompt_transcribed, prompt_text)
            else:
                await _invoke(self._on_state_change, "IDLE")
            return

        # Background speech did not match wake word.
        if not self._awake:
            return

        # Wake matched at onset during this utterance: transcribe full audio and strip wake phrase.
        await _invoke(self._on_state_change, "THINKING")
        full_text = await self._transcribe_prompt(utterance_audio, settings)
        matched, remainder = self._wake_matcher.match_wake_phrase(
            full_text, settings["wake_phrase"], settings["wake_sensitivity"]
        )
        prompt_text = remainder if matched else self._onset_remainder
        self._awake = False
        self._onset_remainder = ""

        if prompt_text:
            await _invoke(self._on_prompt_transcribed, prompt_text)
        else:
            # Wake-only utterance ("Hey Nova"): stay in LISTENING for follow-up prompt.
            self._awake = True
            self._awaiting_followup = True
            self._followup_idle_ms = 0.0
            await _invoke(self._on_state_change, "LISTENING")
