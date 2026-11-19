"""GTK4 / Libadwaita Settings UI for Bytey AI Companion."""

from __future__ import annotations

import copy
import json
import math
import threading
from pathlib import Path
from typing import Any, Callable

import gi

gi.require_version("Gdk", "4.0")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from .api_client import (  # noqa: E402
    DEFAULT_CONFIG,
    DEFAULT_SYSTEM_PROMPT,
    PROVIDER_SECRET_MAP,
    ApiResult,
    DaemonClient,
    deep_merge,
)

PROVIDERS = ("ollama", "gemini", "openai", "anthropic", "custom")
PROVIDER_DEFAULT_URLS = {
    "ollama": "http://localhost:11434",
    "gemini": "",
    "openai": "https://api.openai.com/v1",
    "anthropic": "https://api.anthropic.com",
    "custom": "http://localhost:8000/v1",
}
STT_BACKENDS = ("local_whisper", "openai_whisper", "custom_whisper")
TTS_PROVIDERS = ("kokoro", "piper", "elevenlabs", "disabled")
PIPER_VOICE_PRESETS: tuple[tuple[str, str, float | None, float | None, float | None], ...] = (
    ("normal", "Normal", 1.0, 0.0, 0.667),
    ("chipmunk", "Chipmunk", 1.25, 8.0, 0.8),
    ("helium", "Helium Squeak", 1.15, 10.5, 0.9),
    ("deep_monster", "Deep Monster", 0.82, -8.0, 0.667),
    ("sleepy_giant", "Sleepy Giant", 0.68, -5.5, 0.55),
    ("hyper_gremlin", "Hyper Gremlin", 1.55, 5.5, 1.0),
    ("flat_robot", "Flat Robot", 1.05, -2.5, 0.1),
    ("custom", "Custom", None, None, None),
)
_PIPER_SAMPLE_TEXTS: dict[str, str] = {
    "en": "Hello! This is Bytey testing my Piper local voice.",
    "es": "¡Hola! Soy Bytey probando mi voz local de Piper.",
    "fr": "Bonjour ! Ici Bytey, je teste ma voix locale Piper.",
    "de": "Hallo! Hier ist Bytey und ich teste meine lokale Piper-Stimme.",
    "it": "Ciao! Sono Bytey e sto provando la mia voce locale Piper.",
    "pt": "Olá! Aqui é o Bytey testando a minha voz local Piper.",
    "nl": "Hallo! Dit is Bytey en ik test mijn lokale Piper-stem.",
    "pl": "Cześć! Tu Bytey, testuję mój lokalny głos Piper.",
    "ru": "Привет! Это Байти, я проверяю свой локальный голос Piper.",
    "uk": "Привіт! Це Байті, я перевіряю свій локальний голос Piper.",
    "zh": "你好！我是 Bytey，正在测试我的本地 Piper 语音。",
    "ja": "こんにちは！Byteyです。ローカルのPiper音声をテストしています。",
    "ko": "안녕하세요! Bytey입니다. 로컬 Piper 음성을 테스트하고 있습니다.",
    "tr": "Merhaba! Ben Bytey, yerel Piper sesimi test ediyorum.",
    "ar": "مرحباً! أنا بايتي، أختبر صوتي المحلي من بايبر.",
    "hi": "नमस्ते! मैं बाइटी हूँ, अपनी लोकल पाइपर आवाज़ की जांच कर रही हूँ।",
    "sv": "Hej! Det här är Bytey som testar min lokala Piper-röst.",
    "da": "Hej! Det er Bytey, der tester min lokale Piper-stemme.",
    "no": "Hei! Dette er Bytey som tester min lokale Piper-stemme.",
    "nb": "Hei! Dette er Bytey som tester min lokale Piper-stemme.",
    "fi": "Hei! Täällä Bytey testaa paikallista Piper-ääntäni.",
    "cs": "Ahoj! Tady Bytey, testuji svůj lokální hlas Piper.",
    "el": "Γεια σας! Είμαι ο Bytey και δοκιμάζω την τοπική φωνή Piper.",
    "ro": "Salut! Sunt Bytey și îmi testez vocea locală Piper.",
    "hu": "Szia! Bytey vagyok, a helyi Piper hangomat tesztelem.",
    "vi": "Xin chào! Tôi là Bytey đang thử nghiệm giọng nói Piper cục bộ.",
    "ca": "Hola! Soc en Bytey provant la meva veu local de Piper.",
    "fa": "سلام! من بایتی هستم و صدای محلی پایپر را آزمایش می‌کنم.",
}
ANIMATION_MODES = ("full", "state_only", "disabled")
POSITION_MODE_OPTIONS: tuple[tuple[str, str], ...] = (
    ("Fixed", "fixed"),
    ("Floating", "floating"),
    ("Notch", "notch"),
)
POSITION_MODES = tuple(value for _, value in POSITION_MODE_OPTIONS)
CHAT_DENSITIES = ("compact", "comfortable", "spacious")
FIXED_ALIGNMENTS = (
    "top-left",
    "top-center",
    "top-right",
    "center-left",
    "center",
    "center-right",
    "bottom-left",
    "bottom-center",
    "bottom-right",
)
MCP_TRANSPORTS = ("stdio", "http", "sse")

DEFAULT_EYE_COLOR = "#F7FAFF"
DEFAULT_BG_COLOR = "#0A0A0C"
DEFAULT_BORDER_COLOR = "#2A2C34"
DEFAULT_CONTEXT_FALLBACK_MAX = 131072

# Canonical setting -> (nested_path, flat_aliases)
FIELD_PATHS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "pet_name": (("general", "pet_name"), ("pet_name",)),
    "wake_phrase": (("general", "wake_phrase"), ("wake_phrase",)),
    "system_prompt": (("general", "system_prompt"), ("system_prompt",)),
    "llm_provider": (("llm", "provider"), ("llm_provider", "provider")),
    "llm_base_url": (("llm", "base_url"), ("llm_base_url",)),
    "llm_model": (("llm", "model"), ("llm_model", "model")),
    "llm_supports_tools": (("llm", "supports_tools"), ("supports_tools",)),
    "llm_force_tools": (("llm", "force_enable_tools"), ("force_enable_tools", "force_tools")),
    "llm_context_size": (("llm", "context_size"), ("llm_context_size", "context_size")),
    "llm_max_context_size": (("llm", "max_context_size"), ("max_context_size",)),
    "mic_muted": (("voice_input", "mic_muted"), ("mic_muted",)),
    "wake_sensitivity": (("voice_input", "wake_sensitivity"), ("wake_sensitivity",)),
    "vad_silence_timeout_ms": (
        ("voice_input", "vad_silence_timeout_ms"),
        ("vad_silence_timeout_ms", "silence_timeout_ms"),
    ),
    "stt_backend": (("voice_input", "stt_backend"), ("stt_backend",)),
    "whisper_model": (("voice_input", "whisper_model"), ("whisper_model", "stt_model")),
    "stt_api_url": (("voice_input", "stt_api_url"), ("stt_api_url",)),
    "tts_muted": (("voice_output", "tts_muted"), ("tts_muted",)),
    "tts_provider": (("voice_output", "provider"), ("tts_provider",)),
    "kokoro_base_url": (("voice_output", "kokoro_base_url"), ("kokoro_base_url",)),
    "kokoro_voice": (("voice_output", "kokoro_voice"), ("kokoro_voice",)),
    "kokoro_speed": (("voice_output", "kokoro_speed"), ("kokoro_speed",)),
    "piper_model": (("voice_output", "piper_model"), ("piper_model",)),
    "piper_models_dir": (("voice_output", "piper_models_dir"), ("piper_models_dir",)),
    "piper_speed": (("voice_output", "piper_speed"), ("piper_speed",)),
    "piper_pitch": (("voice_output", "piper_pitch"), ("piper_pitch",)),
    "piper_noise_scale": (("voice_output", "piper_noise_scale"), ("piper_noise_scale",)),
    "elevenlabs_voice_id": (("voice_output", "elevenlabs_voice_id"), ("elevenlabs_voice_id",)),
    "elevenlabs_model_id": (("voice_output", "elevenlabs_model_id"), ("elevenlabs_model_id",)),
    "animation_mode": (("visuals", "animation_mode"), ("animation_mode",)),
    "cursor_tracking": (("visuals", "cursor_tracking"), ("cursor_tracking",)),
    "eye_color": (("visuals", "eye_color"), ("eye_color",)),
    "background_color": (("visuals", "background_color"), ("background_color",)),
    "border_color": (("visuals", "border_color"), ("border_color",)),
    "position_mode": (("visuals", "position_mode"), ("position_mode",)),
    "monitor_index": (("visuals", "monitor_index"), ("monitor_index",)),
    "fixed_alignment": (("visuals", "fixed_alignment"), ("fixed_alignment",)),
    "offset_x": (("visuals", "offset_x"), ("offset_x",)),
    "offset_y": (("visuals", "offset_y"), ("offset_y",)),
    "float_x": (("visuals", "float_x"), ("float_x",)),
    "float_y": (("visuals", "float_y"), ("float_y",)),
    "chat_width": (("visuals", "chat_width"), ("chat_width",)),
    "chat_height": (("visuals", "chat_height"), ("chat_height",)),
    "chat_density": (("visuals", "chat_density"), ("chat_density",)),
}


def _parse_hex_channels(hex_str: str) -> tuple[float, float, float, float] | None:
    """Parse #RGB, #RRGGBB, or #RRGGBBAA into normalized RGBA floats, or None if invalid."""
    if not isinstance(hex_str, str):
        return None
    cleaned = hex_str.strip()
    if not cleaned.startswith("#"):
        return None
    raw = cleaned[1:]
    if not all(ch in "0123456789abcdefABCDEF" for ch in raw):
        return None
    if len(raw) == 3:
        raw = "".join(ch * 2 for ch in raw) + "FF"
    elif len(raw) == 6:
        raw = f"{raw}FF"
    elif len(raw) != 8:
        return None
    return (
        int(raw[0:2], 16) / 255.0,
        int(raw[2:4], 16) / 255.0,
        int(raw[4:6], 16) / 255.0,
        int(raw[6:8], 16) / 255.0,
    )


def _hex_to_rgba(hex_str: str, fallback: str = "#FFFFFFFF") -> Gdk.RGBA:
    """Parse #RGB, #RRGGBB, or #RRGGBBAA (falling back if invalid) into a Gdk.RGBA."""
    channels = _parse_hex_channels(hex_str)
    if channels is None:
        channels = _parse_hex_channels(fallback) or (1.0, 1.0, 1.0, 1.0)
    r, g, b, a = channels
    return Gdk.RGBA(red=r, green=g, blue=b, alpha=a)


def _rgba_to_hex(rgba: Gdk.RGBA) -> str:
    """Convert Gdk.RGBA to uppercase #RRGGBB (opaque) or #RRGGBBAA (translucent)."""
    r = max(0, min(255, round(rgba.red * 255)))
    g = max(0, min(255, round(rgba.green * 255)))
    b = max(0, min(255, round(rgba.blue * 255)))
    a = max(0, min(255, round(rgba.alpha * 255)))
    if a >= 255:
        return f"#{r:02X}{g:02X}{b:02X}"
    return f"#{r:02X}{g:02X}{b:02X}{a:02X}"


def _get_nested(data: dict[str, Any], path: tuple[str, ...]) -> Any:
    cur: Any = data
    for part in path:
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _set_nested(data: dict[str, Any], path: tuple[str, ...], value: Any) -> None:
    cur = data
    for part in path[:-1]:
        nxt = cur.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[part] = nxt
        cur = nxt
    cur[path[-1]] = value


def _make_string_combo(
    title: str,
    subtitle: str,
    items: tuple[str, ...] | list[str],
    *,
    searchable: bool = False,
) -> Adw.ComboRow:
    row = Adw.ComboRow(title=title, enable_search=searchable)
    if subtitle:
        row.set_subtitle(subtitle)
    if searchable:
        row.set_expression(Gtk.PropertyExpression.new(Gtk.StringObject, None, "string"))
    row.set_model(Gtk.StringList.new(list(items)))
    return row


def _make_scale_row(
    title: str,
    subtitle: str,
    min_val: float,
    max_val: float,
    step: float,
    digits: int,
    on_changed: Callable[[Gtk.Scale], None],
) -> tuple[Adw.ActionRow, Gtk.Scale]:
    """Create an Adw.ActionRow containing a horizontally aligned Gtk.Scale."""
    row = Adw.ActionRow(title=title, subtitle=subtitle)
    scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, min_val, max_val, step)
    scale.set_size_request(200, -1)
    scale.set_draw_value(True)
    scale.set_digits(digits)
    scale.set_valign(Gtk.Align.CENTER)
    scale.connect("value-changed", on_changed)
    row.add_suffix(scale)
    return row, scale


def _match_piper_preset_index(speed: float, pitch: float, noise_scale: float) -> int:
    """Return matching PIPER_VOICE_PRESETS index, or the 'custom' preset index."""
    for idx, (_, _, p_speed, p_pitch, p_noise) in enumerate(PIPER_VOICE_PRESETS):
        if p_speed is None or p_pitch is None or p_noise is None:
            continue
        if (
            abs(speed - p_speed) <= 0.01
            and abs(pitch - p_pitch) <= 0.1
            and abs(noise_scale - p_noise) <= 0.01
        ):
            return idx
    return len(PIPER_VOICE_PRESETS) - 1


def _piper_sample_text(lang_code: str) -> str:
    """Return a short natural preview phrase matching *lang_code*."""
    code = (lang_code or "").strip().replace("-", "_").lower()
    prefix = code.split("_", 1)[0]
    return _PIPER_SAMPLE_TEXTS.get(
        prefix,
        "Hello! This is Bytey testing my Piper local voice.",
    )


def _normalize_piper_voice(item: Any) -> dict[str, Any] | None:
    """Normalize a Piper catalog item (dict or string) into canonical metadata."""
    if isinstance(item, str):
        vid = item.strip()
        if not vid:
            return None
        lang_code = vid.split("-", 1)[0] if "-" in vid and "_" in vid.split("-", 1)[0] else ""
        quality = vid.rsplit("-", 1)[-1] if vid.count("-") >= 2 else ""
        return {
            "id": vid,
            "name": vid,
            "lang_code": lang_code,
            "lang_label": lang_code,
            "quality": quality,
            "installed": False,
        }
    if not isinstance(item, dict):
        return None
    vid = str(
        item.get("id") or item.get("model") or item.get("voice_id") or item.get("key") or item.get("name") or ""
    ).strip()
    if not vid:
        return None
    raw_name = str(item.get("display_name") or item.get("name") or vid).strip()
    lang_obj = item.get("language") or item.get("lang") or item.get("locale")
    if isinstance(lang_obj, dict):
        lang_code = str(lang_obj.get("code") or lang_obj.get("id") or "").strip()
        lang_eng = str(lang_obj.get("name_english") or lang_obj.get("name") or "").strip()
        country = str(lang_obj.get("country_english") or lang_obj.get("region") or "").strip()
        lang_label = f"{lang_eng} ({country})" if lang_eng and country else (lang_eng or lang_code)
    else:
        lang_code = str(lang_obj or item.get("language_code") or item.get("lang_code") or "").strip()
        lang_label = str(item.get("language_name") or item.get("lang_name") or "").strip()
    if not lang_code and "-" in vid:
        candidate = vid.split("-", 1)[0]
        if "_" in candidate or len(candidate) in (2, 5):
            lang_code = candidate
    quality = str(item.get("quality") or "").strip()
    if not quality and vid.count("-") >= 2:
        quality = vid.rsplit("-", 1)[-1]
    installed = bool(
        item["installed"]
        if "installed" in item
        else item.get("downloaded", item.get("is_installed", False))
    )
    return {
        "id": vid,
        "name": raw_name or vid,
        "lang_code": lang_code,
        "lang_label": lang_label or lang_code,
        "quality": quality,
        "installed": installed,
    }


def _format_piper_voice_label(voice: dict[str, Any]) -> str:
    """Format a normalized Piper voice entry for searchable ComboRow display."""
    name = str(voice.get("name") or voice.get("id") or "")
    lang_code = str(voice.get("lang_code") or "")
    lang_label = str(voice.get("lang_label") or "")
    quality = str(voice.get("quality") or "")
    status = "Installed" if voice.get("installed") else "Download"

    meta_parts: list[str] = []
    if lang_label and lang_code and lang_label != lang_code:
        meta_parts.append(f"{lang_label} · {lang_code}")
    elif lang_code or lang_label:
        meta_parts.append(lang_code or lang_label)
    if quality:
        meta_parts.append(quality)
    meta = ", ".join(meta_parts)
    if meta:
        return f"{name} ({meta}) — {status}"
    return f"{name} — {status}"


def _combo_selected_text(row: Adw.ComboRow) -> str:
    item = row.get_selected_item()
    return item.get_string() if item is not None else ""


def _set_combo_text(row: Adw.ComboRow, target: str) -> None:
    model = row.get_model()
    if model is None:
        return
    for idx in range(model.get_n_items()):
        item = model.get_item(idx)
        if item is not None and item.get_string() == target:
            row.set_selected(idx)
            return


def _make_secret_row(title: str, subtitle: str = "") -> tuple[Adw.PasswordEntryRow, Gtk.Label]:
    row = Adw.PasswordEntryRow(title=title, show_apply_button=True)
    if subtitle:
        row.set_subtitle(subtitle)
    badge = Gtk.Label(label="Not set", valign=Gtk.Align.CENTER)
    badge.add_css_class("dim-label")
    badge.add_css_class("caption")
    row.add_suffix(badge)
    return row, badge


SECRET_MASK_PLACEHOLDER = "••••••••••••••••"


def _is_masked_placeholder(value: str) -> bool:
    cleaned = value.strip()
    return bool(cleaned) and (cleaned == SECRET_MASK_PLACEHOLDER or set(cleaned) == {"•"})


def _set_secret_badge(badge: Gtk.Label, configured: bool) -> None:
    if configured:
        badge.set_label("Configured")
        badge.remove_css_class("dim-label")
        badge.add_css_class("success")
    else:
        badge.set_label("Not set")
        badge.remove_css_class("success")
        badge.add_css_class("dim-label")


def _set_secret_row_state(
    row: Adw.PasswordEntryRow, badge: Gtk.Label, configured: bool
) -> None:
    _set_secret_badge(badge, configured)
    row.set_text(SECRET_MASK_PLACEHOLDER if configured else "")


def _normalize_positive_int(value: Any) -> int | None:
    """Parse a positive integer value, returning None for invalid or non-positive inputs."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer():
            return None
        parsed = int(value)
        return parsed if parsed > 0 else None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            parsed = int(text)
        except ValueError:
            return None
        return parsed if parsed > 0 else None
    return None


def format_context_short(tokens: int) -> str:
    """Format a token count compactly (e.g. '128K', '1M', or '100,000')."""
    if tokens >= 1048576 and tokens % 1048576 == 0:
        return f"{tokens // 1048576}M"
    if tokens >= 1024 and tokens % 1024 == 0:
        return f"{tokens // 1024}K"
    return f"{tokens:,}"


def format_context_tokens(tokens: int) -> str:
    """Format a token count for dropdown display (e.g. '128K (131,072)')."""
    short = format_context_short(tokens)
    if short.endswith(("M", "K")):
        return f"{short} ({tokens:,})"
    return short


def build_context_size_presets(
    max_context: int | None,
    current_value: int | None = None,
) -> list[tuple[str, int | None]]:
    """Build (label, token_count) context size preset options bounded by max_context."""
    max_ctx = _normalize_positive_int(max_context)
    cur_val = _normalize_positive_int(current_value)

    values: list[int] = []
    if max_ctx is not None and max_ctx < 4096:
        if max_ctx < 512:
            values.append(max_ctx)
        else:
            step = 512
            while step <= max_ctx:
                values.append(step)
                step *= 2
    else:
        limit = min(
            max_ctx if max_ctx is not None else DEFAULT_CONTEXT_FALLBACK_MAX,
            16777216,
        )
        step = 4096
        while step <= limit:
            values.append(step)
            step *= 2

    if cur_val is not None and (max_ctx is None or cur_val <= max_ctx) and cur_val not in values:
        values.append(cur_val)
        values.sort()

    return [("Auto (Default)", None), *[(format_context_tokens(v), v) for v in values]]


def clamp_context_size(
    value: int | None,
    presets: list[tuple[str, int | None]],
) -> int | None:
    """Clamp a context size token count to the nearest valid preset <= value."""
    cur = _normalize_positive_int(value)
    if cur is None:
        return None
    numeric = [v for _, v in presets if v is not None]
    if cur in numeric:
        return cur
    if not numeric:
        return None
    le = [v for v in numeric if v <= cur]
    return max(le) if le else numeric[-1]


class MCPServerDialog(Adw.Dialog):
    """Modal dialog for adding or editing a single MCP server configuration."""

    def __init__(
        self,
        parent: Gtk.Window,
        on_save: Callable[[str, dict[str, Any], str | None], None],
        existing_name: str | None = None,
        existing_cfg: dict[str, Any] | None = None,
    ) -> None:
        title = f"Edit MCP Server: {existing_name}" if existing_name else "Add MCP Server"
        super().__init__(title=title, content_width=520, content_height=600)
        self._parent = parent
        self._on_save = on_save
        self._original_name = existing_name
        cfg = copy.deepcopy(existing_cfg or {})

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar(show_end_title_buttons=False, show_start_title_buttons=False)
        cancel_btn = Gtk.Button(label="Cancel")
        cancel_btn.connect("clicked", lambda *_: self.close())
        save_btn = Gtk.Button(label="Save")
        save_btn.add_css_class("suggested-action")
        save_btn.connect("clicked", self._handle_save)
        header.pack_start(cancel_btn)
        header.pack_end(save_btn)
        toolbar.add_top_bar(header)

        page = Adw.PreferencesPage()
        core_group = Adw.PreferencesGroup(title="Server Identity & Transport")

        self.name_row = Adw.EntryRow(title="Server Name")
        if existing_name:
            self.name_row.set_text(existing_name)
        core_group.add(self.name_row)

        self.transport_row = _make_string_combo("Transport", "", MCP_TRANSPORTS)
        transport = str(cfg.get("transport") or ("http" if cfg.get("url") else "stdio")).lower()
        if transport not in MCP_TRANSPORTS:
            transport = "stdio"
        _set_combo_text(self.transport_row, transport)
        self.transport_row.connect("notify::selected", lambda *_: self._sync_transport_visibility())
        core_group.add(self.transport_row)

        self.confirm_row = Adw.SwitchRow(
            title="Require Confirmation",
            subtitle="Prompt in island drawer before executing tools on this server",
        )
        self.confirm_row.set_active(bool(cfg.get("require_confirmation", False)))
        core_group.add(self.confirm_row)
        page.add(core_group)

        # Stdio group
        self.stdio_group = Adw.PreferencesGroup(title="Stdio Process")
        self.command_row = Adw.EntryRow(title="Command")
        self.command_row.set_text(str(cfg.get("command", "")))
        self.stdio_group.add(self.command_row)

        self.args_row = Adw.EntryRow(title="Args (JSON array or space-separated)")
        raw_args = cfg.get("args", [])
        if isinstance(raw_args, list):
            self.args_row.set_text(json.dumps(raw_args) if raw_args else "")
        else:
            self.args_row.set_text(str(raw_args))
        self.stdio_group.add(self.args_row)

        self.env_view = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR)
        self.env_view.add_css_class("monospace")
        env_val = cfg.get("env", {})
        self.env_view.get_buffer().set_text(
            json.dumps(env_val, indent=2) if isinstance(env_val, dict) and env_val else "{}"
        )
        env_row = self._make_textview_row("Environment Variables (JSON object)", self.env_view, height=96)
        self.stdio_group.add(env_row)
        page.add(self.stdio_group)

        # HTTP / SSE group
        self.http_group = Adw.PreferencesGroup(title="HTTP / SSE Endpoint")
        self.url_row = Adw.EntryRow(title="URL")
        self.url_row.set_text(str(cfg.get("url", "")))
        self.http_group.add(self.url_row)

        self.headers_view = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR)
        self.headers_view.add_css_class("monospace")
        headers_val = cfg.get("headers", {})
        self.headers_view.get_buffer().set_text(
            json.dumps(headers_val, indent=2)
            if isinstance(headers_val, dict) and headers_val
            else "{}"
        )
        headers_row = self._make_textview_row("HTTP Headers (JSON object)", self.headers_view, height=96)
        self.http_group.add(headers_row)
        page.add(self.http_group)

        self.error_label = Gtk.Label(xalign=0, wrap=True, visible=False)
        self.error_label.add_css_class("error")
        err_group = Adw.PreferencesGroup()
        err_group.add(self.error_label)
        page.add(err_group)

        scrolled = Gtk.ScrolledWindow(vexpand=True, hexpand=True)
        scrolled.set_child(page)
        toolbar.set_content(scrolled)
        self.set_child(toolbar)
        self._sync_transport_visibility()

    @staticmethod
    def _make_textview_row(label_text: str, view: Gtk.TextView, height: int = 100) -> Gtk.Box:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin_top=6, margin_bottom=6)
        lbl = Gtk.Label(label=label_text, xalign=0)
        lbl.add_css_class("caption")
        lbl.add_css_class("dim-label")
        scroll = Gtk.ScrolledWindow(min_content_height=height)
        scroll.set_child(view)
        frame = Gtk.Frame()
        frame.set_child(scroll)
        box.append(lbl)
        box.append(frame)
        return box

    def _sync_transport_visibility(self) -> None:
        is_stdio = _combo_selected_text(self.transport_row) == "stdio"
        self.stdio_group.set_visible(is_stdio)
        self.http_group.set_visible(not is_stdio)

    def _show_error(self, message: str) -> None:
        self.error_label.set_text(message)
        self.error_label.set_visible(True)

    @staticmethod
    def _read_buffer(view: Gtk.TextView) -> str:
        buf = view.get_buffer()
        return buf.get_text(buf.get_start_iter(), buf.get_end_iter(), True).strip()

    def _handle_save(self, *_: object) -> None:
        name = self.name_row.get_text().strip()
        if not name:
            self._show_error("Server name is required.")
            return

        transport = _combo_selected_text(self.transport_row)
        new_cfg: dict[str, Any] = {
            "transport": transport,
            "require_confirmation": self.confirm_row.get_active(),
        }

        if transport == "stdio":
            command = self.command_row.get_text().strip()
            if not command:
                self._show_error("Command is required for stdio transport.")
                return
            new_cfg["command"] = command

            raw_args = self.args_row.get_text().strip()
            if raw_args:
                if raw_args.startswith("["):
                    try:
                        parsed_args = json.loads(raw_args)
                    except json.JSONDecodeError as exc:
                        self._show_error(f"Invalid args JSON: {exc}")
                        return
                    if not isinstance(parsed_args, list):
                        self._show_error("Args JSON must be a list.")
                        return
                    new_cfg["args"] = [str(x) for x in parsed_args]
                else:
                    new_cfg["args"] = raw_args.split()
            else:
                new_cfg["args"] = []

            raw_env = self._read_buffer(self.env_view)
            if raw_env:
                try:
                    parsed_env = json.loads(raw_env)
                except json.JSONDecodeError as exc:
                    self._show_error(f"Invalid env JSON: {exc}")
                    return
                if not isinstance(parsed_env, dict):
                    self._show_error("Environment variables must be a JSON object.")
                    return
                if parsed_env:
                    new_cfg["env"] = {str(k): str(v) for k, v in parsed_env.items()}
        else:
            url = self.url_row.get_text().strip()
            if not url:
                self._show_error("URL is required for HTTP/SSE transport.")
                return
            new_cfg["url"] = url

            raw_headers = self._read_buffer(self.headers_view)
            if raw_headers:
                try:
                    parsed_headers = json.loads(raw_headers)
                except json.JSONDecodeError as exc:
                    self._show_error(f"Invalid headers JSON: {exc}")
                    return
                if not isinstance(parsed_headers, dict):
                    self._show_error("Headers must be a JSON object.")
                    return
                if parsed_headers:
                    new_cfg["headers"] = {str(k): str(v) for k, v in parsed_headers.items()}

        self._on_save(name, new_cfg, self._original_name)
        self.close()


class ByteyPreferencesWindow(Adw.PreferencesWindow):
    """Main 5-tab Libadwaita PreferencesWindow for Bytey AI Companion."""

    def __init__(self, application: Adw.Application, client: DaemonClient) -> None:
        super().__init__(
            application=application,
            title="Bytey AI Companion Settings",
            default_width=780,
            default_height=700,
            search_enabled=True,
        )
        self.client = client
        self._loading = False
        self._config: dict[str, Any] = copy.deepcopy(DEFAULT_CONFIG)
        self._use_flat_keys = False
        self._secrets_configured: dict[str, bool] = {}
        self._model_tool_map: dict[str, bool] = {}
        self._model_context_map: dict[str, int] = {}
        self._context_preset_values: list[int | None] = [None]
        self._elevenlabs_voices: list[tuple[str, str]] = []
        self._piper_all_voices: list[dict[str, Any]] = []
        self._piper_filtered_voices: list[dict[str, Any]] = []
        self._piper_languages: list[tuple[str, str]] = [("", "All Languages")]
        self._piper_downloading = False
        self._suppress_piper_refresh_toast = False
        self._debounce_timers: dict[str, int] = {}
        self._offline_banners: list[Adw.Banner] = []
        self._mcp_servers: dict[str, dict[str, Any]] = {}
        self._mcp_rows: list[Gtk.Widget] = []
        self._color_controls: dict[str, tuple[Adw.ActionRow, Gtk.ColorDialogButton, str]] = {}

        self._build_general_page()
        self._build_ai_mcp_page()
        self._build_voice_input_page()
        self._build_voice_output_page()
        self._build_visuals_page()
        self._build_import_export_page()

        self.connect("close-request", self._on_close_request)
        self.reload_all()

    def _on_close_request(self, *_: object) -> bool:
        for source_id in list(self._debounce_timers.values()):
            GLib.source_remove(source_id)
        self._debounce_timers.clear()
        self._flush_pending_entry_fields()
        return False

    def _flush_pending_entry_fields(self) -> None:
        if self._loading:
            return
        candidates = {
            "pet_name": self.pet_name_row.get_text().strip(),
            "wake_phrase": self.wake_phrase_row.get_text().strip(),
            "llm_model": self.model_entry_row.get_text().strip(),
            "whisper_model": self.whisper_model_row.get_text().strip(),
            "stt_api_url": self.stt_api_url_row.get_text().strip(),
            "kokoro_base_url": self.kokoro_url_row.get_text().strip(),
            "kokoro_voice": self.kokoro_voice_row.get_text().strip(),
            "piper_model": self.piper_model_row.get_text().strip(),
            "piper_models_dir": self.piper_models_dir_row.get_text().strip(),
            "elevenlabs_voice_id": self.el_voice_id_row.get_text().strip(),
            "elevenlabs_model_id": self.el_model_row.get_text().strip(),
        }
        if _combo_selected_text(self.provider_row) != "gemini":
            candidates["llm_base_url"] = self.llm_base_url_row.get_text().strip()
        allow_empty = {"piper_models_dir"}
        dirty: dict[str, Any] = {}
        for field, val in candidates.items():
            if (val or field in allow_empty) and val != str(self._read_field(field, "")):
                dirty[field] = val
        if dirty:
            patch = self._build_patch(dirty)
            try:
                self.client.patch_config(patch)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Async & Config Helpers
    # ------------------------------------------------------------------

    def _run_async(self, fn: Callable[[], Any], on_done: Callable[[Any], None]) -> None:
        def worker() -> None:
            try:
                res = fn()
            except Exception as exc:  # pragma: no cover - defensive guard
                res = ApiResult(ok=False, error=str(exc))
            GLib.idle_add(self._deliver_result, on_done, res)

        threading.Thread(target=worker, daemon=True).start()

    @staticmethod
    def _deliver_result(on_done: Callable[[Any], None], res: Any) -> bool:
        on_done(res)
        return GLib.SOURCE_REMOVE

    def _toast(self, message: str, timeout: int = 3) -> None:
        toast = Adw.Toast.new(message)
        toast.set_timeout(timeout)
        self.add_toast(toast)

    def _create_offline_banner(self) -> Adw.Banner:
        try:
            rel = self.client.config_path.relative_to(Path.home())
            display_path = f"~/{rel}"
        except ValueError:
            display_path = str(self.client.config_path)
        banner = Adw.Banner(
            title=f"Daemon offline — editing {display_path} locally",
            revealed=False,
        )
        banner.set_button_label("Retry")
        banner.connect("button-clicked", lambda *_: self.reload_all())
        self._offline_banners.append(banner)
        return banner

    def _set_offline(self, offline: bool) -> None:
        for banner in self._offline_banners:
            banner.set_revealed(offline)

    def _read_field(self, field: str, default: Any = None) -> Any:
        nested_path, flat_aliases = FIELD_PATHS[field]
        val = _get_nested(self._config, nested_path)
        if val is not None:
            return val
        for alias in flat_aliases:
            if alias in self._config and self._config[alias] is not None:
                return self._config[alias]
        fallback = _get_nested(DEFAULT_CONFIG, nested_path)
        return default if fallback is None else fallback

    def _build_patch(self, updates: dict[str, Any]) -> dict[str, Any]:
        patch: dict[str, Any] = {}
        for field, value in updates.items():
            nested_path, flat_aliases = FIELD_PATHS[field]
            if self._use_flat_keys and flat_aliases:
                patch[flat_aliases[0]] = value
                _set_nested(self._config, (flat_aliases[0],), value)
            else:
                _set_nested(patch, nested_path, value)
                _set_nested(self._config, nested_path, value)
        return patch

    def _patch_fields(self, updates: dict[str, Any], toast_msg: str | None = None) -> None:
        if self._loading:
            return
        patch = self._build_patch(updates)
        self._run_async(
            lambda: self.client.patch_config(patch),
            lambda res: self._on_patch_done(res, toast_msg),
        )

    def _patch_debounced(self, key: str, updates_fn: Callable[[], dict[str, Any]], delay_ms: int = 300) -> None:
        if self._loading:
            return
        existing = self._debounce_timers.pop(key, None)
        if existing is not None:
            GLib.source_remove(existing)

        def fire() -> bool:
            self._debounce_timers.pop(key, None)
            self._patch_fields(updates_fn())
            return GLib.SOURCE_REMOVE

        self._debounce_timers[key] = GLib.timeout_add(delay_ms, fire)

    def _on_patch_done(self, res: ApiResult, toast_msg: str | None) -> None:
        self._set_offline(res.offline)
        if not res.ok:
            self._toast(f"Save failed: {res.error}")
            return
        if (
            isinstance(res.data, dict)
            and isinstance(res.data.get("llm"), dict)
            and "max_context_size" in res.data["llm"]
        ):
            llm_data = res.data["llm"]
            if isinstance(llm_data.get("supports_tools"), bool):
                self._config.setdefault("llm", {})["supports_tools"] = llm_data["supports_tools"]
            max_ctx = _normalize_positive_int(llm_data["max_context_size"])
            self._config.setdefault("llm", {})["max_context_size"] = max_ctx
            model = self.model_entry_row.get_text().strip()
            if model and max_ctx is not None:
                self._model_context_map[model] = max_ctx
            clamped_val, changed = self._refresh_context_size_row(clamp_current=True)
            if changed:
                self._patch_fields({"llm_context_size": clamped_val})
            self._update_tools_sensitivity()
        if toast_msg:
            suffix = " (offline)" if res.offline else ""
            self._toast(f"{toast_msg}{suffix}")

    def _save_secret(self, secret_name: str, entry: Adw.PasswordEntryRow, badge: Gtk.Label) -> None:
        value = entry.get_text().strip()
        if not value or _is_masked_placeholder(value):
            if _is_masked_placeholder(value):
                self._toast(f"{secret_name} already configured (type a new key to replace)")
            else:
                self._toast("Enter a secret value before applying")
            return
        self._flush_pending_entry_fields()

        def on_done(res: ApiResult) -> None:
            self._set_offline(res.offline)
            if not res.ok:
                self._toast(f"Secret save failed: {res.error}")
                return
            self._secrets_configured[secret_name] = True
            self._loading = True
            _set_secret_row_state(entry, badge, True)
            self._loading = False
            self._toast(f"Updated {secret_name}")

        self._run_async(lambda: self.client.put_secrets({secret_name: value}), on_done)

    # ------------------------------------------------------------------
    # Page 1: General
    # ------------------------------------------------------------------

    def _build_general_page(self) -> None:
        page = Adw.PreferencesPage(title="General", icon_name="preferences-system-symbolic")
        page.set_name("general")

        banner_group = Adw.PreferencesGroup()
        banner_group.add(self._create_offline_banner())
        page.add(banner_group)

        identity_group = Adw.PreferencesGroup(
            title="Identity & Wake Word",
            description="Customize your companion's name and open-vocabulary wake phrase.",
        )
        self.pet_name_row = Adw.EntryRow(title="Companion Name", show_apply_button=True)
        self.pet_name_row.connect(
            "apply", lambda *_: self._patch_fields({"pet_name": self.pet_name_row.get_text().strip()}, "Saved companion name")
        )
        identity_group.add(self.pet_name_row)

        self.wake_phrase_row = Adw.EntryRow(title="Custom Wake Phrase", show_apply_button=True)
        self.wake_phrase_row.connect(
            "apply",
            lambda *_: self._patch_fields(
                {"wake_phrase": self.wake_phrase_row.get_text().strip()}, "Saved wake phrase"
            ),
        )
        identity_group.add(self.wake_phrase_row)
        page.add(identity_group)

        prompt_group = Adw.PreferencesGroup(
            title="System Prompt",
            description="Instructions guiding personality, conciseness, and [emotion:<tag>] markers.",
        )
        self.system_prompt_view = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR)
        self.system_prompt_view.set_top_margin(8)
        self.system_prompt_view.set_bottom_margin(8)
        self.system_prompt_view.set_left_margin(10)
        self.system_prompt_view.set_right_margin(10)

        prompt_scroll = Gtk.ScrolledWindow(min_content_height=150)
        prompt_scroll.set_child(self.system_prompt_view)
        prompt_frame = Gtk.Frame()
        prompt_frame.set_child(prompt_scroll)
        prompt_group.add(prompt_frame)

        btn_box = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=8,
            halign=Gtk.Align.END,
            margin_top=10,
        )
        reset_btn = Gtk.Button(label="Reset Default")
        reset_btn.connect("clicked", self._on_reset_system_prompt)
        save_btn = Gtk.Button(label="Save")
        save_btn.add_css_class("suggested-action")
        save_btn.connect("clicked", self._on_save_general)
        btn_box.append(reset_btn)
        btn_box.append(save_btn)
        prompt_group.add(btn_box)

        page.add(prompt_group)
        self.add(page)

    def _on_reset_system_prompt(self, *_: object) -> None:
        self.system_prompt_view.get_buffer().set_text(DEFAULT_SYSTEM_PROMPT)
        self._toast("System prompt reset (click Save to apply)")

    def _on_save_general(self, *_: object) -> None:
        buf = self.system_prompt_view.get_buffer()
        prompt = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), True).strip()
        self._patch_fields(
            {
                "pet_name": self.pet_name_row.get_text().strip() or "Bytey",
                "wake_phrase": self.wake_phrase_row.get_text().strip() or "Hey Bytey",
                "system_prompt": prompt or DEFAULT_SYSTEM_PROMPT,
            },
            "General settings saved",
        )

    # ------------------------------------------------------------------
    # Page 2: AI & MCP
    # ------------------------------------------------------------------

    def _build_ai_mcp_page(self) -> None:
        page = Adw.PreferencesPage(title="AI & MCP", icon_name="system-run-symbolic")
        page.set_name("ai-mcp")

        banner_group = Adw.PreferencesGroup()
        banner_group.add(self._create_offline_banner())
        page.add(banner_group)

        ai_group = Adw.PreferencesGroup(title="AI Provider & Model")

        self.provider_row = _make_string_combo("Provider", "", PROVIDERS)
        self.provider_row.connect("notify::selected", self._on_provider_changed)
        ai_group.add(self.provider_row)

        self.llm_base_url_row = Adw.EntryRow(title="Base URL", show_apply_button=True)
        self.llm_base_url_row.connect(
            "apply",
            lambda *_: self._patch_fields(
                {"llm_base_url": self.llm_base_url_row.get_text().strip()}, "Saved Base URL"
            ),
        )
        ai_group.add(self.llm_base_url_row)

        self.llm_api_key_row, self.llm_api_key_badge = _make_secret_row("API Key")
        self.llm_api_key_row.connect("apply", self._on_apply_llm_api_key)
        ai_group.add(self.llm_api_key_row)

        self.model_combo_row = _make_string_combo(
            "Discovered Models",
            "Refresh to populate models from active provider",
            [""],
        )
        refresh_models_btn = Gtk.Button(label="Refresh Models", valign=Gtk.Align.CENTER)
        refresh_models_btn.connect("clicked", lambda *_: self.refresh_models())
        self.model_combo_row.add_suffix(refresh_models_btn)
        self.model_combo_row.connect("notify::selected", self._on_model_combo_selected)
        ai_group.add(self.model_combo_row)

        self.model_entry_row = Adw.EntryRow(title="Active Model", show_apply_button=True)
        self.model_entry_row.connect(
            "apply",
            lambda *_: self._apply_model_name(self.model_entry_row.get_text().strip()),
        )
        ai_group.add(self.model_entry_row)

        self.context_size_row = _make_string_combo(
            "Context Size",
            "Context window tokens (Auto uses provider default)",
            ["Auto (Default)"],
        )
        self.context_size_row.connect("notify::selected", self._on_context_size_selected)
        ai_group.add(self.context_size_row)

        self.tools_status_row = Adw.ActionRow(
            title="Tools Support",
            subtitle="Auto-detected capability for the selected model",
        )
        self.tools_badge = Gtk.Label(label="Unknown", valign=Gtk.Align.CENTER)
        self.tools_badge.add_css_class("caption")
        self.tools_badge.add_css_class("dim-label")
        self.tools_status_row.add_suffix(self.tools_badge)
        ai_group.add(self.tools_status_row)

        self.force_tools_row = Adw.SwitchRow(
            title="Force Enable Tools",
            subtitle="Override detection and expose MCP tools to this model",
        )
        self.force_tools_row.connect("notify::active", self._on_force_tools_toggled)
        ai_group.add(self.force_tools_row)

        page.add(ai_group)

        # MCP Servers structured list
        self.mcp_group = Adw.PreferencesGroup(
            title="MCP Servers",
            description="Enabled when active model supports tools or Force Enable Tools is active.",
        )
        add_mcp_btn = Gtk.Button(label="Add MCP Server", valign=Gtk.Align.CENTER)
        add_mcp_btn.add_css_class("flat")
        add_mcp_btn.connect("clicked", lambda *_: self._open_mcp_dialog(None))
        self.mcp_group.set_header_suffix(add_mcp_btn)
        page.add(self.mcp_group)

        # Raw mcpServers JSON editor
        self.mcp_json_group = Adw.PreferencesGroup(
            title="Raw mcpServers JSON",
            description="Two-way synchronized with the structured MCP server list above.",
        )
        self.mcp_json_view = Gtk.TextView(wrap_mode=Gtk.WrapMode.NONE)
        self.mcp_json_view.add_css_class("monospace")
        self.mcp_json_view.set_top_margin(8)
        self.mcp_json_view.set_bottom_margin(8)
        self.mcp_json_view.set_left_margin(10)
        self.mcp_json_view.set_right_margin(10)

        mcp_scroll = Gtk.ScrolledWindow(min_content_height=180)
        mcp_scroll.set_child(self.mcp_json_view)
        mcp_frame = Gtk.Frame()
        mcp_frame.set_child(mcp_scroll)
        self.mcp_json_group.add(mcp_frame)

        apply_json_box = Gtk.Box(halign=Gtk.Align.END, margin_top=8)
        apply_json_btn = Gtk.Button(label="Validate & Apply JSON")
        apply_json_btn.add_css_class("suggested-action")
        apply_json_btn.connect("clicked", lambda *_: self._apply_raw_mcp_json())
        apply_json_box.append(apply_json_btn)
        self.mcp_json_group.add(apply_json_box)
        page.add(self.mcp_json_group)

        self.add(page)

    def _on_provider_changed(self, *_: object) -> None:
        if self._loading:
            return
        provider = _combo_selected_text(self.provider_row)
        default_url = PROVIDER_DEFAULT_URLS.get(provider, "")
        self._model_tool_map.clear()
        self._model_context_map.clear()
        self._config.setdefault("llm", {})["max_context_size"] = None
        self._loading = True
        self.llm_base_url_row.set_text(default_url)
        self._refresh_llm_secret_indicator()
        self._loading = False
        self._refresh_context_size_row()
        self._patch_fields({"llm_provider": provider, "llm_base_url": default_url}, f"Provider: {provider}")

    def _refresh_llm_secret_indicator(self) -> None:
        provider = _combo_selected_text(self.provider_row)
        self.llm_base_url_row.set_visible(provider != "gemini")
        secret_key = PROVIDER_SECRET_MAP.get(provider)
        if not secret_key:
            self.llm_api_key_row.set_text("")
            self.llm_api_key_row.set_sensitive(False)
            self.llm_api_key_badge.set_label("Not needed")
            self.llm_api_key_badge.remove_css_class("success")
            self.llm_api_key_badge.add_css_class("dim-label")
            return
        self.llm_api_key_row.set_sensitive(True)
        _set_secret_row_state(
            self.llm_api_key_row,
            self.llm_api_key_badge,
            bool(self._secrets_configured.get(secret_key, False)),
        )

    def _on_apply_llm_api_key(self, *_: object) -> None:
        provider = _combo_selected_text(self.provider_row)
        secret_key = PROVIDER_SECRET_MAP.get(provider)
        if not secret_key:
            self._toast(f"Provider '{provider}' does not use an API key")
            return
        self._save_secret(secret_key, self.llm_api_key_row, self.llm_api_key_badge)

    def refresh_models(self) -> None:
        provider = _combo_selected_text(self.provider_row)
        base_url = "" if provider == "gemini" else self.llm_base_url_row.get_text().strip()
        self._toast(f"Fetching models for {provider}...")

        def on_done(res: ApiResult) -> None:
            self._set_offline(res.offline)
            if not res.ok:
                self._toast(f"Model fetch failed: {res.error}")
                return
            models_raw = res.data
            if isinstance(models_raw, dict):
                models_raw = models_raw.get("models", [])
            if not isinstance(models_raw, list):
                models_raw = []

            names: list[str] = []
            self._model_tool_map.clear()
            self._model_context_map.clear()
            for item in models_raw:
                if isinstance(item, str):
                    names.append(item)
                elif isinstance(item, dict):
                    mid = str(item.get("id") or item.get("name") or "")
                    if mid:
                        names.append(mid)
                        if "supports_tools" in item:
                            self._model_tool_map[mid] = bool(item["supports_tools"])
                        ctx_len = _normalize_positive_int(item.get("context_length"))
                        if ctx_len is not None:
                            self._model_context_map[mid] = ctx_len

            current = self.model_entry_row.get_text().strip()
            if current and current not in names:
                names.insert(0, current)
            if not names:
                names = [current or ""]

            self._loading = True
            self.model_combo_row.set_model(Gtk.StringList.new(names))
            if current:
                _set_combo_text(self.model_combo_row, current)
            self._loading = False
            self._update_tools_sensitivity()
            clamped_val, changed = self._refresh_context_size_row(clamp_current=True)
            if changed:
                self._patch_fields({"llm_context_size": clamped_val})
            self._toast(f"Loaded {len([n for n in names if n])} model(s)")

        self._run_async(lambda: self.client.list_models(provider, base_url), on_done)

    def _on_model_combo_selected(self, *_: object) -> None:
        if self._loading:
            return
        selected = _combo_selected_text(self.model_combo_row).strip()
        if not selected:
            return
        self._loading = True
        self.model_entry_row.set_text(selected)
        self._loading = False
        self._apply_model_name(selected)

    def _apply_model_name(self, model_name: str) -> None:
        if not model_name:
            return
        updates: dict[str, Any] = {"llm_model": model_name}
        if model_name in self._model_tool_map:
            updates["llm_supports_tools"] = self._model_tool_map[model_name]
        clamped_val, changed = self._refresh_context_size_row(clamp_current=True)
        if changed:
            updates["llm_context_size"] = clamped_val
        self._patch_fields(updates, f"Model: {model_name}")
        self._update_tools_sensitivity()

    def _refresh_context_size_row(self, *, clamp_current: bool = False) -> tuple[int | None, bool]:
        model = self.model_entry_row.get_text().strip()
        max_ctx = self._model_context_map.get(model) or _normalize_positive_int(
            self._read_field("llm_max_context_size", None)
        )
        cur_val = _normalize_positive_int(self._read_field("llm_context_size", None))
        presets = build_context_size_presets(max_ctx, cur_val)
        next_val = clamp_context_size(cur_val, presets) if clamp_current else cur_val
        changed = next_val != cur_val
        self._context_preset_values = [v for _, v in presets]

        prev_loading = self._loading
        self._loading = True
        try:
            self.context_size_row.set_model(Gtk.StringList.new([lbl for lbl, _ in presets]))
            try:
                sel_idx = self._context_preset_values.index(next_val)
            except ValueError:
                sel_idx = 0
            self.context_size_row.set_selected(sel_idx)
            self.context_size_row.set_subtitle(
                f"Max supported by model: {format_context_short(max_ctx)} ({max_ctx:,} tokens)"
                if max_ctx
                else "Context window tokens (Auto uses provider default)"
            )
        finally:
            self._loading = prev_loading
        return (next_val, changed)

    def _on_context_size_selected(self, *_: object) -> None:
        if self._loading:
            return
        idx = int(self.context_size_row.get_selected())
        if idx < 0 or idx >= len(self._context_preset_values):
            return
        val = self._context_preset_values[idx]
        label = _combo_selected_text(self.context_size_row)
        if val == _normalize_positive_int(self._read_field("llm_context_size", None)):
            return
        self._patch_fields(
            {"llm_context_size": val},
            f"Saved context size: {label}" if val else "Context size set to Auto",
        )

    def _on_force_tools_toggled(self, *_: object) -> None:
        if self._loading:
            return
        forced = self.force_tools_row.get_active()
        self._patch_fields({"llm_force_tools": forced})
        self._update_tools_sensitivity()

    def _update_tools_sensitivity(self) -> None:
        model = self.model_entry_row.get_text().strip()
        detected = self._model_tool_map.get(model)
        if detected is None:
            detected = bool(self._read_field("llm_supports_tools", False))
        forced = self.force_tools_row.get_active()
        active = bool(detected or forced)

        self.tools_badge.remove_css_class("success")
        self.tools_badge.remove_css_class("warning")
        self.tools_badge.remove_css_class("dim-label")
        if detected:
            self.tools_badge.set_label("Supported")
            self.tools_badge.add_css_class("success")
        elif forced:
            self.tools_badge.set_label("Forced On")
            self.tools_badge.add_css_class("warning")
        else:
            self.tools_badge.set_label("Not Supported")
            self.tools_badge.add_css_class("dim-label")

        self.mcp_group.set_sensitive(active)
        self.mcp_json_group.set_sensitive(active)

    def _rebuild_mcp_list(self) -> None:
        for widget in self._mcp_rows:
            self.mcp_group.remove(widget)
        self._mcp_rows.clear()

        if not self._mcp_servers:
            empty_row = Adw.ActionRow(
                title="No MCP servers configured",
                subtitle="Click 'Add MCP Server' or edit the raw mcpServers JSON below.",
            )
            self.mcp_group.add(empty_row)
            self._mcp_rows.append(empty_row)
            return

        for name, cfg in sorted(self._mcp_servers.items()):
            transport = str(cfg.get("transport") or ("http" if cfg.get("url") else "stdio"))
            target = cfg.get("url") if transport != "stdio" else " ".join(
                [str(cfg.get("command", "")), *[str(a) for a in cfg.get("args", [])]]
            ).strip()
            confirm_tag = " • Confirm required" if cfg.get("require_confirmation") else ""
            row = Adw.ActionRow(
                title=name,
                subtitle=f"{transport}: {target or '(unconfigured)'}{confirm_tag}",
            )

            enabled_switch = Gtk.Switch(
                active=not bool(cfg.get("disabled", False)),
                valign=Gtk.Align.CENTER,
                tooltip_text="Enable or disable server",
            )
            enabled_switch.connect("notify::active", self._on_mcp_toggle, name)

            test_btn = Gtk.Button(label="Test Connection", valign=Gtk.Align.CENTER)
            test_btn.connect("clicked", lambda *_, n=name: self._test_mcp_server(n))

            edit_btn = Gtk.Button(icon_name="document-edit-symbolic", valign=Gtk.Align.CENTER, tooltip_text="Edit")
            edit_btn.add_css_class("flat")
            edit_btn.connect("clicked", lambda *_, n=name: self._open_mcp_dialog(n))

            del_btn = Gtk.Button(
                icon_name="user-trash-symbolic",
                valign=Gtk.Align.CENTER,
                tooltip_text="Delete",
            )
            del_btn.add_css_class("flat")
            del_btn.add_css_class("destructive-action")
            del_btn.connect("clicked", lambda *_, n=name: self._delete_mcp_server(n))

            row.add_suffix(enabled_switch)
            row.add_suffix(test_btn)
            row.add_suffix(edit_btn)
            row.add_suffix(del_btn)
            self.mcp_group.add(row)
            self._mcp_rows.append(row)

    def _sync_mcp_json_text(self) -> None:
        raw = json.dumps({"mcpServers": self._mcp_servers}, indent=2)
        self.mcp_json_view.get_buffer().set_text(raw)

    def _push_mcp_changes(self, toast_msg: str) -> None:
        raw = json.dumps({"mcpServers": self._mcp_servers}, indent=2)
        self._sync_mcp_json_text()
        self._rebuild_mcp_list()

        def on_done(res: ApiResult) -> None:
            self._set_offline(res.offline)
            if not res.ok:
                self._toast(f"MCP update failed: {res.error}")
                return
            suffix = " (offline)" if res.offline else ""
            self._toast(f"{toast_msg}{suffix}")

        self._run_async(lambda: self.client.put_mcp_json(raw), on_done)

    def _on_mcp_toggle(self, switch: Gtk.Switch, _pspec: object, name: str) -> None:
        if self._loading or name not in self._mcp_servers:
            return
        self._mcp_servers[name]["disabled"] = not switch.get_active()
        self._push_mcp_changes(f"{'Enabled' if switch.get_active() else 'Disabled'} {name}")

    def _open_mcp_dialog(self, name: str | None) -> None:
        cfg = self._mcp_servers.get(name, {}) if name else None
        dialog = MCPServerDialog(self, self._on_mcp_dialog_saved, existing_name=name, existing_cfg=cfg)
        dialog.present(self)

    def _on_mcp_dialog_saved(
        self,
        new_name: str,
        new_cfg: dict[str, Any],
        original_name: str | None,
    ) -> None:
        if original_name and original_name != new_name:
            self._mcp_servers.pop(original_name, None)
        if original_name and original_name in self._mcp_servers and "disabled" in self._mcp_servers[original_name]:
            new_cfg["disabled"] = self._mcp_servers[original_name]["disabled"]
        self._mcp_servers[new_name] = new_cfg
        self._push_mcp_changes(f"Saved MCP server '{new_name}'")

    def _delete_mcp_server(self, name: str) -> None:
        if name in self._mcp_servers:
            self._mcp_servers.pop(name)
            self._push_mcp_changes(f"Removed MCP server '{name}'")

    def _test_mcp_server(self, name: str) -> None:
        cfg = self._mcp_servers.get(name)
        if not cfg:
            return
        self._toast(f"Testing MCP server '{name}'...")

        def on_done(res: ApiResult) -> None:
            self._set_offline(res.offline)
            if not res.ok:
                self._toast(f"Test failed for {name}: {res.error}")
                alert = Adw.AlertDialog.new(f"MCP Test Failed: {name}", res.error)
                alert.add_response("close", "Close")
                alert.present(self)
                return

            payload = res.data if isinstance(res.data, dict) else {}
            tools = payload.get("tools", [])
            if payload.get("ok") is False:
                err = str(payload.get("error") or "Connection failed")
                self._toast(f"{name}: {err}")
                alert = Adw.AlertDialog.new(f"MCP Test Failed: {name}", err)
                alert.add_response("close", "Close")
                alert.present(self)
                return

            tool_lines = []
            for t in tools:
                if isinstance(t, dict):
                    t_name = t.get("name", "unknown")
                    t_desc = t.get("description", "")
                    tool_lines.append(f"• {t_name}" + (f" — {t_desc}" if t_desc else ""))
                else:
                    tool_lines.append(f"• {t}")
            summary = "\n".join(tool_lines) if tool_lines else "Connected successfully (0 tools advertised)."
            self._toast(f"{name}: discovered {len(tool_lines)} tool(s)")
            alert = Adw.AlertDialog.new(f"MCP Connected: {name}", summary)
            alert.add_response("close", "Close")
            alert.present(self)

        self._run_async(lambda: self.client.test_mcp_server(name, cfg), on_done)

    def _apply_raw_mcp_json(self) -> None:
        buf = self.mcp_json_view.get_buffer()
        raw = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), True).strip()
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            self._toast(f"Invalid JSON: {exc}")
            return
        if not isinstance(parsed, dict):
            self._toast("Root JSON value must be an object")
            return
        servers = parsed.get("mcpServers", parsed if "mcpServers" not in parsed else {})
        if not isinstance(servers, dict):
            self._toast('"mcpServers" must be a JSON object')
            return

        def on_done(res: ApiResult) -> None:
            self._set_offline(res.offline)
            if not res.ok:
                self._toast(f"Apply failed: {res.error}")
                return
            self._mcp_servers = copy.deepcopy(servers)
            self._sync_mcp_json_text()
            self._rebuild_mcp_list()
            suffix = " (offline)" if res.offline else ""
            self._toast(f"Applied mcpServers JSON ({len(servers)} server(s)){suffix}")

        self._run_async(lambda: self.client.put_mcp_json(raw), on_done)

    # ------------------------------------------------------------------
    # Page 3: Voice Input
    # ------------------------------------------------------------------

    def _build_voice_input_page(self) -> None:
        page = Adw.PreferencesPage(title="Voice Input", icon_name="audio-input-microphone-symbolic")
        page.set_name("voice-input")

        banner_group = Adw.PreferencesGroup()
        banner_group.add(self._create_offline_banner())
        page.add(banner_group)

        mic_group = Adw.PreferencesGroup(title="Microphone & Wake-Word Detection")

        self.mic_muted_row = Adw.SwitchRow(
            title="Mute Microphone",
            subtitle="Pause continuous VAD and wake-word listening",
        )
        self.mic_muted_row.connect(
            "notify::active",
            lambda *_: self._patch_fields({"mic_muted": self.mic_muted_row.get_active()}),
        )
        mic_group.add(self.mic_muted_row)

        sens_row = Adw.ActionRow(
            title="Wake Sensitivity",
            subtitle="Higher values require closer phonetic match (0.40 – 0.95)",
        )
        self.wake_sensitivity_scale = Gtk.Scale.new_with_range(
            Gtk.Orientation.HORIZONTAL, 0.40, 0.95, 0.01
        )
        self.wake_sensitivity_scale.set_size_request(200, -1)
        self.wake_sensitivity_scale.set_draw_value(True)
        self.wake_sensitivity_scale.set_digits(2)
        self.wake_sensitivity_scale.set_valign(Gtk.Align.CENTER)
        self.wake_sensitivity_scale.connect(
            "value-changed",
            lambda *_: self._patch_debounced(
                "wake_sensitivity",
                lambda: {"wake_sensitivity": round(self.wake_sensitivity_scale.get_value(), 2)},
            ),
        )
        sens_row.add_suffix(self.wake_sensitivity_scale)
        mic_group.add(sens_row)

        self.vad_timeout_row = Adw.SpinRow.new_with_range(400, 5000, 100)
        self.vad_timeout_row.set_title("VAD Silence Timeout (ms)")
        self.vad_timeout_row.set_subtitle("Trailing silence duration before ending prompt capture")
        self.vad_timeout_row.connect(
            "notify::value",
            lambda *_: self._patch_debounced(
                "vad_timeout",
                lambda: {"vad_silence_timeout_ms": int(self.vad_timeout_row.get_value())},
            ),
        )
        mic_group.add(self.vad_timeout_row)
        page.add(mic_group)

        stt_group = Adw.PreferencesGroup(title="Speech-to-Text (STT)")

        self.stt_backend_row = _make_string_combo("STT Backend", "", STT_BACKENDS)
        self.stt_backend_row.connect(
            "notify::selected",
            lambda *_: self._patch_fields(
                {"stt_backend": _combo_selected_text(self.stt_backend_row)}, "Saved STT backend"
            ),
        )
        stt_group.add(self.stt_backend_row)

        self.whisper_model_row = Adw.EntryRow(title="Whisper Model", show_apply_button=True)
        self.whisper_model_row.connect(
            "apply",
            lambda *_: self._patch_fields(
                {"whisper_model": self.whisper_model_row.get_text().strip()}, "Saved Whisper model"
            ),
        )
        stt_group.add(self.whisper_model_row)

        self.stt_api_url_row = Adw.EntryRow(title="STT API URL", show_apply_button=True)
        self.stt_api_url_row.connect(
            "apply",
            lambda *_: self._patch_fields(
                {"stt_api_url": self.stt_api_url_row.get_text().strip()}, "Saved STT API URL"
            ),
        )
        stt_group.add(self.stt_api_url_row)

        self.stt_api_key_row, self.stt_api_key_badge = _make_secret_row("STT API Key")
        self.stt_api_key_row.connect(
            "apply",
            lambda *_: self._save_secret("STT_API_KEY", self.stt_api_key_row, self.stt_api_key_badge),
        )
        stt_group.add(self.stt_api_key_row)

        page.add(stt_group)
        self.add(page)

    # ------------------------------------------------------------------
    # Page 4: Voice Output
    # ------------------------------------------------------------------

    def _build_voice_output_page(self) -> None:
        page = Adw.PreferencesPage(title="Voice Output", icon_name="audio-speakers-symbolic")
        page.set_name("voice-output")

        banner_group = Adw.PreferencesGroup()
        banner_group.add(self._create_offline_banner())
        page.add(banner_group)

        tts_group = Adw.PreferencesGroup(title="Text-to-Speech (TTS)")
        self.tts_muted_row = Adw.SwitchRow(
            title="Mute Voice (TTS)",
            subtitle="Keep text transcripts without audio playback",
        )
        self.tts_muted_row.connect(
            "notify::active",
            lambda *_: self._patch_fields({"tts_muted": self.tts_muted_row.get_active()}),
        )
        tts_group.add(self.tts_muted_row)

        self.tts_provider_row = _make_string_combo("TTS Provider", "", TTS_PROVIDERS)
        self.tts_provider_row.connect(
            "notify::selected",
            lambda *_: self._patch_fields(
                {"tts_provider": _combo_selected_text(self.tts_provider_row)}, "Saved TTS provider"
            ),
        )
        tts_group.add(self.tts_provider_row)
        page.add(tts_group)

        # Piper Local TTS
        self.piper_group = Adw.PreferencesGroup(
            title="Piper Local TTS (Offline / Low-CPU)",
            description=(
                "Fast offline neural voices with multi-language catalog, model downloads, "
                "custom .onnx paths, and voice style controls."
            ),
        )
        self.piper_lang_combo = _make_string_combo(
            "Voice Language",
            "Filter available Piper voices by language",
            ["All Languages"],
            searchable=True,
        )
        self.piper_lang_combo.connect("notify::selected", self._on_piper_lang_selected)
        self.piper_group.add(self.piper_lang_combo)

        self.piper_voice_combo = _make_string_combo(
            "Piper Voices",
            "Select a voice model (search by name, language, or quality)",
            [""],
            searchable=True,
        )
        piper_refresh_btn = Gtk.Button(label="Refresh Catalog", valign=Gtk.Align.CENTER)
        piper_refresh_btn.connect(
            "clicked", lambda *_: self._refresh_tts_voices("piper", refresh=True)
        )
        self.piper_download_btn = Gtk.Button(label="Download", valign=Gtk.Align.CENTER)
        self.piper_download_btn.set_sensitive(False)
        self.piper_download_btn.connect(
            "clicked", lambda *_: self._download_selected_piper_model()
        )
        self.piper_voice_combo.add_suffix(piper_refresh_btn)
        self.piper_voice_combo.add_suffix(self.piper_download_btn)
        self.piper_voice_combo.connect("notify::selected", self._on_piper_voice_selected)
        self.piper_group.add(self.piper_voice_combo)

        self.piper_model_row = Adw.EntryRow(
            title="Active Piper Model / .onnx Path",
            show_apply_button=True,
        )
        self.piper_model_row.connect("apply", lambda *_: self._on_piper_model_applied())
        self.piper_group.add(self.piper_model_row)

        self.piper_preset_combo = _make_string_combo(
            "Voice Style (Funny Presets)",
            "Quick speed, pitch, and expressiveness presets",
            [label for _, label, _, _, _ in PIPER_VOICE_PRESETS],
        )
        self.piper_preset_combo.connect("notify::selected", self._on_piper_preset_selected)
        self.piper_group.add(self.piper_preset_combo)

        piper_speed_row, self.piper_speed_scale = _make_scale_row(
            "Piper Speed",
            "Speech rate multiplier (0.40 – 2.50, 1.00 normal)",
            0.40,
            2.50,
            0.05,
            2,
            lambda s: self._on_piper_slider_changed(s, "piper_speed"),
        )
        self.piper_group.add(piper_speed_row)

        piper_pitch_row, self.piper_pitch_scale = _make_scale_row(
            "Piper Pitch",
            "Voice pitch in semitones (-12 deep monster, 0 normal, +12 chipmunk)",
            -12.0,
            12.0,
            0.5,
            1,
            lambda s: self._on_piper_slider_changed(s, "piper_pitch"),
        )
        self.piper_group.add(piper_pitch_row)

        piper_noise_row, self.piper_noise_scale = _make_scale_row(
            "Piper Expressiveness",
            "Expressiveness (0.10 robotic – 0.67 natural – 1.20 animated)",
            0.0,
            1.5,
            0.05,
            2,
            lambda s: self._on_piper_slider_changed(s, "piper_noise_scale"),
        )
        self.piper_group.add(piper_noise_row)

        piper_test_row = Adw.ActionRow(
            title="Preview Piper Voice",
            subtitle="Speak a sample phrase using the active Piper voice and style",
        )
        self.piper_test_btn = Gtk.Button(label="Test Voice", valign=Gtk.Align.CENTER)
        self.piper_test_btn.connect("clicked", lambda *_: self._test_piper_voice())
        piper_test_row.add_suffix(self.piper_test_btn)
        self.piper_group.add(piper_test_row)

        self.piper_models_dir_row = Adw.EntryRow(
            title="Piper Models Directory (Optional)",
            show_apply_button=True,
        )
        self.piper_models_dir_row.connect(
            "apply", lambda *_: self._on_piper_models_dir_applied()
        )
        self.piper_group.add(self.piper_models_dir_row)
        page.add(self.piper_group)

        # Kokoro
        kokoro_group = Adw.PreferencesGroup(title="Kokoro HTTP")
        self.kokoro_url_row = Adw.EntryRow(title="Kokoro Base URL", show_apply_button=True)
        self.kokoro_url_row.connect(
            "apply",
            lambda *_: self._patch_fields(
                {"kokoro_base_url": self.kokoro_url_row.get_text().strip()}, "Saved Kokoro URL"
            ),
        )
        self.kokoro_url_row.connect(
            "changed",
            lambda *_: self._patch_debounced(
                "kokoro_base_url",
                lambda: {"kokoro_base_url": self.kokoro_url_row.get_text().strip()},
                600,
            ),
        )
        kokoro_group.add(self.kokoro_url_row)

        self.kokoro_api_key_row, self.kokoro_api_key_badge = _make_secret_row("Kokoro API Key")
        self.kokoro_api_key_row.connect(
            "apply",
            lambda *_: self._save_secret(
                "KOKORO_API_KEY", self.kokoro_api_key_row, self.kokoro_api_key_badge
            ),
        )
        kokoro_group.add(self.kokoro_api_key_row)

        self.kokoro_voice_combo = _make_string_combo("Kokoro Voices", "", [""])
        kokoro_refresh_btn = Gtk.Button(label="Refresh Voices", valign=Gtk.Align.CENTER)
        kokoro_refresh_btn.connect("clicked", lambda *_: self._refresh_tts_voices("kokoro"))
        self.kokoro_voice_combo.add_suffix(kokoro_refresh_btn)
        self.kokoro_voice_combo.connect("notify::selected", self._on_kokoro_combo_selected)
        kokoro_group.add(self.kokoro_voice_combo)

        self.kokoro_voice_row = Adw.EntryRow(title="Kokoro Voice", show_apply_button=True)
        self.kokoro_voice_row.connect(
            "apply",
            lambda *_: self._patch_fields(
                {"kokoro_voice": self.kokoro_voice_row.get_text().strip()}, "Saved Kokoro voice"
            ),
        )
        kokoro_group.add(self.kokoro_voice_row)

        speed_row = Adw.ActionRow(title="Kokoro Speed", subtitle="Playback speech rate (0.6 – 1.8)")
        self.kokoro_speed_scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0.6, 1.8, 0.05)
        self.kokoro_speed_scale.set_size_request(200, -1)
        self.kokoro_speed_scale.set_draw_value(True)
        self.kokoro_speed_scale.set_digits(2)
        self.kokoro_speed_scale.set_valign(Gtk.Align.CENTER)
        self.kokoro_speed_scale.connect(
            "value-changed",
            lambda *_: self._patch_debounced(
                "kokoro_speed",
                lambda: {"kokoro_speed": round(self.kokoro_speed_scale.get_value(), 2)},
            ),
        )
        speed_row.add_suffix(self.kokoro_speed_scale)
        kokoro_group.add(speed_row)
        page.add(kokoro_group)

        # ElevenLabs
        el_group = Adw.PreferencesGroup(title="ElevenLabs")
        self.el_api_key_row, self.el_api_key_badge = _make_secret_row("ElevenLabs API Key")
        self.el_api_key_row.connect(
            "apply",
            lambda *_: self._save_secret("ELEVENLABS_API_KEY", self.el_api_key_row, self.el_api_key_badge),
        )
        el_group.add(self.el_api_key_row)

        self.el_voice_combo = _make_string_combo("ElevenLabs Voices", "", [""])
        el_refresh_btn = Gtk.Button(label="Refresh Voices", valign=Gtk.Align.CENTER)
        el_refresh_btn.connect("clicked", lambda *_: self._refresh_tts_voices("elevenlabs"))
        self.el_voice_combo.add_suffix(el_refresh_btn)
        self.el_voice_combo.connect("notify::selected", self._on_el_combo_selected)
        el_group.add(self.el_voice_combo)

        self.el_voice_id_row = Adw.EntryRow(title="ElevenLabs Voice ID", show_apply_button=True)
        self.el_voice_id_row.connect(
            "apply",
            lambda *_: self._patch_fields(
                {"elevenlabs_voice_id": self.el_voice_id_row.get_text().strip()}, "Saved ElevenLabs voice"
            ),
        )
        el_group.add(self.el_voice_id_row)

        self.el_model_row = Adw.EntryRow(title="ElevenLabs Model ID", show_apply_button=True)
        self.el_model_row.connect(
            "apply",
            lambda *_: self._patch_fields(
                {"elevenlabs_model_id": self.el_model_row.get_text().strip()}, "Saved ElevenLabs model"
            ),
        )
        el_group.add(self.el_model_row)

        page.add(el_group)
        self.add(page)

    def _refresh_tts_voices(self, provider: str, *, refresh: bool = False) -> None:
        quiet = provider == "piper" and self._suppress_piper_refresh_toast
        base_url = self.kokoro_url_row.get_text().strip() if provider == "kokoro" else ""
        pending_secret_key = ""
        pending_secret_val = ""
        pending_piper_patch: dict[str, Any] | None = None
        if provider == "kokoro":
            pending_secret_val = self.kokoro_api_key_row.get_text().strip()
            if pending_secret_val and not _is_masked_placeholder(pending_secret_val):
                pending_secret_key = "KOKORO_API_KEY"
            else:
                pending_secret_val = ""
            if base_url and base_url != str(self._read_field("kokoro_base_url", "")):
                self._patch_fields({"kokoro_base_url": base_url})
        elif provider == "elevenlabs":
            pending_secret_val = self.el_api_key_row.get_text().strip()
            if pending_secret_val and not _is_masked_placeholder(pending_secret_val):
                pending_secret_key = "ELEVENLABS_API_KEY"
            else:
                pending_secret_val = ""
        elif provider == "piper":
            models_dir = self.piper_models_dir_row.get_text().strip()
            if models_dir != str(self._read_field("piper_models_dir", "")):
                pending_piper_patch = self._build_patch({"piper_models_dir": models_dir})
        if not quiet:
            action = "Refreshing" if refresh else "Fetching"
            self._toast(f"{action} {provider} voices...")

        def fetch_task() -> ApiResult:
            if pending_secret_key and pending_secret_val:
                sec_res = self.client.put_secrets({pending_secret_key: pending_secret_val})
                if sec_res.ok:
                    self._secrets_configured[pending_secret_key] = True
            if pending_piper_patch:
                self.client.patch_config(pending_piper_patch)
            return self.client.list_tts_voices(provider, base_url, refresh=refresh)

        def on_done(res: ApiResult) -> None:
            if pending_secret_key and self._secrets_configured.get(pending_secret_key):
                self._loading = True
                if pending_secret_key == "KOKORO_API_KEY":
                    _set_secret_row_state(self.kokoro_api_key_row, self.kokoro_api_key_badge, True)
                elif pending_secret_key == "ELEVENLABS_API_KEY":
                    _set_secret_row_state(self.el_api_key_row, self.el_api_key_badge, True)
                self._loading = False
            self._set_offline(res.offline)
            if not res.ok:
                if not quiet:
                    self._toast(f"Voice fetch failed: {res.error}")
                return
            if provider == "piper":
                self._apply_piper_voices_payload(res.data, quiet=quiet)
                return

            raw = res.data
            if isinstance(raw, dict):
                raw = raw.get("voices", [])
            if not isinstance(raw, list):
                raw = []

            if provider == "kokoro":
                voices: list[str] = []
                for item in raw:
                    if isinstance(item, str):
                        voices.append(item)
                    elif isinstance(item, dict):
                        vid = str(item.get("id") or item.get("voice_id") or item.get("name") or "")
                        if vid:
                            voices.append(vid)
                current = self.kokoro_voice_row.get_text().strip()
                if current and current not in voices:
                    voices.insert(0, current)
                self._loading = True
                self.kokoro_voice_combo.set_model(Gtk.StringList.new(voices or [""]))
                if current:
                    _set_combo_text(self.kokoro_voice_combo, current)
                self._loading = False
                self._toast(f"Loaded {len(voices)} Kokoro voice(s)")
            else:
                pairs: list[tuple[str, str]] = []
                for item in raw:
                    if isinstance(item, str):
                        pairs.append((item, item))
                    elif isinstance(item, dict):
                        vid = str(item.get("voice_id") or item.get("id") or "")
                        vname = str(item.get("name") or vid)
                        if vid:
                            pairs.append((vid, f"{vname} ({vid})"))
                self._elevenlabs_voices = pairs
                labels = [lbl for _, lbl in pairs] or [""]
                current_id = self.el_voice_id_row.get_text().strip()
                self._loading = True
                self.el_voice_combo.set_model(Gtk.StringList.new(labels))
                for idx, (vid, _) in enumerate(pairs):
                    if vid == current_id:
                        self.el_voice_combo.set_selected(idx)
                        break
                self._loading = False
                self._toast(f"Loaded {len(pairs)} ElevenLabs voice(s)")

        self._run_async(fetch_task, on_done)

    def _apply_piper_voices_payload(self, data: Any, *, quiet: bool = False) -> None:
        payload = data if isinstance(data, dict) else {"voices": data}
        voices_raw = payload.get("voices", []) if isinstance(payload, dict) else []
        langs_raw = payload.get("languages", []) if isinstance(payload, dict) else []
        backend = str(payload.get("backend") or "").strip() if isinstance(payload, dict) else ""
        models_dir = str(payload.get("models_dir") or "").strip() if isinstance(payload, dict) else ""

        normalized: list[dict[str, Any]] = []
        if isinstance(voices_raw, list):
            for item in voices_raw:
                parsed = _normalize_piper_voice(item)
                if parsed is not None:
                    normalized.append(parsed)
        self._piper_all_voices = normalized

        meta_parts: list[str] = []
        if backend:
            meta_parts.append(f"Backend: {backend}")
        if models_dir:
            meta_parts.append(f"Models dir: {models_dir}")
        self.piper_voice_combo.set_subtitle(
            " · ".join(meta_parts)
            if meta_parts
            else "Select a voice model (search by name, language, or quality)"
        )

        lang_map: dict[str, str] = {}
        if isinstance(langs_raw, list):
            for item in langs_raw:
                if isinstance(item, dict):
                    code = str(item.get("code") or item.get("id") or "").strip()
                    label = str(item.get("label") or item.get("name") or "").strip()
                elif isinstance(item, (tuple, list)) and len(item) >= 2:
                    code = str(item[0]).strip()
                    label = str(item[1]).strip()
                elif isinstance(item, str):
                    code = item.strip()
                    label = ""
                else:
                    continue
                if code:
                    lang_map.setdefault(code, label or code)
        for voice in normalized:
            code = str(voice.get("lang_code") or "").strip()
            if code and code not in lang_map:
                lang_map[code] = str(voice.get("lang_label") or code)

        prev_lang = self._selected_piper_language_code()
        languages: list[tuple[str, str]] = [("", "All Languages")]
        for code, label in sorted(lang_map.items(), key=lambda kv: (kv[1].lower(), kv[0].lower())):
            display = f"{label} · {code}" if label and label != code else code
            languages.append((code, display))
        self._piper_languages = languages

        selected_lang_idx = next(
            (i for i, (code, _) in enumerate(languages) if code == prev_lang),
            0,
        )
        was_loading = self._loading
        self._loading = True
        try:
            self.piper_lang_combo.set_model(
                Gtk.StringList.new([lbl for _, lbl in languages])
            )
            self.piper_lang_combo.set_selected(selected_lang_idx)
        finally:
            self._loading = was_loading

        self._apply_piper_language_filter()
        if not quiet:
            self._toast(f"Loaded {len(normalized)} Piper voice(s)")

    def _selected_piper_language_code(self) -> str:
        idx = self.piper_lang_combo.get_selected()
        if 0 <= idx < len(self._piper_languages):
            return self._piper_languages[idx][0]
        return ""

    def _selected_piper_voice(self) -> dict[str, Any] | None:
        idx = self.piper_voice_combo.get_selected()
        if 0 <= idx < len(self._piper_filtered_voices):
            return self._piper_filtered_voices[idx]
        return None

    def _apply_piper_language_filter(self) -> None:
        lang_code = self._selected_piper_language_code()
        if lang_code:
            filtered = [v for v in self._piper_all_voices if v.get("lang_code") == lang_code]
        else:
            filtered = list(self._piper_all_voices)
        self._piper_filtered_voices = filtered

        labels = [_format_piper_voice_label(v) for v in filtered] or [""]
        current_model = self.piper_model_row.get_text().strip()
        match_idx = next(
            (i for i, v in enumerate(filtered) if v.get("id") == current_model),
            0,
        )
        was_loading = self._loading
        self._loading = True
        try:
            self.piper_voice_combo.set_model(Gtk.StringList.new(labels))
            self.piper_voice_combo.set_selected(match_idx)
        finally:
            self._loading = was_loading
        self._sync_piper_download_button()

    def _sync_piper_download_button(self) -> None:
        if self._piper_downloading:
            return
        voice = self._selected_piper_voice()
        if voice is not None:
            installed = bool(voice.get("installed"))
            self.piper_download_btn.set_label("Installed" if installed else "Download")
            self.piper_download_btn.set_sensitive(not installed)
            return

        entry_val = self.piper_model_row.get_text().strip()
        matching = next(
            (v for v in self._piper_all_voices if v.get("id") == entry_val),
            None,
        )
        if matching is not None:
            installed = bool(matching.get("installed"))
            self.piper_download_btn.set_label("Installed" if installed else "Download")
            self.piper_download_btn.set_sensitive(not installed)
        else:
            self.piper_download_btn.set_label("Download")
            self.piper_download_btn.set_sensitive(False)

    def _on_piper_lang_selected(self, *_: object) -> None:
        if self._loading:
            return
        self._apply_piper_language_filter()

    def _on_piper_voice_selected(self, *_: object) -> None:
        if self._loading:
            return
        voice = self._selected_piper_voice()
        if voice is None:
            self._sync_piper_download_button()
            return
        vid = str(voice.get("id") or "").strip()
        if not vid:
            return
        self._loading = True
        self.piper_model_row.set_text(vid)
        self._loading = False
        self._sync_piper_download_button()
        self._patch_fields({"piper_model": vid}, f"Piper voice: {vid}")

    def _on_piper_model_applied(self) -> None:
        val = self.piper_model_row.get_text().strip()
        if not val:
            self._toast("Piper model cannot be empty")
            return
        match_idx = next(
            (i for i, v in enumerate(self._piper_filtered_voices) if v.get("id") == val),
            None,
        )
        if match_idx is not None:
            self._loading = True
            self.piper_voice_combo.set_selected(match_idx)
            self._loading = False
        self._sync_piper_download_button()
        self._patch_fields({"piper_model": val}, f"Saved Piper model: {val}")

    def _on_piper_models_dir_applied(self) -> None:
        if self._loading:
            return
        models_dir = self.piper_models_dir_row.get_text().strip()
        patch = self._build_patch({"piper_models_dir": models_dir})

        def save_task() -> ApiResult:
            return self.client.patch_config(patch)

        def on_done(res: ApiResult) -> None:
            self._on_patch_done(res, "Saved Piper models directory")
            if res.ok and not res.offline:
                self._suppress_piper_refresh_toast = True
                try:
                    self._refresh_tts_voices("piper", refresh=False)
                finally:
                    self._suppress_piper_refresh_toast = False

        self._run_async(save_task, on_done)

    def _download_selected_piper_model(self) -> None:
        voice = self._selected_piper_voice()
        if voice is not None:
            model_id = str(voice.get("id") or "").strip()
        else:
            entry_val = self.piper_model_row.get_text().strip()
            model_id = (
                entry_val
                if entry_val and not entry_val.endswith(".onnx") and "/" not in entry_val
                else ""
            )
        if not model_id:
            self._toast("Select a Piper catalog voice to download")
            return

        self._piper_downloading = True
        self.piper_download_btn.set_sensitive(False)
        self.piper_download_btn.set_label("Downloading...")
        self._toast(f"Downloading Piper voice {model_id}...")

        def on_done(res: ApiResult) -> None:
            self._piper_downloading = False
            self._set_offline(res.offline)
            if not res.ok:
                self._sync_piper_download_button()
                self._toast(f"Download failed: {res.error}")
                return
            for item in self._piper_all_voices:
                if item.get("id") == model_id:
                    item["installed"] = True
            self._loading = True
            self.piper_model_row.set_text(model_id)
            self._loading = False
            self._apply_piper_language_filter()
            self._patch_fields({"piper_model": model_id})
            self._toast(f"Downloaded Piper voice: {model_id}")
            self._suppress_piper_refresh_toast = True
            try:
                self._refresh_tts_voices("piper", refresh=False)
            finally:
                self._suppress_piper_refresh_toast = False

        self._run_async(lambda: self.client.download_piper_model(model_id), on_done)

    def _sync_piper_preset_combo(
        self, speed: float, pitch: float, noise_scale: float
    ) -> None:
        idx = _match_piper_preset_index(speed, pitch, noise_scale)
        was_loading = self._loading
        self._loading = True
        try:
            self.piper_preset_combo.set_selected(idx)
        finally:
            self._loading = was_loading

    def _on_piper_preset_selected(self, *_: object) -> None:
        if self._loading:
            return
        idx = self.piper_preset_combo.get_selected()
        if not (0 <= idx < len(PIPER_VOICE_PRESETS)):
            return
        _, label, speed, pitch, noise = PIPER_VOICE_PRESETS[idx]
        if speed is None or pitch is None or noise is None:
            return
        for key in ("piper_speed", "piper_pitch", "piper_noise_scale"):
            timer_id = self._debounce_timers.pop(key, None)
            if timer_id is not None:
                GLib.source_remove(timer_id)
        self._loading = True
        try:
            self.piper_speed_scale.set_value(speed)
            self.piper_pitch_scale.set_value(pitch)
            self.piper_noise_scale.set_value(noise)
        finally:
            self._loading = False
        self._patch_fields(
            {
                "piper_speed": speed,
                "piper_pitch": pitch,
                "piper_noise_scale": noise,
            },
            f"Piper style: {label}",
        )

    def _on_piper_slider_changed(self, _scale: Gtk.Scale, field: str) -> None:
        if self._loading:
            return
        speed = round(self.piper_speed_scale.get_value(), 2)
        pitch = round(self.piper_pitch_scale.get_value(), 1)
        noise = round(self.piper_noise_scale.get_value(), 3)
        self._sync_piper_preset_combo(speed, pitch, noise)
        values = {
            "piper_speed": speed,
            "piper_pitch": pitch,
            "piper_noise_scale": noise,
        }
        val = values[field]
        self._patch_debounced(field, lambda: {field: val})

    def _test_piper_voice(self) -> None:
        if self.tts_muted_row.get_active():
            self._toast("Voice (TTS) is muted — unmute TTS to preview Piper voice")
            return
        if _combo_selected_text(self.tts_provider_row) != "piper":
            self._toast("Select 'piper' as TTS Provider above to preview Piper voice")
            return

        for key in ("piper_speed", "piper_pitch", "piper_noise_scale"):
            timer_id = self._debounce_timers.pop(key, None)
            if timer_id is not None:
                GLib.source_remove(timer_id)

        speed = round(self.piper_speed_scale.get_value(), 2)
        pitch = round(self.piper_pitch_scale.get_value(), 1)
        noise = round(self.piper_noise_scale.get_value(), 3)
        model_txt = self.piper_model_row.get_text().strip()
        models_dir_txt = self.piper_models_dir_row.get_text().strip()

        dirty: dict[str, Any] = {}
        if speed != float(self._read_field("piper_speed", 1.0)):
            dirty["piper_speed"] = speed
        if pitch != float(self._read_field("piper_pitch", 0.0)):
            dirty["piper_pitch"] = pitch
        if noise != float(self._read_field("piper_noise_scale", 0.667)):
            dirty["piper_noise_scale"] = noise
        if model_txt and model_txt != str(self._read_field("piper_model", "")):
            dirty["piper_model"] = model_txt
        if models_dir_txt != str(self._read_field("piper_models_dir", "")):
            dirty["piper_models_dir"] = models_dir_txt
        pending_patch = self._build_patch(dirty) if dirty else None

        voice = self._selected_piper_voice()
        if voice is not None and voice.get("id") == model_txt:
            lang_code = str(voice.get("lang_code") or "")
        else:
            lang_code = self._selected_piper_language_code() or model_txt
        sample_text = _piper_sample_text(lang_code)

        self.piper_test_btn.set_sensitive(False)
        self.piper_test_btn.set_label("Playing...")

        def preview_task() -> ApiResult:
            if pending_patch:
                patch_res = self.client.patch_config(pending_patch)
                if not patch_res.ok:
                    return patch_res
            return self.client.replay_tts(sample_text)

        def on_done(res: ApiResult) -> None:
            self.piper_test_btn.set_sensitive(True)
            self.piper_test_btn.set_label("Test Voice")
            self._set_offline(res.offline)
            if not res.ok:
                self._toast(f"Preview failed: {res.error}")
            else:
                self._toast("Playing Piper voice preview")

        self._run_async(preview_task, on_done)

    def _on_kokoro_combo_selected(self, *_: object) -> None:
        if self._loading:
            return
        voice = _combo_selected_text(self.kokoro_voice_combo).strip()
        if not voice:
            return
        self._loading = True
        self.kokoro_voice_row.set_text(voice)
        self._loading = False
        self._patch_fields({"kokoro_voice": voice}, f"Kokoro voice: {voice}")

    def _on_el_combo_selected(self, *_: object) -> None:
        if self._loading:
            return
        idx = self.el_voice_combo.get_selected()
        if 0 <= idx < len(self._elevenlabs_voices):
            vid, _ = self._elevenlabs_voices[idx]
            self._loading = True
            self.el_voice_id_row.set_text(vid)
            self._loading = False
            self._patch_fields({"elevenlabs_voice_id": vid}, f"ElevenLabs voice: {vid}")

    # ------------------------------------------------------------------
    # Page 5: Visuals & Position
    # ------------------------------------------------------------------

    def _build_visuals_page(self) -> None:
        page = Adw.PreferencesPage(title="Visuals & Position", icon_name="preferences-desktop-display-symbolic")
        page.set_name("visuals")

        banner_group = Adw.PreferencesGroup()
        banner_group.add(self._create_offline_banner())
        page.add(banner_group)

        pos_group = Adw.PreferencesGroup(title="Island Placement")
        self.position_mode_row = _make_string_combo(
            "Mode", "", [label for label, _ in POSITION_MODE_OPTIONS]
        )
        self.position_mode_row.connect("notify::selected", self._on_position_mode_changed)
        pos_group.add(self.position_mode_row)

        self.monitor_index_row = Adw.SpinRow.new_with_range(0, 15, 1)
        self.monitor_index_row.set_title("Monitor Index")
        self.monitor_index_row.connect(
            "notify::value",
            lambda *_: self._patch_debounced(
                "monitor_index",
                lambda: {"monitor_index": int(self.monitor_index_row.get_value())},
            ),
        )
        pos_group.add(self.monitor_index_row)
        page.add(pos_group)

        self.fixed_group = Adw.PreferencesGroup(title="Fixed Alignment")
        self.fixed_alignment_row = _make_string_combo("Fixed Alignment", "", FIXED_ALIGNMENTS)
        self.fixed_alignment_row.connect(
            "notify::selected",
            lambda *_: self._patch_fields(
                {"fixed_alignment": _combo_selected_text(self.fixed_alignment_row)}, "Saved alignment"
            ),
        )
        self.fixed_group.add(self.fixed_alignment_row)

        self.offset_x_row = Adw.SpinRow.new_with_range(-4000, 4000, 4)
        self.offset_x_row.set_title("Offset X")
        self.offset_x_row.connect(
            "notify::value",
            lambda *_: self._patch_debounced("offset_x", lambda: {"offset_x": int(self.offset_x_row.get_value())}),
        )
        self.fixed_group.add(self.offset_x_row)

        self.offset_y_row = Adw.SpinRow.new_with_range(-4000, 4000, 4)
        self.offset_y_row.set_title("Offset Y")
        self.offset_y_row.connect(
            "notify::value",
            lambda *_: self._patch_debounced("offset_y", lambda: {"offset_y": int(self.offset_y_row.get_value())}),
        )
        self.fixed_group.add(self.offset_y_row)
        page.add(self.fixed_group)

        self.floating_group = Adw.PreferencesGroup(title="Floating Coordinates")
        self.float_x_row = Adw.SpinRow.new_with_range(0, 10000, 10)
        self.float_x_row.set_title("Floating X")
        self.float_x_row.connect(
            "notify::value",
            lambda *_: self._patch_debounced("float_x", lambda: {"float_x": int(self.float_x_row.get_value())}),
        )
        self.floating_group.add(self.float_x_row)

        self.float_y_row = Adw.SpinRow.new_with_range(0, 10000, 10)
        self.float_y_row.set_title("Floating Y")
        self.float_y_row.connect(
            "notify::value",
            lambda *_: self._patch_debounced("float_y", lambda: {"float_y": int(self.float_y_row.get_value())}),
        )
        self.floating_group.add(self.float_y_row)
        page.add(self.floating_group)

        eye_group = Adw.PreferencesGroup(title="Eye Animations")
        self.anim_mode_row = _make_string_combo("Animation Mode", "", ANIMATION_MODES)
        self.anim_mode_row.connect(
            "notify::selected",
            lambda *_: self._patch_fields(
                {"animation_mode": _combo_selected_text(self.anim_mode_row)}, "Saved animation mode"
            ),
        )
        eye_group.add(self.anim_mode_row)

        self.cursor_tracking_row = Adw.SwitchRow(
            title="Cursor Tracking",
            subtitle="Subtle eye glance toward pointer while idle in Full mode",
        )
        self.cursor_tracking_row.connect(
            "notify::active",
            lambda *_: self._patch_fields({"cursor_tracking": self.cursor_tracking_row.get_active()}),
        )
        eye_group.add(self.cursor_tracking_row)
        page.add(eye_group)

        colors_group = Adw.PreferencesGroup(
            title="Colors",
            description="Customize eye, island background, and border colors.",
        )
        reset_colors_btn = Gtk.Button(label="Reset Defaults", valign=Gtk.Align.CENTER)
        reset_colors_btn.add_css_class("flat")
        reset_colors_btn.connect("clicked", self._on_reset_colors)
        colors_group.set_header_suffix(reset_colors_btn)
        colors_group.add(self._make_color_row("Eye Color", "eye_color", DEFAULT_EYE_COLOR))
        colors_group.add(self._make_color_row("Background Color", "background_color", DEFAULT_BG_COLOR))
        colors_group.add(self._make_color_row("Border Color", "border_color", DEFAULT_BORDER_COLOR))
        page.add(colors_group)

        chat_group = Adw.PreferencesGroup(
            title="Chat Window & Density",
            description="Resize the transcript drawer or adapt its UI density to your desktop.",
        )
        self.chat_density_row = _make_string_combo("Chat Density", "", CHAT_DENSITIES)
        self.chat_density_row.connect(
            "notify::selected",
            lambda *_: self._patch_fields(
                {"chat_density": _combo_selected_text(self.chat_density_row)}, "Saved chat density"
            ),
        )
        chat_group.add(self.chat_density_row)

        self.chat_width_row = Adw.SpinRow.new_with_range(260, 900, 10)
        self.chat_width_row.set_title("Chat Width (px)")
        self.chat_width_row.connect(
            "notify::value",
            lambda *_: self._patch_debounced(
                "chat_width", lambda: {"chat_width": int(self.chat_width_row.get_value())}
            ),
        )
        chat_group.add(self.chat_width_row)

        self.chat_height_row = Adw.SpinRow.new_with_range(220, 900, 10)
        self.chat_height_row.set_title("Chat Height (px)")
        self.chat_height_row.connect(
            "notify::value",
            lambda *_: self._patch_debounced(
                "chat_height", lambda: {"chat_height": int(self.chat_height_row.get_value())}
            ),
        )
        chat_group.add(self.chat_height_row)
        page.add(chat_group)

        self.add(page)

    def _make_color_row(self, title: str, field_key: str, default_hex: str) -> Adw.ActionRow:
        """Create an ActionRow with a ColorDialogButton bound to a color config key."""
        row = Adw.ActionRow(title=title, subtitle=default_hex)
        btn = Gtk.ColorDialogButton(
            dialog=Gtk.ColorDialog(with_alpha=True),
            valign=Gtk.Align.CENTER,
        )
        btn.set_rgba(_hex_to_rgba(default_hex, default_hex))

        def on_color_changed(*_: object) -> None:
            if self._loading:
                return
            hex_val = _rgba_to_hex(btn.get_rgba())
            row.set_subtitle(hex_val)
            self._patch_fields({field_key: hex_val}, f"Saved {title.lower()}")

        btn.connect("notify::rgba", on_color_changed)
        row.add_suffix(btn)
        self._color_controls[field_key] = (row, btn, default_hex)
        return row

    def _on_reset_colors(self, *_: object) -> None:
        """Reset all color pickers to defaults and persist them in a single patch."""
        self._loading = True
        try:
            for row, btn, default_hex in self._color_controls.values():
                btn.set_rgba(_hex_to_rgba(default_hex, default_hex))
                row.set_subtitle(default_hex)
        finally:
            self._loading = False
        self._patch_fields(
            {
                "eye_color": DEFAULT_EYE_COLOR,
                "background_color": DEFAULT_BG_COLOR,
                "border_color": DEFAULT_BORDER_COLOR,
            },
            "Reset colors to defaults",
        )

    def _selected_position_mode(self) -> str:
        """Return the config position_mode value for the selected Mode row."""
        idx = self.position_mode_row.get_selected()
        if 0 <= idx < len(POSITION_MODE_OPTIONS):
            return POSITION_MODE_OPTIONS[idx][1]
        return "fixed"

    def _set_position_mode_selection(self, mode: str) -> None:
        """Select the Mode dropdown option matching the normalized position_mode."""
        norm = str(mode or "fixed").strip().lower()
        for idx, (_, value) in enumerate(POSITION_MODE_OPTIONS):
            if value == norm:
                self.position_mode_row.set_selected(idx)
                return
        self.position_mode_row.set_selected(0)

    def _on_position_mode_changed(self, *_: object) -> None:
        mode = self._selected_position_mode()
        self._sync_position_groups(mode)
        if not self._loading:
            self._patch_fields({"position_mode": mode}, f"Mode: {mode.capitalize()}")

    def _sync_position_groups(self, mode: str) -> None:
        norm = str(mode or "fixed").strip().lower()
        self.fixed_group.set_sensitive(norm == "fixed")
        self.floating_group.set_sensitive(norm == "floating")

    # ------------------------------------------------------------------
    # Page 6: Import / Export
    # ------------------------------------------------------------------

    def _build_import_export_page(self) -> None:
        page = Adw.PreferencesPage(title="Import / Export", icon_name="document-save-symbolic")
        page.set_name("import-export")

        banner_group = Adw.PreferencesGroup()
        banner_group.add(self._create_offline_banner())
        page.add(banner_group)

        backup_group = Adw.PreferencesGroup(
            title="Backup & Restore",
            description="Export all configuration to a JSON file, or import a JSON file to replace all current settings.",
        )

        export_row = Adw.ActionRow(
            title="Export Configuration",
            subtitle="Save all non-secret settings (general, provider, voice, visuals, MCP) to JSON",
        )
        export_btn = Gtk.Button(label="Export JSON…", valign=Gtk.Align.CENTER)
        export_btn.connect("clicked", self._on_export_config_clicked)
        export_row.add_suffix(export_btn)
        backup_group.add(export_row)

        import_row = Adw.ActionRow(
            title="Import Configuration",
            subtitle="Replace all existing settings from a previously exported JSON file",
        )
        import_btn = Gtk.Button(label="Import JSON…", valign=Gtk.Align.CENTER)
        import_btn.add_css_class("destructive-action")
        import_btn.connect("clicked", self._on_import_config_clicked)
        import_row.add_suffix(import_btn)
        backup_group.add(import_row)

        page.add(backup_group)
        self.add(page)

    @staticmethod
    def _build_json_file_filters() -> Gio.ListStore:
        flt = Gtk.FileFilter()
        flt.set_name("JSON files")
        flt.add_mime_type("application/json")
        flt.add_pattern("*.json")
        store = Gio.ListStore.new(Gtk.FileFilter)
        store.append(flt)
        return store

    @staticmethod
    def _export_payload(raw_cfg: dict[str, Any]) -> dict[str, Any]:
        keys = ("general", "provider", "voice_in", "voice_out", "visuals", "mcp_servers")
        return {k: copy.deepcopy(raw_cfg[k]) for k in keys if k in raw_cfg}

    def _on_export_config_clicked(self, *_: object) -> None:
        def _after_fetch(res: ApiResult) -> None:
            if not res.ok or not isinstance(res.data, dict):
                self._toast(res.error or "Failed to fetch configuration")
                return
            payload = json.dumps(self._export_payload(res.data), indent=2) + "\n"
            dlg = Gtk.FileDialog(title="Export Bytey Configuration", initial_name="bytey-config.json")
            dlg.set_filters(self._build_json_file_filters())

            def _on_save_done(dialog: Gtk.FileDialog, result: Gio.AsyncResult) -> None:
                try:
                    gfile = dialog.save_finish(result)
                except GLib.Error:
                    return
                if gfile is None or not gfile.get_path():
                    return
                try:
                    Path(gfile.get_path()).write_text(payload, encoding="utf-8")
                    self._toast(f"Exported configuration to {gfile.get_basename()}")
                except OSError as exc:
                    self._toast(f"Export failed: {exc}")

            dlg.save(self, None, _on_save_done)

        self._run_async(self.client.get_config, _after_fetch)

    def _on_import_config_clicked(self, *_: object) -> None:
        dlg = Gtk.FileDialog(title="Import Bytey Configuration")
        dlg.set_filters(self._build_json_file_filters())

        def _on_open_done(dialog: Gtk.FileDialog, result: Gio.AsyncResult) -> None:
            try:
                gfile = dialog.open_finish(result)
            except GLib.Error:
                return
            if gfile is None or not gfile.get_path():
                return
            try:
                raw_text = Path(gfile.get_path()).read_text(encoding="utf-8")
                parsed = json.loads(raw_text)
            except (OSError, json.JSONDecodeError) as exc:
                self._toast(f"Invalid JSON file: {exc}")
                return
            if not isinstance(parsed, dict):
                self._toast("Invalid JSON: expected a configuration object")
                return
            self._confirm_replace_import(parsed)

        dlg.open(self, None, _on_open_done)

    def _confirm_replace_import(self, parsed: dict[str, Any]) -> None:
        def _apply_replace() -> None:
            def _on_done(res: ApiResult) -> None:
                self._set_offline(res.offline)
                if not res.ok:
                    self._toast(res.error or "Failed to import configuration")
                    return
                self.reload_all()
                self._toast("Configuration imported and replaced")

            self._run_async(lambda: self.client.replace_config(parsed), _on_done)

        if hasattr(Adw, "AlertDialog"):
            dialog = Adw.AlertDialog(
                heading="Replace All Configuration?",
                body="Importing this JSON file will replace all existing Bytey configurations. This cannot be undone.",
            )
            dialog.add_response("cancel", "Cancel")
            dialog.add_response("replace", "Replace All")
            dialog.set_response_appearance("replace", Adw.ResponseAppearance.DESTRUCTIVE)
            dialog.set_default_response("cancel")
            dialog.set_close_response("cancel")
            dialog.connect("response", lambda _d, resp: _apply_replace() if resp == "replace" else None)
            dialog.present(self)
        else:
            dialog = Adw.MessageDialog(
                transient_for=self,
                modal=True,
                heading="Replace All Configuration?",
                body="Importing this JSON file will replace all existing Bytey configurations. This cannot be undone.",
            )
            dialog.add_response("cancel", "Cancel")
            dialog.add_response("replace", "Replace All")
            dialog.set_response_appearance("replace", Adw.ResponseAppearance.DESTRUCTIVE)
            dialog.set_default_response("cancel")
            dialog.connect("response", lambda _d, resp: _apply_replace() if resp == "replace" else None)
            dialog.present()

    # ------------------------------------------------------------------
    # Initial Load & Population
    # ------------------------------------------------------------------

    def reload_all(self) -> None:
        self._run_async(self.client.get_config, self._on_config_loaded)
        self._run_async(self.client.get_mcp_json, self._on_mcp_loaded)

    def _on_config_loaded(self, res: ApiResult) -> None:
        self._set_offline(res.offline)
        if not res.ok:
            self._toast(f"Could not load config: {res.error}")
            return

        payload = res.data if isinstance(res.data, dict) else {}
        if isinstance(payload.get("config"), dict):
            raw_cfg = payload["config"]
            secrets_meta = payload.get("secrets_configured") or payload.get("configured_secrets") or {}
        else:
            raw_cfg = payload
            secrets_meta = (
                payload.get("secrets_configured")
                or payload.get("configured_secrets")
                or payload.get("secrets")
                or {}
            )

        self._use_flat_keys = not any(
            isinstance(raw_cfg.get(section), dict)
            for section in ("general", "llm", "voice_input", "voice_output", "visuals")
        )
        self._config = deep_merge(DEFAULT_CONFIG, raw_cfg)
        if (
            res.offline
            and isinstance(raw_cfg.get("provider"), dict)
            and raw_cfg["provider"].get("context_size") is not None
            and not (isinstance(raw_cfg.get("llm"), dict) and "context_size" in raw_cfg["llm"])
        ):
            self._config.setdefault("llm", {})["context_size"] = raw_cfg["provider"]["context_size"]
        self._secrets_configured = (
            {str(k): bool(v) for k, v in secrets_meta.items()} if isinstance(secrets_meta, dict) else {}
        )
        self._populate_fields()
        if not res.offline:
            self._suppress_piper_refresh_toast = True
            try:
                self._refresh_tts_voices("piper", refresh=False)
            finally:
                self._suppress_piper_refresh_toast = False

    def _on_mcp_loaded(self, res: ApiResult) -> None:
        self._set_offline(res.offline)
        if not res.ok or not isinstance(res.data, dict):
            return
        servers = res.data.get("mcpServers")
        if not isinstance(servers, dict):
            raw_json = res.data.get("raw_json", "")
            try:
                parsed = json.loads(raw_json) if raw_json else {}
                servers = parsed.get("mcpServers", {}) if isinstance(parsed, dict) else {}
            except json.JSONDecodeError:
                servers = {}
        self._loading = True
        self._mcp_servers = copy.deepcopy(servers) if isinstance(servers, dict) else {}
        self._sync_mcp_json_text()
        self._rebuild_mcp_list()
        self._loading = False

    def _populate_fields(self) -> None:
        self._loading = True
        try:
            # General
            self.pet_name_row.set_text(str(self._read_field("pet_name", "Bytey")))
            self.wake_phrase_row.set_text(str(self._read_field("wake_phrase", "Hey Bytey")))
            self.system_prompt_view.get_buffer().set_text(
                str(self._read_field("system_prompt", DEFAULT_SYSTEM_PROMPT))
            )

            # AI & MCP
            provider = str(self._read_field("llm_provider", "ollama"))
            _set_combo_text(self.provider_row, provider)
            self.llm_base_url_row.set_text(str(self._read_field("llm_base_url", "")))
            model_name = str(self._read_field("llm_model", ""))
            self.model_entry_row.set_text(model_name)
            if model_name:
                self.model_combo_row.set_model(Gtk.StringList.new([model_name]))
                self.model_combo_row.set_selected(0)
            self.force_tools_row.set_active(bool(self._read_field("llm_force_tools", False)))
            self._refresh_llm_secret_indicator()
            self._update_tools_sensitivity()
            self._refresh_context_size_row()

            # Voice Input
            self.mic_muted_row.set_active(bool(self._read_field("mic_muted", False)))
            self.wake_sensitivity_scale.set_value(float(self._read_field("wake_sensitivity", 0.72)))
            self.vad_timeout_row.set_value(float(self._read_field("vad_silence_timeout_ms", 1300)))
            _set_combo_text(self.stt_backend_row, str(self._read_field("stt_backend", "local_whisper")))
            self.whisper_model_row.set_text(str(self._read_field("whisper_model", "base")))
            self.stt_api_url_row.set_text(str(self._read_field("stt_api_url", "")))
            _set_secret_row_state(
                self.stt_api_key_row,
                self.stt_api_key_badge,
                bool(self._secrets_configured.get("STT_API_KEY", False)),
            )

            # Voice Output
            self.tts_muted_row.set_active(bool(self._read_field("tts_muted", False)))
            _set_combo_text(self.tts_provider_row, str(self._read_field("tts_provider", "kokoro")))
            piper_model = str(self._read_field("piper_model", "en_US-lessac-medium"))
            self.piper_model_row.set_text(piper_model)
            if self._piper_all_voices:
                self._apply_piper_language_filter()
            elif piper_model:
                self.piper_voice_combo.set_model(Gtk.StringList.new([piper_model]))
                self.piper_voice_combo.set_selected(0)
            else:
                self.piper_voice_combo.set_model(Gtk.StringList.new([""]))
                self.piper_voice_combo.set_selected(0)
            self.piper_models_dir_row.set_text(str(self._read_field("piper_models_dir", "")))
            p_speed = float(self._read_field("piper_speed", 1.0))
            p_pitch = float(self._read_field("piper_pitch", 0.0))
            p_noise = float(self._read_field("piper_noise_scale", 0.667))
            self.piper_speed_scale.set_value(p_speed)
            self.piper_pitch_scale.set_value(p_pitch)
            self.piper_noise_scale.set_value(p_noise)
            self._sync_piper_preset_combo(p_speed, p_pitch, p_noise)
            self._sync_piper_download_button()
            self.kokoro_url_row.set_text(str(self._read_field("kokoro_base_url", "http://localhost:8880/v1")))
            _set_secret_row_state(
                self.kokoro_api_key_row,
                self.kokoro_api_key_badge,
                bool(self._secrets_configured.get("KOKORO_API_KEY", False)),
            )
            k_voice = str(self._read_field("kokoro_voice", "af_heart"))
            self.kokoro_voice_row.set_text(k_voice)
            if k_voice:
                self.kokoro_voice_combo.set_model(Gtk.StringList.new([k_voice]))
                self.kokoro_voice_combo.set_selected(0)
            self.kokoro_speed_scale.set_value(float(self._read_field("kokoro_speed", 1.0)))
            _set_secret_row_state(
                self.el_api_key_row,
                self.el_api_key_badge,
                bool(self._secrets_configured.get("ELEVENLABS_API_KEY", False)),
            )
            self.el_voice_id_row.set_text(str(self._read_field("elevenlabs_voice_id", "")))
            self.el_model_row.set_text(str(self._read_field("elevenlabs_model_id", "eleven_flash_v2_5")))

            # Visuals & Position
            _set_combo_text(self.anim_mode_row, str(self._read_field("animation_mode", "full")))
            self.cursor_tracking_row.set_active(bool(self._read_field("cursor_tracking", True)))
            for key, (row, btn, default_hex) in self._color_controls.items():
                raw_val = self._read_field(key, default_hex)
                rgba = _hex_to_rgba(str(raw_val) if raw_val is not None else default_hex, default_hex)
                btn.set_rgba(rgba)
                row.set_subtitle(_rgba_to_hex(rgba))
            _set_combo_text(self.chat_density_row, str(self._read_field("chat_density", "comfortable")))
            self.chat_width_row.set_value(float(self._read_field("chat_width", 380)))
            self.chat_height_row.set_value(float(self._read_field("chat_height", 420)))
            pos_mode = str(self._read_field("position_mode", "fixed"))
            self._set_position_mode_selection(pos_mode)
            self._sync_position_groups(pos_mode)
            self.monitor_index_row.set_value(float(self._read_field("monitor_index", 0)))
            _set_combo_text(self.fixed_alignment_row, str(self._read_field("fixed_alignment", "top-center")))
            self.offset_x_row.set_value(float(self._read_field("offset_x", 0)))
            self.offset_y_row.set_value(float(self._read_field("offset_y", 12)))
            self.float_x_row.set_value(float(self._read_field("float_x", 100)))
            self.float_y_row.set_value(float(self._read_field("float_y", 80)))
        finally:
            self._loading = False


class SettingsApplication(Adw.Application):
    """Libadwaita application wrapper for Bytey AI Companion Settings."""

    def __init__(self, daemon_url: str = "http://127.0.0.1:8765") -> None:
        super().__init__(application_id="local.bytey_ai_companion.Settings")
        self._daemon_url = daemon_url
        self._window: ByteyPreferencesWindow | None = None

    def do_activate(self) -> None:
        if self._window is None:
            client = DaemonClient(base_url=self._daemon_url)
            self._window = ByteyPreferencesWindow(application=self, client=client)
            self._window.connect("destroy", lambda *_: client.close())
        self._window.present()
