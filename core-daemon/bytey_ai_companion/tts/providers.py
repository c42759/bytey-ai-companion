"""Async TTS provider clients for Kokoro HTTP and ElevenLabs API."""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

DEFAULT_KOKORO_VOICES = [
    "af_heart",
    "af_bella",
    "af_nicole",
    "af_sarah",
    "am_adam",
    "am_michael",
    "bf_emma",
    "bm_george",
]

ELEVENLABS_BASE_URL = "https://api.elevenlabs.io/v1"


def _normalize_kokoro_voices(raw: Any) -> list[dict[str, Any]]:
    items = raw.get("voices", raw) if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        return []
    voices: list[dict[str, Any]] = []
    for entry in items:
        if isinstance(entry, str) and entry.strip():
            voices.append({"id": entry.strip(), "name": entry.strip()})
        elif isinstance(entry, dict):
            vid = str(entry.get("id") or entry.get("voice_id") or entry.get("name") or "").strip()
            name = str(entry.get("name") or vid).strip()
            if vid:
                voices.append({**entry, "id": vid, "name": name})
    return voices


def _normalize_kokoro_base_url(base_url: str) -> str:
    """Normalize Kokoro base URL so /v1 is included whether user omitted or included it."""
    base = (base_url or "").strip().rstrip("/")
    if not base:
        return ""
    for suffix in ("/audio/speech", "/audio/voices"):
        if base.lower().endswith(suffix):
            base = base[: -len(suffix)].rstrip("/")
    if not base.lower().endswith("/v1"):
        base = f"{base}/v1"
    return base


class KokoroTTSClient:
    """Client for OpenAI-compatible Kokoro HTTP TTS instances."""

    def __init__(self, http_client: httpx.AsyncClient | None = None) -> None:
        self._http_client = http_client
        self._owns_client = http_client is None

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

    async def list_voices(
        self, base_url: str, api_key: str | None = None
    ) -> list[dict[str, Any]]:
        """Fetch voices from GET {base_url}/audio/voices with DEFAULT_KOKORO_VOICES fallback."""
        fallback = [{"id": v, "name": v} for v in DEFAULT_KOKORO_VOICES]
        base = _normalize_kokoro_base_url(base_url)
        if not base:
            return fallback

        headers: dict[str, str] = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        client = self._get_client()
        try:
            resp = await client.get(f"{base}/audio/voices", headers=headers)
            resp.raise_for_status()
            voices = _normalize_kokoro_voices(resp.json())
            return voices if voices else fallback
        except Exception as exc:
            logger.warning("Kokoro list_voices failed, using defaults: %s", exc)
            return fallback

    async def synthesize(
        self,
        text: str,
        base_url: str,
        voice: str = "af_heart",
        speed: float = 1.0,
        api_key: str | None = None,
    ) -> tuple[bytes, str]:
        """Synthesize text via POST {base_url}/audio/speech requesting WAV audio."""
        base = _normalize_kokoro_base_url(base_url)
        if not base:
            raise ValueError("Kokoro base_url is required")

        headers: dict[str, str] = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        payload = {
            "model": "kokoro",
            "input": text,
            "voice": voice or "af_heart",
            "speed": float(speed),
            "response_format": "wav",
        }
        client = self._get_client()
        resp = await client.post(f"{base}/audio/speech", json=payload, headers=headers)
        resp.raise_for_status()
        return resp.content, "wav"


class ElevenLabsTTSClient:
    """Client for ElevenLabs voice discovery and low-latency PCM synthesis."""

    def __init__(self, http_client: httpx.AsyncClient | None = None) -> None:
        self._http_client = http_client
        self._owns_client = http_client is None

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

    async def list_voices(self, api_key: str) -> list[dict[str, Any]]:
        """Fetch available voices from GET https://api.elevenlabs.io/v1/voices."""
        headers: dict[str, str] = {}
        if api_key:
            headers["xi-api-key"] = api_key

        client = self._get_client()
        resp = await client.get(f"{ELEVENLABS_BASE_URL}/voices", headers=headers)
        resp.raise_for_status()
        raw_voices = resp.json().get("voices", [])
        voices: list[dict[str, Any]] = []
        for entry in raw_voices:
            if not isinstance(entry, dict):
                continue
            vid = str(entry.get("voice_id") or entry.get("id") or "").strip()
            name = str(entry.get("name") or vid).strip()
            if vid:
                voices.append({**entry, "id": vid, "name": name})
        return voices

    async def synthesize(
        self,
        text: str,
        voice_id: str,
        model_id: str = "eleven_flash_v2_5",
        api_key: str = "",
        stability: float | None = None,
        similarity_boost: float | None = None,
    ) -> tuple[bytes, str]:
        """Synthesize text via ElevenLabs returning raw 24kHz 16-bit mono PCM bytes."""
        if not voice_id:
            raise ValueError("ElevenLabs voice_id is required")

        headers: dict[str, str] = {"Content-Type": "application/json"}
        if api_key:
            headers["xi-api-key"] = api_key

        payload: dict[str, Any] = {
            "text": text,
            "model_id": model_id or "eleven_flash_v2_5",
        }
        if stability is not None or similarity_boost is not None:
            voice_settings: dict[str, float] = {}
            if stability is not None:
                voice_settings["stability"] = float(stability)
            if similarity_boost is not None:
                voice_settings["similarity_boost"] = float(similarity_boost)
            payload["voice_settings"] = voice_settings

        url = f"{ELEVENLABS_BASE_URL}/text-to-speech/{voice_id}?output_format=pcm_24000"
        client = self._get_client()
        resp = await client.post(url, json=payload, headers=headers)
        resp.raise_for_status()
        return resp.content, "pcm_24000"
