"""Unified async LLM provider router, model discovery, tool detection, and streaming."""

from __future__ import annotations

import asyncio
import inspect
import json
import re
from collections.abc import Callable
from typing import Any

import httpx

try:
    from google import genai as _google_genai
    from google.genai import types as _genai_types
except ImportError:
    _google_genai = None  # type: ignore[assignment]
    _genai_types = None  # type: ignore[assignment]

from .emotions import EmotionStreamParser

SUPPORTED_PROVIDERS: tuple[str, ...] = (
    "gemini",
    "openai",
    "ollama",
    "anthropic",
    "custom",
)

DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"
DEFAULT_OLLAMA_BASE_URL = "http://localhost:11434"
DEFAULT_GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
DEFAULT_ANTHROPIC_BASE_URL = "https://api.anthropic.com"
ANTHROPIC_VERSION = "2023-06-01"

ANTHROPIC_FALLBACK_MODELS: tuple[dict[str, Any], ...] = (
    {"id": "claude-sonnet-4-20250514", "name": "Claude Sonnet 4", "supports_tools": True},
    {"id": "claude-opus-4-20250514", "name": "Claude Opus 4", "supports_tools": True},
    {"id": "claude-3-7-sonnet-20250219", "name": "Claude 3.7 Sonnet", "supports_tools": True},
    {"id": "claude-3-5-sonnet-20241022", "name": "Claude 3.5 Sonnet", "supports_tools": True},
    {"id": "claude-3-5-haiku-20241022", "name": "Claude 3.5 Haiku", "supports_tools": True},
)

_NON_TOOL_TOKENS: tuple[str, ...] = (
    "embed",
    "embedding",
    "tts",
    "whisper",
    "transcribe",
    "dall-e",
    "imagen",
    "veo",
    "moderation",
    "sora",
    "babbage",
    "davinci",
    "aqa",
    "rerank",
)

_OPENAI_TOOL_MODEL_RE = re.compile(
    r"(?:^|/)(?:gpt-3\.5|gpt-4|gpt-5|chatgpt|o1(?!-mini|-preview)|o3|o4)",
    re.IGNORECASE,
)


def _normalize_ollama_base(base_url: str) -> str:
    base = (base_url or DEFAULT_OLLAMA_BASE_URL).strip().rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3]
    return base or DEFAULT_OLLAMA_BASE_URL


def _safe_json_loads(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not raw or not isinstance(raw, str):
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _stringify_content(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    return json.dumps(content, ensure_ascii=False)


def _extract_tool_def(tool: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
    fn = tool.get("function") if isinstance(tool.get("function"), dict) else tool
    name = str(fn.get("name", "")).strip()
    description = str(fn.get("description") or "")
    params = fn.get("parameters") or fn.get("inputSchema") or {"type": "object", "properties": {}}
    if not isinstance(params, dict):
        params = {"type": "object", "properties": {}}
    return name, description, params


def _extract_message_tool_calls(msg: dict[str, Any]) -> list[dict[str, Any]]:
    raw_calls = msg.get("tool_calls") or []
    normalized: list[dict[str, Any]] = []
    for idx, tc in enumerate(raw_calls):
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function") if isinstance(tc.get("function"), dict) else tc
        name = str(fn.get("name") or tc.get("name") or "").strip()
        if not name:
            continue
        args = _safe_json_loads(fn.get("arguments") if "arguments" in fn else tc.get("arguments"))
        tc_id = str(tc.get("id") or f"call_{idx}")
        normalized.append({"id": tc_id, "name": name, "arguments": args})
    return normalized


async def _invoke_callback(cb: Callable[..., Any] | None, *args: Any) -> None:
    if cb is None:
        return
    result = cb(*args)
    if inspect.isawaitable(result):
        await result


def _heuristic_tool_support(provider: str, model: str) -> bool:
    prov = (provider or "").strip().lower()
    mod = (model or "").strip().lower()
    if not mod:
        return False

    if any(token in mod for token in _NON_TOOL_TOKENS):
        return False

    if prov == "gemini":
        if "vision" in mod and "gemini-1.0" in mod or mod.endswith("-vision"):
            return False
        return "gemini" in mod

    if prov == "openai":
        return bool(_OPENAI_TOOL_MODEL_RE.search(mod))

    if prov == "anthropic":
        if mod.startswith(("claude-2", "claude-instant")):
            return False
        return "claude" in mod

    if prov == "custom":
        return True

    return False


def _build_raw_assistant_message(
    content: str,
    tool_calls: list[dict[str, Any]],
) -> dict[str, Any]:
    msg: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls:
        msg["tool_calls"] = [
            {
                "id": tc["id"],
                "type": "function",
                "function": {
                    "name": tc["name"],
                    "arguments": json.dumps(tc["arguments"], ensure_ascii=False),
                },
            }
            for tc in tool_calls
        ]
    return msg


class LLMRouter:
    """Async multi-provider LLM router for model discovery, tool detection, and streaming chat."""

    def __init__(self, http_client: httpx.AsyncClient | None = None) -> None:
        self._client = http_client
        self._owns_client = http_client is None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=10.0))
            self._owns_client = True
        return self._client

    async def close(self) -> None:
        """Close the underlying HTTP client if owned by this router."""
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def __aenter__(self) -> LLMRouter:
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()

    async def discover_models(
        self,
        provider: str,
        base_url: str = "",
        api_key: str | None = None,
    ) -> dict[str, Any]:
        """Discover available models and their tool-calling capability for a provider."""
        prov = (provider or "").strip().lower()
        if prov == "ollama":
            return await self._discover_ollama_models(base_url)
        if prov in ("openai", "custom"):
            return await self._discover_openai_compat_models(prov, base_url, api_key)
        if prov == "gemini":
            return await self._discover_gemini_models(base_url, api_key)
        if prov == "anthropic":
            return await self._discover_anthropic_models(base_url, api_key)
        return {"models": [], "error": f"Unsupported provider: {provider}"}

    async def _discover_ollama_models(self, base_url: str) -> dict[str, Any]:
        base = _normalize_ollama_base(base_url)
        client = self._get_client()
        try:
            resp = await client.get(f"{base}/api/tags", timeout=10.0)
            resp.raise_for_status()
            payload = resp.json()
        except Exception as exc:
            return {"models": [], "error": str(exc)}

        raw_models = payload.get("models") if isinstance(payload, dict) else None
        if not isinstance(raw_models, list):
            return {"models": [], "error": "Unexpected /api/tags response format"}

        sem = asyncio.Semaphore(8)

        async def inspect_model(entry: dict[str, Any]) -> dict[str, Any] | None:
            mid = str(entry.get("name") or entry.get("model") or "").strip()
            if not mid:
                return None
            supports_tools = False
            async with sem:
                try:
                    show_resp = await client.post(
                        f"{base}/api/show",
                        json={"model": mid},
                        timeout=10.0,
                    )
                    if show_resp.status_code < 400:
                        show_data = show_resp.json()
                        caps = show_data.get("capabilities") if isinstance(show_data, dict) else None
                        if isinstance(caps, list):
                            supports_tools = "tools" in [str(c).lower() for c in caps]
                except Exception:
                    supports_tools = False
            return {"id": mid, "name": mid, "supports_tools": supports_tools}

        inspected = await asyncio.gather(
            *(inspect_model(m) for m in raw_models if isinstance(m, dict))
        )
        models = [m for m in inspected if m is not None]
        return {"models": models, "error": None}

    async def _discover_openai_compat_models(
        self,
        provider: str,
        base_url: str,
        api_key: str | None,
    ) -> dict[str, Any]:
        base = base_url.strip().rstrip("/") if base_url else ""
        if provider == "openai" and not base:
            base = DEFAULT_OPENAI_BASE_URL
        if not base:
            return {"models": [], "error": "Base URL is required for custom provider"}

        headers: dict[str, str] = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        client = self._get_client()
        try:
            resp = await client.get(f"{base}/models", headers=headers, timeout=15.0)
            resp.raise_for_status()
            payload = resp.json()
        except Exception as exc:
            return {"models": [], "error": str(exc)}

        raw_list = payload.get("data") if isinstance(payload, dict) else payload
        if not isinstance(raw_list, list):
            return {"models": [], "error": "Unexpected /models response format"}

        models: list[dict[str, Any]] = []
        for item in raw_list:
            if not isinstance(item, dict):
                continue
            mid = str(item.get("id") or "").strip()
            if not mid:
                continue
            name = str(item.get("name") or item.get("display_name") or mid)
            supported_params = item.get("supported_parameters")
            if isinstance(supported_params, list):
                supports_tools = "tools" in [str(p).lower() for p in supported_params]
            else:
                supports_tools = _heuristic_tool_support(provider, mid)
            models.append({"id": mid, "name": name, "supports_tools": supports_tools})

        models.sort(key=lambda m: m["id"])
        return {"models": models, "error": None}

    async def _discover_gemini_models(
        self,
        base_url: str,
        api_key: str | None,
    ) -> dict[str, Any]:
        del base_url  # Official google-genai SDK handles endpoint routing via API key only.
        if _google_genai is None:
            return {
                "models": [],
                "error": "google-genai package is not installed; run `pip install google-genai`",
            }
        if not api_key:
            return {"models": [], "error": "GEMINI_API_KEY is not configured"}

        client = _google_genai.Client(api_key=api_key)
        models: list[dict[str, Any]] = []
        try:
            pager = await client.aio.models.list()
            async for item in pager:
                actions = [str(a) for a in (getattr(item, "supported_actions", None) or [])]
                if actions and "generateContent" not in actions:
                    continue
                raw_name = str(getattr(item, "name", "") or "").strip()
                mid = raw_name.removeprefix("models/")
                if not mid:
                    continue
                display_name = str(getattr(item, "display_name", None) or mid)
                models.append(
                    {
                        "id": mid,
                        "name": display_name,
                        "supports_tools": _heuristic_tool_support("gemini", mid),
                    }
                )
        except Exception as exc:
            return {"models": [], "error": str(exc)}
        finally:
            try:
                await client.aio.aclose()
            except Exception:
                pass
        return {"models": models, "error": None}

    async def _discover_anthropic_models(
        self,
        base_url: str,
        api_key: str | None,
    ) -> dict[str, Any]:
        base = (base_url or DEFAULT_ANTHROPIC_BASE_URL).strip().rstrip("/")
        url = f"{base}/models" if base.endswith("/v1") else f"{base}/v1/models"
        headers = {
            "x-api-key": api_key or "",
            "anthropic-version": ANTHROPIC_VERSION,
        }
        client = self._get_client()
        try:
            resp = await client.get(url, headers=headers, timeout=15.0)
            resp.raise_for_status()
            payload = resp.json()
        except Exception as exc:
            return {
                "models": [dict(m) for m in ANTHROPIC_FALLBACK_MODELS],
                "error": str(exc),
            }

        raw_list = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(raw_list, list) or not raw_list:
            return {
                "models": [dict(m) for m in ANTHROPIC_FALLBACK_MODELS],
                "error": None,
            }

        models: list[dict[str, Any]] = []
        for item in raw_list:
            if not isinstance(item, dict):
                continue
            mid = str(item.get("id") or "").strip()
            if not mid:
                continue
            name = str(item.get("display_name") or item.get("name") or mid)
            models.append(
                {
                    "id": mid,
                    "name": name,
                    "supports_tools": _heuristic_tool_support("anthropic", mid),
                }
            )
        if not models:
            return {
                "models": [dict(m) for m in ANTHROPIC_FALLBACK_MODELS],
                "error": None,
            }
        return {"models": models, "error": None}

    async def detect_tool_support(
        self,
        provider: str,
        model: str,
        override: bool | None = None,
        base_url: str = "",
        api_key: str | None = None,
    ) -> bool:
        """Resolve whether the selected provider/model supports tool calling."""
        if override is not None:
            return bool(override)

        prov = (provider or "").strip().lower()
        if prov == "ollama":
            base = _normalize_ollama_base(base_url)
            client = self._get_client()
            try:
                resp = await client.post(
                    f"{base}/api/show",
                    json={"model": model},
                    timeout=10.0,
                )
                if resp.status_code >= 400:
                    return False
                data = resp.json()
                caps = data.get("capabilities") if isinstance(data, dict) else None
                if isinstance(caps, list):
                    return "tools" in [str(c).lower() for c in caps]
                return False
            except Exception:
                return False

        return _heuristic_tool_support(prov, model)

    async def generate_chat(
        self,
        messages: list[dict[str, Any]],
        system_prompt: str,
        provider: str,
        model: str,
        base_url: str = "",
        api_key: str | None = None,
        temperature: float = 0.7,
        tools: list[dict[str, Any]] | None = None,
        on_text_chunk: Callable[[str], Any] | None = None,
        on_emotion: Callable[[str], Any] | None = None,
    ) -> dict[str, Any]:
        """Stream a chat completion with emotion tag extraction and normalized tool calls."""
        prov = (provider or "").strip().lower()
        parser = EmotionStreamParser(on_emotion=on_emotion)
        clean_parts: list[str] = []

        effective_prompt = system_prompt
        if tools:
            tool_rule = (
                "\nIMPORTANT TOOL RULE: When a question requires external or up-to-date information "
                "(such as web search, weather, location, or website fetch), invoke the matching tool "
                "function directly right away. Never reply with a text message saying you are going "
                "to use a tool without calling the tool first; only output your [emotion:<tag>] "
                "spoken reply after receiving the tool result."
            )
            effective_prompt = f"{system_prompt.rstrip()}\n{tool_rule}" if system_prompt else tool_rule.lstrip()

        async def emit_delta(raw_delta: str) -> None:
            if not raw_delta:
                return
            clean = parser.feed(raw_delta)
            await parser.wait_callbacks()
            if clean:
                clean_parts.append(clean)
                await _invoke_callback(on_text_chunk, clean)

        if prov in ("openai", "custom"):
            tool_calls = await self._chat_openai_compat(
                prov, messages, effective_prompt, model, base_url, api_key, temperature, tools, emit_delta
            )
        elif prov == "ollama":
            tool_calls = await self._chat_ollama(
                messages, effective_prompt, model, base_url, temperature, tools, emit_delta
            )
        elif prov == "gemini":
            tool_calls = await self._chat_gemini(
                messages, effective_prompt, model, base_url, api_key, temperature, tools, emit_delta
            )
        elif prov == "anthropic":
            tool_calls = await self._chat_anthropic(
                messages, effective_prompt, model, base_url, api_key, temperature, tools, emit_delta
            )
        else:
            raise ValueError(f"Unsupported LLM provider: {provider}")

        tail = parser.flush()
        await parser.wait_callbacks()
        if tail:
            clean_parts.append(tail)
            await _invoke_callback(on_text_chunk, tail)

        content = "".join(clean_parts)
        emotion = parser.emotion or "neutral"
        return {
            "content": content,
            "emotion": emotion,
            "tool_calls": tool_calls,
            "raw_assistant_message": _build_raw_assistant_message(content, tool_calls),
        }

    async def _chat_openai_compat(
        self,
        provider: str,
        messages: list[dict[str, Any]],
        system_prompt: str,
        model: str,
        base_url: str,
        api_key: str | None,
        temperature: float,
        tools: list[dict[str, Any]] | None,
        emit_delta: Callable[[str], Any],
    ) -> list[dict[str, Any]]:
        base = base_url.strip().rstrip("/") if base_url else ""
        if provider == "openai" and not base:
            base = DEFAULT_OPENAI_BASE_URL
        if not base:
            raise ValueError("Base URL is required for custom provider")

        url = base if base.endswith("/chat/completions") else f"{base}/chat/completions"
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        wire_messages: list[dict[str, Any]] = []
        if system_prompt:
            wire_messages.append({"role": "system", "content": system_prompt})

        for msg in messages:
            role = msg.get("role", "user")
            if role in ("user", "system"):
                wire_messages.append({"role": role, "content": _stringify_content(msg.get("content"))})
            elif role == "assistant":
                tcs = _extract_message_tool_calls(msg)
                entry: dict[str, Any] = {
                    "role": "assistant",
                    "content": msg.get("content") if msg.get("content") is not None else (None if tcs else ""),
                }
                if tcs:
                    entry["tool_calls"] = [
                        {
                            "id": tc["id"],
                            "type": "function",
                            "function": {
                                "name": tc["name"],
                                "arguments": json.dumps(tc["arguments"], ensure_ascii=False),
                            },
                        }
                        for tc in tcs
                    ]
                wire_messages.append(entry)
            elif role == "tool":
                wire_messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": str(msg.get("tool_call_id") or ""),
                        "content": _stringify_content(msg.get("content")),
                    }
                )

        body: dict[str, Any] = {
            "model": model,
            "messages": wire_messages,
            "temperature": temperature,
            "stream": True,
        }
        if tools:
            body["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": name,
                        "description": desc,
                        "parameters": params,
                    },
                }
                for name, desc, params in (_extract_tool_def(t) for t in tools)
                if name
            ]

        client = self._get_client()
        tc_acc: dict[int, dict[str, Any]] = {}

        async with client.stream("POST", url, headers=headers, json=body) as resp:
            if resp.status_code >= 400:
                err_body = (await resp.aread()).decode("utf-8", errors="replace")
                raise RuntimeError(f"{provider} API error {resp.status_code}: {err_body}")

            content_type = resp.headers.get("content-type", "").lower()
            if "application/json" in content_type and "event-stream" not in content_type:
                raw_bytes = await resp.aread()
                data = json.loads(raw_bytes.decode("utf-8"))
                choice = (data.get("choices") or [{}])[0]
                message = choice.get("message") or {}
                if message.get("content"):
                    await emit_delta(str(message["content"]))
                return _extract_message_tool_calls(message)

            last_implicit_idx = -1
            last_seen_id = ""
            async for line in resp.aiter_lines():
                stripped = line.strip()
                if not stripped or stripped.startswith(":"):
                    continue
                if stripped.startswith("data:"):
                    stripped = stripped[5:].strip()
                if stripped == "[DONE]":
                    break
                try:
                    chunk = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                choices = chunk.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}
                if delta.get("content"):
                    await emit_delta(str(delta["content"]))
                for tc_delta in delta.get("tool_calls") or []:
                    if not isinstance(tc_delta, dict):
                        continue
                    if "index" in tc_delta and tc_delta["index"] is not None:
                        idx = int(tc_delta["index"])
                    else:
                        tc_id = tc_delta.get("id") or ""
                        if tc_id and tc_id != last_seen_id:
                            last_implicit_idx += 1
                            last_seen_id = tc_id
                        elif last_implicit_idx < 0:
                            last_implicit_idx = 0
                        idx = last_implicit_idx
                    slot = tc_acc.setdefault(idx, {"id": "", "name": "", "args_parts": []})
                    if tc_delta.get("id"):
                        slot["id"] = str(tc_delta["id"])
                    fn = tc_delta.get("function") or {}
                    if fn.get("name"):
                        slot["name"] += str(fn["name"])
                    if "arguments" in fn and fn["arguments"] is not None:
                        raw_args = fn["arguments"]
                        if isinstance(raw_args, dict):
                            slot["args_parts"].append(json.dumps(raw_args))
                        else:
                            slot["args_parts"].append(str(raw_args))

        return [
            {
                "id": slot["id"] or f"call_{idx}",
                "name": slot["name"],
                "arguments": _safe_json_loads("".join(slot["args_parts"])),
            }
            for idx, slot in sorted(tc_acc.items())
            if slot["name"]
        ]

    async def _chat_ollama(
        self,
        messages: list[dict[str, Any]],
        system_prompt: str,
        model: str,
        base_url: str,
        temperature: float,
        tools: list[dict[str, Any]] | None,
        emit_delta: Callable[[str], Any],
    ) -> list[dict[str, Any]]:
        base = _normalize_ollama_base(base_url)
        url = f"{base}/api/chat"

        wire_messages: list[dict[str, Any]] = []
        if system_prompt:
            wire_messages.append({"role": "system", "content": system_prompt})

        for msg in messages:
            role = msg.get("role", "user")
            if role in ("user", "system"):
                wire_messages.append({"role": role, "content": _stringify_content(msg.get("content"))})
            elif role == "assistant":
                tcs = _extract_message_tool_calls(msg)
                entry: dict[str, Any] = {
                    "role": "assistant",
                    "content": _stringify_content(msg.get("content")),
                }
                if tcs:
                    entry["tool_calls"] = [
                        {"function": {"name": tc["name"], "arguments": tc["arguments"]}}
                        for tc in tcs
                    ]
                wire_messages.append(entry)
            elif role == "tool":
                tool_entry: dict[str, Any] = {
                    "role": "tool",
                    "content": _stringify_content(msg.get("content")),
                }
                if msg.get("name"):
                    tool_entry["tool_name"] = str(msg["name"])
                wire_messages.append(tool_entry)

        body: dict[str, Any] = {
            "model": model,
            "messages": wire_messages,
            "stream": True,
            "options": {"temperature": temperature},
        }
        if tools:
            body["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": name,
                        "description": desc,
                        "parameters": params,
                    },
                }
                for name, desc, params in (_extract_tool_def(t) for t in tools)
                if name
            ]

        client = self._get_client()
        tool_calls: list[dict[str, Any]] = []

        async with client.stream("POST", url, json=body) as resp:
            if resp.status_code >= 400:
                err_body = (await resp.aread()).decode("utf-8", errors="replace")
                raise RuntimeError(f"ollama API error {resp.status_code}: {err_body}")

            async for line in resp.aiter_lines():
                stripped = line.strip()
                if not stripped:
                    continue
                if stripped.startswith("data:"):
                    stripped = stripped[5:].strip()
                try:
                    chunk = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                if isinstance(chunk, dict) and chunk.get("error"):
                    raise RuntimeError(f"ollama error: {chunk['error']}")
                msg_obj = chunk.get("message") or {}
                if msg_obj.get("content"):
                    await emit_delta(str(msg_obj["content"]))
                for tc in msg_obj.get("tool_calls") or []:
                    if not isinstance(tc, dict):
                        continue
                    fn = tc.get("function") if isinstance(tc.get("function"), dict) else tc
                    name = str(fn.get("name") or "").strip()
                    if not name:
                        continue
                    args = _safe_json_loads(fn.get("arguments"))
                    tc_id = str(tc.get("id") or f"ollama_call_{len(tool_calls)}")
                    tool_calls.append({"id": tc_id, "name": name, "arguments": args})
                if chunk.get("done") is True:
                    break

        return tool_calls

    async def _chat_gemini(
        self,
        messages: list[dict[str, Any]],
        system_prompt: str,
        model: str,
        base_url: str,
        api_key: str | None,
        temperature: float,
        tools: list[dict[str, Any]] | None,
        emit_delta: Callable[[str], Any],
    ) -> list[dict[str, Any]]:
        del base_url  # Official google-genai SDK handles endpoint routing via API key only.
        if _google_genai is None or _genai_types is None:
            raise RuntimeError("google-genai package is not installed")
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY is required for Gemini provider")

        model_name = (model or "gemini-2.5-flash").strip().removeprefix("models/")
        contents: list[Any] = []
        call_id_to_name: dict[str, str] = {}

        for msg in messages:
            role = msg.get("role", "user")
            if role == "system":
                continue
            if role == "user":
                contents.append(
                    _genai_types.Content(
                        role="user",
                        parts=[_genai_types.Part.from_text(text=_stringify_content(msg.get("content")))],
                    )
                )
            elif role == "assistant":
                parts: list[Any] = []
                text = _stringify_content(msg.get("content"))
                if text:
                    parts.append(_genai_types.Part.from_text(text=text))
                for tc in _extract_message_tool_calls(msg):
                    call_id_to_name[tc["id"]] = tc["name"]
                    parts.append(
                        _genai_types.Part.from_function_call(
                            name=tc["name"],
                            args=tc["arguments"] if isinstance(tc["arguments"], dict) else {},
                        )
                    )
                if parts:
                    contents.append(_genai_types.Content(role="model", parts=parts))
            elif role == "tool":
                tool_name = str(
                    msg.get("name") or call_id_to_name.get(str(msg.get("tool_call_id") or ""), "")
                )
                raw_val = msg.get("content")
                if isinstance(raw_val, dict):
                    resp_obj: dict[str, Any] = raw_val
                else:
                    resp_obj = {"result": _stringify_content(raw_val)}
                part = _genai_types.Part.from_function_response(
                    name=tool_name,
                    response=resp_obj,
                )
                if (
                    contents
                    and getattr(contents[-1], "role", None) == "user"
                    and any(getattr(p, "function_response", None) is not None for p in (contents[-1].parts or []))
                ):
                    contents[-1].parts.append(part)
                else:
                    contents.append(_genai_types.Content(role="user", parts=[part]))

        genai_tools: list[Any] | None = None
        if tools:
            decls: list[Any] = []
            for name, desc, params in (_extract_tool_def(t) for t in tools):
                if not name:
                    continue
                cleaned_params = {k: v for k, v in params.items() if k != "$schema"}
                decls.append(
                    _genai_types.FunctionDeclaration(
                        name=name,
                        description=desc,
                        parameters=cleaned_params,
                    )
                )
            if decls:
                genai_tools = [_genai_types.Tool(function_declarations=decls)]

        config = _genai_types.GenerateContentConfig(
            temperature=temperature,
            system_instruction=system_prompt or None,
            tools=genai_tools,
        )

        client = _google_genai.Client(api_key=api_key)
        tool_calls: list[dict[str, Any]] = []
        try:
            stream = await client.aio.models.generate_content_stream(
                model=model_name,
                contents=contents,
                config=config,
            )
            async for chunk in stream:
                for cand in getattr(chunk, "candidates", None) or []:
                    content_obj = getattr(cand, "content", None)
                    for part in getattr(content_obj, "parts", None) or []:
                        if getattr(part, "thought", False):
                            continue
                        part_text = getattr(part, "text", None)
                        if part_text:
                            await emit_delta(str(part_text))
                        fc = getattr(part, "function_call", None)
                        if fc is not None:
                            fc_name = str(getattr(fc, "name", None) or "").strip()
                            if fc_name:
                                raw_args = getattr(fc, "args", None)
                                args_dict = dict(raw_args) if isinstance(raw_args, dict) else {}
                                tool_calls.append(
                                    {
                                        "id": str(getattr(fc, "id", None) or f"gemini_call_{len(tool_calls)}"),
                                        "name": fc_name,
                                        "arguments": args_dict,
                                    }
                                )
        finally:
            try:
                await client.aio.aclose()
            except Exception:
                pass

        return tool_calls

    async def _chat_anthropic(
        self,
        messages: list[dict[str, Any]],
        system_prompt: str,
        model: str,
        base_url: str,
        api_key: str | None,
        temperature: float,
        tools: list[dict[str, Any]] | None,
        emit_delta: Callable[[str], Any],
    ) -> list[dict[str, Any]]:
        base = (base_url or DEFAULT_ANTHROPIC_BASE_URL).strip().rstrip("/")
        url = f"{base}/messages" if base.endswith("/v1") else f"{base}/v1/messages"
        headers = {
            "Content-Type": "application/json",
            "x-api-key": api_key or "",
            "anthropic-version": ANTHROPIC_VERSION,
        }

        wire_messages: list[dict[str, Any]] = []
        extra_system: list[str] = [system_prompt] if system_prompt else []

        for msg in messages:
            role = msg.get("role", "user")
            if role == "system":
                sys_text = _stringify_content(msg.get("content"))
                if sys_text:
                    extra_system.append(sys_text)
            elif role == "user":
                wire_messages.append({"role": "user", "content": _stringify_content(msg.get("content"))})
            elif role == "assistant":
                blocks: list[dict[str, Any]] = []
                text = _stringify_content(msg.get("content"))
                if text:
                    blocks.append({"type": "text", "text": text})
                for tc in _extract_message_tool_calls(msg):
                    blocks.append(
                        {
                            "type": "tool_use",
                            "id": tc["id"],
                            "name": tc["name"],
                            "input": tc["arguments"],
                        }
                    )
                if not blocks:
                    blocks.append({"type": "text", "text": ""})
                wire_messages.append({"role": "assistant", "content": blocks})
            elif role == "tool":
                block = {
                    "type": "tool_result",
                    "tool_use_id": str(msg.get("tool_call_id") or ""),
                    "content": _stringify_content(msg.get("content")),
                }
                if (
                    wire_messages
                    and wire_messages[-1]["role"] == "user"
                    and isinstance(wire_messages[-1]["content"], list)
                ):
                    wire_messages[-1]["content"].append(block)
                else:
                    wire_messages.append({"role": "user", "content": [block]})

        body: dict[str, Any] = {
            "model": model,
            "max_tokens": 4096,
            "messages": wire_messages,
            "temperature": temperature,
            "stream": True,
        }
        if extra_system:
            body["system"] = "\n\n".join(extra_system)
        if tools:
            body["tools"] = [
                {
                    "name": name,
                    "description": desc,
                    "input_schema": params,
                }
                for name, desc, params in (_extract_tool_def(t) for t in tools)
                if name
            ]

        client = self._get_client()
        tool_blocks: dict[int, dict[str, Any]] = {}

        async with client.stream("POST", url, headers=headers, json=body) as resp:
            if resp.status_code >= 400:
                err_body = (await resp.aread()).decode("utf-8", errors="replace")
                raise RuntimeError(f"anthropic API error {resp.status_code}: {err_body}")

            content_type = resp.headers.get("content-type", "").lower()
            if "application/json" in content_type and "event-stream" not in content_type:
                raw_bytes = await resp.aread()
                data = json.loads(raw_bytes.decode("utf-8"))
                tool_calls: list[dict[str, Any]] = []
                for idx, block in enumerate(data.get("content") or []):
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "text" and block.get("text"):
                        await emit_delta(str(block["text"]))
                    elif block.get("type") == "tool_use":
                        tool_calls.append(
                            {
                                "id": str(block.get("id") or f"anthropic_call_{idx}"),
                                "name": str(block.get("name") or ""),
                                "arguments": block.get("input") if isinstance(block.get("input"), dict) else {},
                            }
                        )
                return tool_calls

            async for line in resp.aiter_lines():
                stripped = line.strip()
                if not stripped or stripped.startswith(":") or stripped.startswith("event:"):
                    continue
                if stripped.startswith("data:"):
                    stripped = stripped[5:].strip()
                if stripped == "[DONE]":
                    break
                try:
                    event = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                ev_type = event.get("type")
                if ev_type == "error":
                    err_obj = event.get("error") or {}
                    raise RuntimeError(f"anthropic stream error: {err_obj.get('message', err_obj)}")
                if ev_type == "content_block_start":
                    idx = int(event.get("index", 0))
                    block = event.get("content_block") or {}
                    btype = block.get("type")
                    if btype == "text" and block.get("text"):
                        await emit_delta(str(block["text"]))
                    elif btype == "tool_use":
                        tool_blocks[idx] = {
                            "id": str(block.get("id") or f"anthropic_call_{idx}"),
                            "name": str(block.get("name") or ""),
                            "initial_input": block.get("input") if isinstance(block.get("input"), dict) else {},
                            "json_parts": [],
                        }
                elif ev_type == "content_block_delta":
                    idx = int(event.get("index", 0))
                    delta = event.get("delta") or {}
                    dtype = delta.get("type")
                    if dtype == "text_delta" and delta.get("text"):
                        await emit_delta(str(delta["text"]))
                    elif dtype == "input_json_delta" and delta.get("partial_json"):
                        slot = tool_blocks.get(idx)
                        if slot is not None:
                            slot["json_parts"].append(str(delta["partial_json"]))
                elif ev_type == "message_stop":
                    break

        return [
            {
                "id": slot["id"],
                "name": slot["name"],
                "arguments": (
                    _safe_json_loads("".join(slot["json_parts"]))
                    if slot["json_parts"]
                    else slot["initial_input"]
                ),
            }
            for _, slot in sorted(tool_blocks.items())
            if slot["name"]
        ]
