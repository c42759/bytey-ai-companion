"""Local Piper neural TTS provider with model discovery, download, 3-tier synthesis, and DSP pitch shift."""

from __future__ import annotations

import asyncio
import inspect
import io
import json
import logging
import math
import os
import re
import shutil
import tempfile
import unicodedata
import urllib.parse
import wave
from pathlib import Path
from typing import Any

import httpx
import numpy as np

from ..config import (
    PIPER_NOISE_SCALE_RANGE,
    PIPER_PITCH_RANGE,
    PIPER_SPEED_RANGE,
    PIPER_VOICE_PRESETS,
)
from ..storage import resolve_data_dir

logger = logging.getLogger(__name__)

PIPER_VOICES_BASE_URL = "https://huggingface.co/rhasspy/piper-voices/resolve/main"
PIPER_VOICES_INDEX_URL = f"{PIPER_VOICES_BASE_URL}/voices.json"

_LANGUAGE_DISPLAY_NAMES: dict[str, str] = {
    "en_US": "English (United States)",
    "en_GB": "English (United Kingdom)",
    "pt_PT": "Portuguese (Portugal)",
    "pt_BR": "Portuguese (Brazil)",
    "es_ES": "Spanish (Spain)",
    "es_MX": "Spanish (Mexico)",
    "fr_FR": "French (France)",
    "de_DE": "German (Germany)",
    "it_IT": "Italian (Italy)",
    "nl_NL": "Dutch (Netherlands)",
    "pl_PL": "Polish (Poland)",
    "ru_RU": "Russian (Russia)",
    "uk_UA": "Ukrainian (Ukraine)",
    "zh_CN": "Chinese (China)",
}

_CURATED_VOICE_SPECS: tuple[tuple[str, str, int], ...] = (
    ("en_US-lessac-medium", "Lessac", 1),
    ("en_US-amy-low", "Amy", 1),
    ("en_US-ryan-medium", "Ryan", 1),
    ("en_GB-alan-medium", "Alan", 1),
    ("en_GB-alba-medium", "Alba", 1),
    ("pt_PT-tugão-medium", "Tugão", 1),
    ("pt_BR-faber-medium", "Faber", 1),
    ("es_ES-davefx-medium", "Davefx", 1),
    ("es_ES-sharvard-medium", "Sharvard", 2),
    ("es_MX-ald-medium", "Ald", 1),
    ("fr_FR-siwis-medium", "Siwis", 1),
    ("fr_FR-upmc-medium", "UPMC", 2),
    ("de_DE-thorsten-medium", "Thorsten", 1),
    ("de_DE-eva_k-x_low", "Eva K", 1),
    ("it_IT-paola-medium", "Paola", 1),
    ("it_IT-riccardo-x_low", "Riccardo", 1),
    ("nl_NL-mls-medium", "MLS", 52),
    ("pl_PL-gosia-medium", "Gosia", 1),
    ("ru_RU-dmitri-medium", "Dmitri", 1),
    ("uk_UA-ukrainian_tts-medium", "Ukrainian TTS", 3),
    ("zh_CN-huayan-medium", "Huayan", 1),
)


def _infer_voice_paths(model_id: str) -> tuple[str, str, str, str, str]:
    """Infer (locale, speaker, quality, onnx_rel, json_rel) from a Piper model ID."""
    parts = model_id.split("-")
    if len(parts) >= 3:
        locale = parts[0]
        quality = parts[-1]
        speaker = "-".join(parts[1:-1])
        family = locale.split("_")[0].lower()
        onnx_rel = f"{family}/{locale}/{speaker}/{quality}/{model_id}.onnx"
    else:
        locale = ""
        speaker = model_id
        quality = ""
        onnx_rel = f"{model_id}.onnx"
    return locale, speaker, quality, onnx_rel, f"{onnx_rel}.json"


def _build_curated_voices() -> list[dict[str, Any]]:
    voices: list[dict[str, Any]] = []
    for vid, display_name, num_speakers in _CURATED_VOICE_SPECS:
        locale, _, quality, onnx_rel, json_rel = _infer_voice_paths(vid)
        voices.append(
            {
                "id": vid,
                "name": display_name,
                "language": locale,
                "language_name": _LANGUAGE_DISPLAY_NAMES.get(locale, locale),
                "quality": quality,
                "onnx_rel": onnx_rel,
                "json_rel": json_rel,
                "num_speakers": num_speakers,
            }
        )
    return voices


CURATED_PIPER_VOICES: list[dict[str, Any]] = _build_curated_voices()

__all__ = [
    "CURATED_PIPER_VOICES",
    "PIPER_NOISE_SCALE_RANGE",
    "PIPER_PITCH_RANGE",
    "PIPER_SPEED_RANGE",
    "PIPER_VOICES_BASE_URL",
    "PIPER_VOICES_INDEX_URL",
    "PIPER_VOICE_PRESETS",
    "PiperTTSClient",
    "apply_pitch_shift_wav",
]


def _clamp_float(value: Any, default: float, bounds: tuple[float, float]) -> float:
    """Coerce value to a finite float clamped within bounds, falling back to default."""
    if value is None or isinstance(value, bool):
        return default
    try:
        num = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(num):
        return default
    lo, hi = bounds
    return max(lo, min(hi, num))


def _accepts_kwarg(fn: Any, kwarg: str) -> bool:
    """Return True if callable accepts kwarg directly or via **kwargs."""
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return True
    return kwarg in sig.parameters or any(
        p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
    )


def _find_espeak_ng() -> str | None:
    """Locate espeak-ng binary if installed."""
    found = shutil.which("espeak-ng")
    if found:
        return found
    if os.path.isfile("/usr/bin/espeak-ng") and os.access("/usr/bin/espeak-ng", os.X_OK):
        return "/usr/bin/espeak-ng"
    return None


def _import_piper_voice() -> Any:
    """Return PiperVoice class if piper Python package is installed, else None."""
    try:
        import piper  # type: ignore[import-not-found]

        voice_cls = getattr(piper, "PiperVoice", None)
        if voice_cls is not None:
            return voice_cls
        from piper.voice import PiperVoice  # type: ignore[import-not-found]

        return PiperVoice
    except Exception:
        return None


def _has_onnxruntime() -> bool:
    """Return True if onnxruntime can be imported."""
    try:
        import onnxruntime  # type: ignore[import-not-found]  # noqa: F401

        return True
    except Exception:
        return False


def _find_sibling_json(onnx_path: Path) -> Path | None:
    """Locate <model>.onnx.json or <model>.json adjacent to an ONNX file."""
    candidates = (
        Path(f"{onnx_path}.json"),
        onnx_path.with_suffix(".json"),
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def _read_json_dict(path: Path) -> dict[str, Any] | None:
    """Read and parse a JSON object file, returning None on error or non-dict payload."""
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
        return parsed if isinstance(parsed, dict) else None
    except Exception:
        return None


def _write_bytes_atomic(path: Path, payload: bytes) -> None:
    """Atomically write payload bytes to path via sibling temporary file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
        os.replace(tmp_path, path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def _parse_wav_sample_rate(wav_bytes: bytes, default: int = 22050) -> int:
    """Read sample rate from WAV header, returning default on parse failure."""
    try:
        with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
            rate = int(wf.getframerate())
            return rate if rate > 0 else default
    except Exception:
        return default


def _build_atempo_chain(tempo_factor: float) -> list[str]:
    """Decompose any positive tempo factor into chained ffmpeg atempo filters in [0.5, 2.0]."""
    if not math.isfinite(tempo_factor) or tempo_factor <= 0:
        return ["atempo=1.000000"]
    stages: list[str] = []
    remaining = float(tempo_factor)
    while remaining < 0.5:
        stages.append("atempo=0.500000")
        remaining /= 0.5
    while remaining > 2.0:
        stages.append("atempo=2.000000")
        remaining /= 2.0
    stages.append(f"atempo={remaining:.6f}")
    return stages


def _pitch_shift_wav_numpy(wav_bytes: bytes, semitones: float) -> bytes:
    """Pure-numpy fallback resampling int16 PCM by 2**(semitones/12) preserving WAV header."""
    if not wav_bytes:
        return wav_bytes
    clamped = _clamp_float(semitones, 0.0, PIPER_PITCH_RANGE)
    if abs(clamped) < 0.05:
        return wav_bytes

    try:
        with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
            channels = wf.getnchannels()
            sampwidth = wf.getsampwidth()
            sample_rate = wf.getframerate()
            frames = wf.readframes(wf.getnframes())
    except Exception:
        return wav_bytes

    if not frames or sampwidth != 2 or channels < 1:
        return wav_bytes

    usable = len(frames) - (len(frames) % (2 * channels))
    if usable <= 0:
        return wav_bytes

    samples = np.frombuffer(frames[:usable], dtype=np.int16).astype(np.float64)
    if channels > 1:
        samples = samples.reshape(-1, channels)
    else:
        samples = samples.reshape(-1, 1)

    n_in = samples.shape[0]
    if n_in <= 1:
        return wav_bytes

    pitch_factor = 2.0 ** (clamped / 12.0)
    n_out = max(1, int(round(n_in / pitch_factor)))
    x_old = np.arange(n_in, dtype=np.float64)
    x_new = np.linspace(0.0, float(n_in - 1), n_out, dtype=np.float64)

    out_cols = [np.interp(x_new, x_old, samples[:, ch]) for ch in range(channels)]
    resampled = np.column_stack(out_cols) if channels > 1 else out_cols[0]
    pcm_out = np.clip(np.rint(resampled), -32768.0, 32767.0).astype(np.int16)

    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm_out.tobytes())
    return buf.getvalue()


async def apply_pitch_shift_wav(
    wav_bytes: bytes,
    semitones: float,
    *,
    ffmpeg_bin: str | None = None,
) -> bytes:
    """Shift WAV pitch in semitones via ffmpeg asetrate+atempo, with numpy fallback."""
    if not wav_bytes:
        return wav_bytes
    clamped = _clamp_float(semitones, 0.0, PIPER_PITCH_RANGE)
    if abs(clamped) < 0.05:
        return wav_bytes

    if ffmpeg_bin is not None:
        resolved_ffmpeg = shutil.which(ffmpeg_bin) if ffmpeg_bin else None
    else:
        resolved_ffmpeg = shutil.which("ffmpeg") or (
            "/usr/bin/ffmpeg"
            if os.path.isfile("/usr/bin/ffmpeg") and os.access("/usr/bin/ffmpeg", os.X_OK)
            else None
        )

    if not resolved_ffmpeg:
        return _pitch_shift_wav_numpy(wav_bytes, clamped)

    sample_rate = _parse_wav_sample_rate(wav_bytes, default=22050)
    pitch_factor = 2.0 ** (clamped / 12.0)
    atempo_filters = ",".join(_build_atempo_chain(1.0 / pitch_factor))
    filter_graph = (
        f"asetrate={sample_rate * pitch_factor:.4f},"
        f"aresample={sample_rate},"
        f"{atempo_filters}"
    )

    proc: asyncio.subprocess.Process | None = None
    try:
        proc = await asyncio.create_subprocess_exec(
            resolved_ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "wav",
            "-i",
            "pipe:0",
            "-af",
            filter_graph,
            "-f",
            "wav",
            "pipe:1",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(wav_bytes), timeout=30.0)
        if proc.returncode == 0 and len(stdout) >= 44 and stdout[:4] == b"RIFF":
            return stdout
        logger.warning(
            "ffmpeg pitch shift failed (rc=%s): %s",
            proc.returncode,
            stderr.decode("utf-8", errors="replace").strip(),
        )
    except Exception as exc:
        logger.warning("ffmpeg pitch shift error, falling back to numpy: %s", exc)
        if proc is not None and proc.returncode is None:
            try:
                proc.kill()
                await proc.wait()
            except ProcessLookupError:
                pass

    return _pitch_shift_wav_numpy(wav_bytes, clamped)


def _extract_language_info(
    model_id: str,
    meta: dict[str, Any],
) -> tuple[str, str]:
    """Extract (language_code, language_name) from Piper metadata or model ID."""
    lang_obj = meta.get("language")
    code = ""
    name = ""
    if isinstance(lang_obj, dict):
        code = str(lang_obj.get("code") or "").strip()
        lang_en = str(lang_obj.get("name_english") or lang_obj.get("name_native") or "").strip()
        country_en = str(lang_obj.get("country_english") or "").strip()
        if lang_en and country_en:
            name = f"{lang_en} ({country_en})"
        elif lang_en:
            name = lang_en
    elif isinstance(lang_obj, str):
        code = lang_obj.strip()

    if not code:
        code, _, _, _, _ = _infer_voice_paths(model_id)
    if not name and code:
        name = _LANGUAGE_DISPLAY_NAMES.get(code, code)
    return code, name


def _parse_voices_index(raw_index: Any) -> dict[str, dict[str, Any]]:
    """Normalize a rhasspy/piper-voices voices.json payload into id -> metadata entries."""
    entries: dict[str, dict[str, Any]] = {}
    if isinstance(raw_index, dict):
        items = raw_index.items()
    elif isinstance(raw_index, list):
        items = [
            (str(item.get("key") or item.get("id") or ""), item)
            for item in raw_index
            if isinstance(item, dict)
        ]
    else:
        return entries

    for key, entry in items:
        if not isinstance(entry, dict):
            continue
        vid = str(entry.get("key") or entry.get("id") or key or "").strip()
        if not vid:
            continue

        lang_code, lang_name = _extract_language_info(vid, entry)
        _, inferred_speaker, inferred_quality, default_onnx_rel, default_json_rel = _infer_voice_paths(vid)
        quality = str(entry.get("quality") or inferred_quality).strip()
        raw_name = str(entry.get("name") or inferred_speaker or vid).strip()
        display_name = raw_name.replace("_", " ").title() if raw_name.islower() else raw_name

        onnx_rel = str(entry.get("onnx_rel") or "").strip()
        json_rel = str(entry.get("json_rel") or "").strip()
        files = entry.get("files")
        if isinstance(files, dict):
            for file_rel in files:
                rel_str = str(file_rel).strip()
                if rel_str.endswith(".onnx") and not onnx_rel:
                    onnx_rel = rel_str
                elif rel_str.endswith(".onnx.json") and not json_rel:
                    json_rel = rel_str
        if not onnx_rel:
            onnx_rel = default_onnx_rel
        if not json_rel:
            json_rel = f"{onnx_rel}.json" if onnx_rel else default_json_rel

        entries[vid] = {
            "id": vid,
            "name": display_name,
            "language": lang_code,
            "language_name": lang_name,
            "quality": quality,
            "onnx_rel": onnx_rel,
            "json_rel": json_rel,
            "num_speakers": max(1, int(entry.get("num_speakers") or 1)),
        }
    return entries


class PiperTTSClient:
    """Async Piper local TTS client supporting model management and 3-tier synthesis."""

    def __init__(
        self,
        *,
        models_dir: Path | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._models_dir = Path(models_dir).expanduser() if models_dir is not None else None
        self._http_client = http_client
        self._owns_client = http_client is None
        self._download_locks: dict[str, asyncio.Lock] = {}

        self._cached_piper_key: tuple[str, int] | None = None
        self._cached_piper_voice: Any = None
        self._cached_onnx_key: tuple[str, int] | None = None
        self._cached_onnx_bundle: tuple[Any, dict[str, Any]] | None = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(timeout=300.0, follow_redirects=True)
            self._owns_client = True
        return self._http_client

    async def close(self) -> None:
        """Close owned HTTP client and release cached Piper/ONNX models."""
        if self._owns_client and self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None
        self._cached_piper_key = None
        self._cached_piper_voice = None
        self._cached_onnx_key = None
        self._cached_onnx_bundle = None

    def resolve_models_dir(self, override: str | Path | None = None) -> Path:
        """Resolve Piper models directory from override, instance default, or data_dir/piper."""
        if isinstance(override, Path):
            return override.expanduser()
        if isinstance(override, str) and override.strip():
            return Path(override.strip()).expanduser()
        if self._models_dir is not None:
            return self._models_dir
        return resolve_data_dir() / "piper"

    def _validate_model_id(self, model_id: str) -> str:
        """Validate a Piper voice identifier against path traversal or empty values."""
        if not isinstance(model_id, str):
            raise ValueError("Invalid Piper model ID")
        cleaned = model_id.strip()
        if (
            not cleaned
            or "\x00" in cleaned
            or ".." in cleaned
            or "/" in cleaned
            or "\\" in cleaned
        ):
            raise ValueError(f"Invalid Piper model ID: {model_id!r}")
        return cleaned

    def detect_backend(self) -> str:
        """Detect highest-priority local Piper synthesis backend available on host."""
        if _import_piper_voice() is not None:
            return "piper_python"
        if shutil.which("piper"):
            return "piper_cli"
        if _has_onnxruntime() and _find_espeak_ng():
            return "onnx_espeak"
        return "unavailable"

    def _candidate_model_dirs(self, models_dir: str | Path | None = None) -> list[Path]:
        primary = self.resolve_models_dir(models_dir)
        fallback = Path.home() / ".local" / "share" / "piper"
        dirs = [primary]
        if fallback.expanduser() != primary.expanduser():
            dirs.append(fallback)
        return dirs

    def scan_local_models(
        self, models_dir: str | Path | None = None
    ) -> dict[str, dict[str, Any]]:
        """Scan primary and fallback Piper directories for installed .onnx + JSON models."""
        found: dict[str, dict[str, Any]] = {}
        for base_dir in self._candidate_model_dirs(models_dir):
            if not base_dir.is_dir():
                continue
            for onnx_path in sorted(base_dir.rglob("*.onnx")):
                if not onnx_path.is_file():
                    continue
                json_path = _find_sibling_json(onnx_path)
                if json_path is None:
                    continue
                meta = _read_json_dict(json_path)
                if meta is None:
                    continue

                model_id = onnx_path.stem
                if model_id in found:
                    continue

                lang_code, lang_name = _extract_language_info(model_id, meta)
                _, speaker, quality, _, _ = _infer_voice_paths(model_id)
                audio_meta = meta.get("audio") if isinstance(meta.get("audio"), dict) else {}
                espeak_meta = meta.get("espeak") if isinstance(meta.get("espeak"), dict) else {}

                display_name = speaker.replace("_", " ").title() if speaker.islower() else speaker
                found[model_id] = {
                    "id": model_id,
                    "name": display_name or model_id,
                    "path": str(onnx_path),
                    "config_path": str(json_path),
                    "language": lang_code,
                    "language_name": lang_name,
                    "quality": str(audio_meta.get("quality") or quality),
                    "espeak_voice": str(espeak_meta.get("voice") or "").strip(),
                    "sample_rate": int(audio_meta.get("sample_rate") or 22050),
                    "num_speakers": max(1, int(meta.get("num_speakers") or 1)),
                    "installed": True,
                }
        return found

    async def _http_get_bytes(self, url: str) -> bytes:
        client = self._get_client()
        kwargs: dict[str, Any] = {}
        if _accepts_kwarg(client.get, "follow_redirects"):
            kwargs["follow_redirects"] = True
        if _accepts_kwarg(client.get, "timeout"):
            kwargs["timeout"] = 60.0
        resp = await client.get(url, **kwargs)
        resp.raise_for_status()
        return bytes(resp.content)

    async def _http_download_file(self, url: str, dest_tmp: Path) -> None:
        client = self._get_client()
        stream_fn = getattr(client, "stream", None)
        if callable(stream_fn):
            stream_kwargs: dict[str, Any] = {}
            if _accepts_kwarg(stream_fn, "follow_redirects"):
                stream_kwargs["follow_redirects"] = True
            if _accepts_kwarg(stream_fn, "timeout"):
                stream_kwargs["timeout"] = 300.0
            stream_cm = stream_fn("GET", url, **stream_kwargs)
            if hasattr(stream_cm, "__aenter__"):
                async with stream_cm as resp:
                    resp.raise_for_status()
                    with open(dest_tmp, "wb") as handle:
                        if hasattr(resp, "aiter_bytes"):
                            async for chunk in resp.aiter_bytes(chunk_size=65536):
                                if chunk:
                                    handle.write(chunk)
                        else:
                            handle.write(resp.content)
                return

        get_kwargs: dict[str, Any] = {}
        if _accepts_kwarg(client.get, "follow_redirects"):
            get_kwargs["follow_redirects"] = True
        if _accepts_kwarg(client.get, "timeout"):
            get_kwargs["timeout"] = 300.0
        resp = await client.get(url, **get_kwargs)
        resp.raise_for_status()
        dest_tmp.write_bytes(resp.content)

    def _read_cached_voices_index(self, target_dir: Path) -> dict[str, dict[str, Any]]:
        cache_path = target_dir / "voices.json"
        if not cache_path.is_file():
            return {}
        try:
            raw = json.loads(cache_path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        return _parse_voices_index(raw)

    async def _fetch_and_cache_voices_index(self, target_dir: Path) -> dict[str, dict[str, Any]]:
        try:
            payload = await self._http_get_bytes(PIPER_VOICES_INDEX_URL)
            parsed = json.loads(payload.decode("utf-8"))
            entries = _parse_voices_index(parsed)
            if entries:
                _write_bytes_atomic(target_dir / "voices.json", payload)
            return entries
        except Exception as exc:
            logger.warning("Failed fetching Piper voices index: %s", exc)
            return self._read_cached_voices_index(target_dir)

    async def list_voices(
        self,
        *,
        models_dir: str | Path | None = None,
        refresh: bool = False,
        language: str = "",
    ) -> list[dict[str, Any]]:
        """List curated, cached/fetched catalog, and locally installed Piper voices."""
        target_dir = self.resolve_models_dir(models_dir)
        local_models = self.scan_local_models(target_dir)
        index_entries = (
            await self._fetch_and_cache_voices_index(target_dir)
            if refresh
            else self._read_cached_voices_index(target_dir)
        )

        merged: dict[str, dict[str, Any]] = {}
        for curated in CURATED_PIPER_VOICES:
            merged[curated["id"]] = {
                "id": curated["id"],
                "name": curated["name"],
                "language": curated["language"],
                "language_name": curated["language_name"],
                "quality": curated["quality"],
                "num_speakers": curated.get("num_speakers", 1),
                "installed": False,
                "path": "",
            }

        for vid, entry in index_entries.items():
            existing = merged.get(vid, {})
            merged[vid] = {
                "id": vid,
                "name": existing.get("name") or entry["name"],
                "language": existing.get("language") or entry["language"],
                "language_name": existing.get("language_name") or entry["language_name"],
                "quality": existing.get("quality") or entry["quality"],
                "num_speakers": entry.get("num_speakers") or existing.get("num_speakers", 1),
                "installed": False,
                "path": "",
            }

        for vid, local in local_models.items():
            existing = merged.get(vid, {})
            merged[vid] = {
                "id": vid,
                "name": existing.get("name") or local["name"],
                "language": local.get("language") or existing.get("language", ""),
                "language_name": local.get("language_name") or existing.get("language_name", ""),
                "quality": existing.get("quality") or local.get("quality", ""),
                "num_speakers": local.get("num_speakers") or existing.get("num_speakers", 1),
                "installed": True,
                "path": local["path"],
            }

        voices = list(merged.values())
        lang_filter = (language or "").strip().lower().replace("-", "_")
        if lang_filter:
            voices = [
                v
                for v in voices
                if str(v.get("language") or "").lower().replace("-", "_").startswith(lang_filter)
            ]

        voices.sort(
            key=lambda v: (
                not bool(v.get("installed")),
                str(v.get("language") or ""),
                str(v.get("name") or "").lower(),
                str(v.get("id") or ""),
            )
        )
        return voices

    async def _resolve_download_rel_paths(
        self, clean_id: str, target_dir: Path
    ) -> tuple[str, str]:
        cached = self._read_cached_voices_index(target_dir)
        if clean_id in cached:
            entry = cached[clean_id]
            return entry["onnx_rel"], entry["json_rel"]

        for curated in CURATED_PIPER_VOICES:
            if curated["id"] == clean_id:
                return str(curated["onnx_rel"]), str(curated["json_rel"])

        live = await self._fetch_and_cache_voices_index(target_dir)
        if clean_id in live:
            entry = live[clean_id]
            return entry["onnx_rel"], entry["json_rel"]

        _, _, _, onnx_rel, json_rel = _infer_voice_paths(clean_id)
        return onnx_rel, json_rel

    async def download_model(
        self,
        model_id: str,
        *,
        models_dir: str | Path | None = None,
    ) -> dict[str, Any]:
        """Download a Piper .onnx model and .onnx.json config atomically into models_dir."""
        clean_id = self._validate_model_id(model_id)
        target_dir = self.resolve_models_dir(models_dir)
        target_dir.mkdir(parents=True, exist_ok=True)
        resolved_dir = target_dir.resolve()

        onnx_path = (resolved_dir / f"{clean_id}.onnx").resolve()
        json_path = (resolved_dir / f"{clean_id}.onnx.json").resolve()
        if onnx_path.parent != resolved_dir or json_path.parent != resolved_dir:
            raise ValueError(f"Invalid Piper model ID: {model_id!r}")

        lock_key = f"{resolved_dir}::{clean_id}"
        lock = self._download_locks.setdefault(lock_key, asyncio.Lock())
        async with lock:
            if (
                onnx_path.is_file()
                and onnx_path.stat().st_size > 0
                and json_path.is_file()
                and _read_json_dict(json_path) is not None
            ):
                return {
                    "ok": True,
                    "model": clean_id,
                    "path": str(onnx_path),
                    "config_path": str(json_path),
                    "installed": True,
                }

            onnx_rel, json_rel = await self._resolve_download_rel_paths(clean_id, resolved_dir)
            onnx_url = (
                f"{PIPER_VOICES_BASE_URL}/{urllib.parse.quote(onnx_rel.lstrip('/'), safe='/')}"
            )
            json_url = (
                f"{PIPER_VOICES_BASE_URL}/{urllib.parse.quote(json_rel.lstrip('/'), safe='/')}"
            )

            onnx_tmp = resolved_dir / f".{clean_id}.onnx.tmp"
            json_tmp = resolved_dir / f".{clean_id}.onnx.json.tmp"
            try:
                await self._http_download_file(json_url, json_tmp)
                if _read_json_dict(json_tmp) is None:
                    raise ValueError(f"Downloaded Piper config for {clean_id!r} is invalid JSON")

                await self._http_download_file(onnx_url, onnx_tmp)
                if not onnx_tmp.is_file() or onnx_tmp.stat().st_size <= 0:
                    raise ValueError(f"Downloaded Piper ONNX model for {clean_id!r} is empty")

                os.replace(json_tmp, json_path)
                os.replace(onnx_tmp, onnx_path)
            finally:
                onnx_tmp.unlink(missing_ok=True)
                json_tmp.unlink(missing_ok=True)

        return {
            "ok": True,
            "model": clean_id,
            "path": str(onnx_path),
            "config_path": str(json_path),
            "installed": True,
        }

    def resolve_model_files(
        self,
        model: str,
        models_dir: str | Path | None = None,
    ) -> tuple[Path, Path] | None:
        """Resolve (onnx_path, json_path) for a model ID or explicit .onnx path."""
        raw = (model or "").strip()
        if not raw or "\x00" in raw:
            if not raw:
                raise ValueError("Piper model identifier is required")
            raise ValueError(f"Invalid Piper model identifier: {model!r}")

        is_explicit = "/" in raw or "\\" in raw or raw.endswith(".onnx")
        if is_explicit:
            if ".." in raw:
                return None
            candidate = Path(raw).expanduser()
            if not candidate.is_file():
                return None
            sib = _find_sibling_json(candidate)
            return (candidate, sib) if sib is not None else None

        clean_id = self._validate_model_id(raw)
        for base_dir in self._candidate_model_dirs(models_dir):
            candidate = base_dir / f"{clean_id}.onnx"
            if candidate.is_file():
                sib = _find_sibling_json(candidate)
                if sib is not None:
                    return candidate, sib
        return None

    def _get_piper_voice(self, onnx_path: Path, json_path: Path) -> Any:
        key = (str(onnx_path), onnx_path.stat().st_mtime_ns)
        if self._cached_piper_key == key and self._cached_piper_voice is not None:
            return self._cached_piper_voice

        voice_cls = _import_piper_voice()
        if voice_cls is None:
            raise RuntimeError("piper Python package is unavailable")
        if _accepts_kwarg(voice_cls.load, "config_path"):
            voice = voice_cls.load(str(onnx_path), config_path=str(json_path))
        else:
            voice = voice_cls.load(str(onnx_path), str(json_path))
        self._cached_piper_key = key
        self._cached_piper_voice = voice
        return voice

    def _synth_piper_python_sync(
        self,
        text: str,
        onnx_path: Path,
        json_path: Path,
        length_scale: float,
        noise_scale: float,
        noise_w: float,
        speaker_id: int | None,
    ) -> bytes:
        voice = self._get_piper_voice(onnx_path, json_path)
        model_cfg = _read_json_dict(json_path) or {}
        num_speakers = max(1, int(model_cfg.get("num_speakers") or 1))
        clamped_sid = (
            max(0, min(num_speakers - 1, int(speaker_id)))
            if speaker_id is not None and num_speakers > 1
            else (0 if num_speakers > 1 else None)
        )

        syn_config = None
        try:
            import piper  # type: ignore[import-not-found]

            syn_cls = getattr(piper, "SynthesisConfig", None)
            if syn_cls is None:
                from piper.config import SynthesisConfig as syn_cls  # type: ignore[import-not-found]
            syn_kwargs: dict[str, Any] = {
                "speaker_id": clamped_sid,
                "length_scale": length_scale,
                "noise_scale": noise_scale,
                "noise_w": noise_w,
            }
            filtered = {k: v for k, v in syn_kwargs.items() if _accepts_kwarg(syn_cls, k)}
            syn_config = syn_cls(**filtered)
        except Exception:
            syn_config = None

        buf = io.BytesIO()
        legacy_opts = {
            "speaker_id": clamped_sid,
            "length_scale": length_scale,
            "noise_scale": noise_scale,
            "noise_w": noise_w,
        }

        def _invoke_wav_writer(writer_fn: Any) -> bytes:
            with wave.open(buf, "wb") as wf:
                kwargs: dict[str, Any] = {}
                if syn_config is not None and _accepts_kwarg(writer_fn, "syn_config"):
                    kwargs["syn_config"] = syn_config
                else:
                    kwargs = {
                        k: v for k, v in legacy_opts.items() if _accepts_kwarg(writer_fn, k)
                    }
                writer_fn(text, wf, **kwargs)
                try:
                    wf.getnchannels()
                except wave.Error:
                    wf.setnchannels(1)
                    wf.setsampwidth(2)
                    wf.setframerate(22050)
            return buf.getvalue()

        synth_wav_fn = getattr(voice, "synthesize_wav", None)
        if callable(synth_wav_fn):
            return _invoke_wav_writer(synth_wav_fn)

        synth_fn = voice.synthesize
        sig = inspect.signature(synth_fn)
        if "wav_file" in sig.parameters or (
            "syn_config" not in sig.parameters and len(sig.parameters) >= 2
        ):
            return _invoke_wav_writer(synth_fn)

        call_kwargs = {"syn_config": syn_config} if syn_config is not None else {}
        chunks = list(synth_fn(text, **call_kwargs))
        default_rate = int((model_cfg.get("audio") or {}).get("sample_rate") or 22050)
        sample_rate = (
            int(getattr(chunks[0], "sample_rate", default_rate)) if chunks else default_rate
        )
        channels = int(getattr(chunks[0], "sample_channels", 1)) if chunks else 1
        sampwidth = int(getattr(chunks[0], "sample_width", 2)) if chunks else 2
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(channels)
            wf.setsampwidth(sampwidth)
            wf.setframerate(sample_rate)
            for chunk in chunks:
                audio_bytes = getattr(chunk, "audio_int16_bytes", b"")
                if audio_bytes:
                    wf.writeframes(audio_bytes)
        return buf.getvalue()

    async def _synth_piper_cli(
        self,
        text: str,
        onnx_path: Path,
        json_path: Path,
        length_scale: float,
        noise_scale: float,
        noise_w: float,
        speaker_id: int | None,
    ) -> bytes:
        piper_bin = shutil.which("piper")
        if not piper_bin:
            raise RuntimeError("piper CLI binary not found")

        cmd = [
            piper_bin,
            "--model",
            str(onnx_path),
            "--config",
            str(json_path),
            "--length_scale",
            str(length_scale),
            "--noise_scale",
            str(noise_scale),
            "--noise_w",
            str(noise_w),
            "--output_file",
            "-",
        ]
        if speaker_id is not None:
            cmd.extend(["--speaker", str(max(0, int(speaker_id)))])

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(text.encode("utf-8")), timeout=60.0
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise RuntimeError("piper CLI synthesis timed out")

        if proc.returncode != 0 or not stdout.startswith(b"RIFF"):
            raise RuntimeError(
                f"piper CLI failed ({proc.returncode}): "
                f"{stderr.decode('utf-8', errors='replace').strip()}"
            )
        return stdout

    def _get_onnx_bundle(self, onnx_path: Path, json_path: Path) -> tuple[Any, dict[str, Any]]:
        key = (str(onnx_path), onnx_path.stat().st_mtime_ns)
        if self._cached_onnx_key == key and self._cached_onnx_bundle is not None:
            return self._cached_onnx_bundle

        import onnxruntime as ort  # type: ignore[import-not-found]

        model_cfg = _read_json_dict(json_path)
        if model_cfg is None:
            raise ValueError(f"Invalid Piper model config: {json_path}")

        sess_opts = ort.SessionOptions()
        sess_opts.intra_op_num_threads = min(4, os.cpu_count() or 2)

        init_kwargs: dict[str, Any] = {}
        if _accepts_kwarg(ort.InferenceSession, "sess_options"):
            init_kwargs["sess_options"] = sess_opts
        if _accepts_kwarg(ort.InferenceSession, "providers"):
            init_kwargs["providers"] = ["CPUExecutionProvider"]

        session = ort.InferenceSession(str(onnx_path), **init_kwargs)
        self._cached_onnx_key = key
        self._cached_onnx_bundle = (session, model_cfg)
        return self._cached_onnx_bundle

    async def _phonemize_espeak(self, text: str, espeak_voice: str) -> str:
        espeak_bin = _find_espeak_ng()
        if not espeak_bin:
            raise RuntimeError("espeak-ng binary not found")

        clauses = re.findall(r"[^.!?;:,]+[.!?;:,]*", text) or [text]
        parts: list[str] = []
        for clause in clauses:
            stripped = clause.strip()
            if not stripped:
                continue
            punct_match = re.search(r"([.!?;:,]+)$", stripped)
            if punct_match:
                trail_punct = punct_match.group(1)
                body = stripped[: punct_match.start()].strip()
            else:
                trail_punct = ""
                body = stripped

            if not body:
                parts.append(trail_punct)
                continue

            proc = await asyncio.create_subprocess_exec(
                espeak_bin,
                "-v",
                espeak_voice or "en-us",
                "-q",
                "--ipa",
                "--stdin",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(body.encode("utf-8")),
                    timeout=15.0,
                )
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                raise RuntimeError("espeak-ng phonemization timed out")

            if proc.returncode != 0:
                raise RuntimeError(
                    f"espeak-ng failed ({proc.returncode}): "
                    f"{stderr.decode('utf-8', errors='replace').strip()}"
                )
            ipa = " ".join(stdout.decode("utf-8", errors="replace").splitlines()).strip()
            parts.append(f"{ipa}{trail_punct}" if ipa else trail_punct)

        return " ".join(part for part in parts if part)

    def _phonemes_to_ids(
        self, phoneme_str: str, phoneme_id_map: dict[str, list[int]]
    ) -> list[int]:
        bos_ids = [int(i) for i in phoneme_id_map.get("^", [])]
        eos_ids = [int(i) for i in phoneme_id_map.get("$", [])]
        pad_ids = [int(i) for i in phoneme_id_map.get("_", [])]

        ids: list[int] = list(bos_ids)
        if pad_ids:
            ids.extend(pad_ids)

        for ch in unicodedata.normalize("NFD", phoneme_str):
            mapped = phoneme_id_map.get(ch)
            if not mapped:
                continue
            ids.extend(int(i) for i in mapped)
            if pad_ids:
                ids.extend(pad_ids)

        ids.extend(eos_ids)
        return ids

    def _run_onnx_inference_sync(
        self,
        session: Any,
        model_cfg: dict[str, Any],
        phoneme_ids: list[int],
        length_scale: float,
        noise_scale: float,
        noise_w: float,
        speaker_id: int | None,
    ) -> bytes:
        inputs: dict[str, Any] = {
            "input": np.array([phoneme_ids], dtype=np.int64),
            "input_lengths": np.array([len(phoneme_ids)], dtype=np.int64),
            "scales": np.array([noise_scale, length_scale, noise_w], dtype=np.float32),
        }
        num_speakers = max(1, int(model_cfg.get("num_speakers") or 1))
        if num_speakers > 1:
            sid_val = (
                0
                if speaker_id is None
                else max(0, min(num_speakers - 1, int(speaker_id)))
            )
            inputs["sid"] = np.array([sid_val], dtype=np.int64)

        outputs = session.run(None, inputs)
        audio = np.squeeze(np.asarray(outputs[0], dtype=np.float32))
        if audio.ndim == 0:
            audio = np.zeros(0, dtype=np.float32)

        if audio.size > 0:
            max_abs = float(np.max(np.abs(audio)))
            if max_abs > 1.0:
                audio = audio / max_abs
        audio = np.clip(audio, -1.0, 1.0)
        pcm_int16 = np.rint(audio * 32767.0).astype(np.int16)

        sample_rate = int((model_cfg.get("audio") or {}).get("sample_rate") or 22050)
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sample_rate)
            wf.writeframes(pcm_int16.tobytes())
        return buf.getvalue()

    async def _synth_onnx_espeak(
        self,
        text: str,
        onnx_path: Path,
        json_path: Path,
        length_scale: float,
        noise_scale: float,
        noise_w: float,
        speaker_id: int | None,
    ) -> bytes:
        session, model_cfg = await asyncio.to_thread(
            self._get_onnx_bundle, onnx_path, json_path
        )
        phoneme_id_map = model_cfg.get("phoneme_id_map")
        if not isinstance(phoneme_id_map, dict):
            raise ValueError(f"Piper config missing phoneme_id_map: {json_path}")

        phoneme_type = str(model_cfg.get("phoneme_type") or "espeak").strip().lower()
        if phoneme_type == "text":
            phoneme_str = unicodedata.normalize("NFD", text)
        else:
            espeak_meta = model_cfg.get("espeak") if isinstance(model_cfg.get("espeak"), dict) else {}
            espeak_voice = str(espeak_meta.get("voice") or "").strip()
            if not espeak_voice:
                lang_code, _ = _extract_language_info(onnx_path.stem, model_cfg)
                espeak_voice = lang_code.lower().replace("_", "-") or "en-us"
            phoneme_str = await self._phonemize_espeak(text, espeak_voice)

        phoneme_ids = self._phonemes_to_ids(phoneme_str, phoneme_id_map)
        if not phoneme_ids:
            phoneme_ids = [0]

        return await asyncio.to_thread(
            self._run_onnx_inference_sync,
            session,
            model_cfg,
            phoneme_ids,
            length_scale,
            noise_scale,
            noise_w,
            speaker_id,
        )

    async def synthesize(
        self,
        text: str,
        *,
        model: str = "en_US-lessac-medium",
        models_dir: str = "",
        speed: float = 1.0,
        pitch: float = 0.0,
        noise_scale: float = 0.667,
        speaker_id: int | None = None,
        auto_download: bool = True,
    ) -> tuple[bytes, str]:
        """Synthesize text locally with Piper and apply optional pitch shifting, returning (wav_bytes, 'wav')."""
        cleaned = (text or "").strip()
        if not cleaned:
            raise ValueError("Text is required")

        raw_model = (model or "en_US-lessac-medium").strip()
        speed_val = _clamp_float(speed, 1.0, PIPER_SPEED_RANGE)
        pitch_val = _clamp_float(pitch, 0.0, PIPER_PITCH_RANGE)
        noise_scale_val = _clamp_float(noise_scale, 0.667, PIPER_NOISE_SCALE_RANGE)
        length_scale = round(1.0 / speed_val, 4)

        is_explicit_path = "/" in raw_model or "\\" in raw_model or raw_model.endswith(".onnx")
        resolved = self.resolve_model_files(raw_model, models_dir=models_dir)
        if resolved is None and auto_download and not is_explicit_path:
            await self.download_model(raw_model, models_dir=models_dir or None)
            resolved = self.resolve_model_files(raw_model, models_dir=models_dir)
        if resolved is None:
            raise FileNotFoundError(f"Piper voice model not found: {raw_model}")

        onnx_path, json_path = resolved
        model_cfg = _read_json_dict(json_path) or {}
        inf_cfg = model_cfg.get("inference") if isinstance(model_cfg.get("inference"), dict) else {}
        noise_w = float(inf_cfg.get("noise_w") or 0.8)

        backend = self.detect_backend()
        if backend == "piper_python":
            wav_bytes = await asyncio.to_thread(
                self._synth_piper_python_sync,
                cleaned,
                onnx_path,
                json_path,
                length_scale,
                noise_scale_val,
                noise_w,
                speaker_id,
            )
        elif backend == "piper_cli":
            wav_bytes = await self._synth_piper_cli(
                cleaned,
                onnx_path,
                json_path,
                length_scale,
                noise_scale_val,
                noise_w,
                speaker_id,
            )
        elif backend == "onnx_espeak":
            wav_bytes = await self._synth_onnx_espeak(
                cleaned,
                onnx_path,
                json_path,
                length_scale,
                noise_scale_val,
                noise_w,
                speaker_id,
            )
        else:
            raise RuntimeError(
                "No local Piper TTS backend available (install piper-tts or onnxruntime + espeak-ng)"
            )

        wav_bytes = await apply_pitch_shift_wav(wav_bytes, pitch_val)
        return wav_bytes, "wav"
