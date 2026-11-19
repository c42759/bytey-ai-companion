"""GTK4 / Libadwaita Settings UI for Bytey AI Companion."""

from __future__ import annotations

import copy
import json
import threading
from pathlib import Path
from typing import Any, Callable

import gi

gi.require_version("Gdk", "4.0")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GLib, Gtk  # noqa: E402

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
TTS_PROVIDERS = ("kokoro", "elevenlabs", "disabled")
ANIMATION_MODES = ("full", "state_only", "disabled")
POSITION_MODES = ("fixed", "floating")
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


def _make_string_combo(title: str, subtitle: str, items: tuple[str, ...] | list[str]) -> Adw.ComboRow:
    row = Adw.ComboRow(title=title)
    if subtitle:
        row.set_subtitle(subtitle)
    row.set_model(Gtk.StringList.new(list(items)))
    return row


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
        self._elevenlabs_voices: list[tuple[str, str]] = []
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
            "elevenlabs_voice_id": self.el_voice_id_row.get_text().strip(),
            "elevenlabs_model_id": self.el_model_row.get_text().strip(),
        }
        if _combo_selected_text(self.provider_row) != "gemini":
            candidates["llm_base_url"] = self.llm_base_url_row.get_text().strip()
        dirty: dict[str, Any] = {}
        for field, val in candidates.items():
            if val and val != str(self._read_field(field, "")):
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
        self._loading = True
        self.llm_base_url_row.set_text(default_url)
        self._refresh_llm_secret_indicator()
        self._loading = False
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
            for item in models_raw:
                if isinstance(item, str):
                    names.append(item)
                elif isinstance(item, dict):
                    mid = str(item.get("id") or item.get("name") or "")
                    if mid:
                        names.append(mid)
                        if "supports_tools" in item:
                            self._model_tool_map[mid] = bool(item["supports_tools"])

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
        self._patch_fields(updates, f"Model: {model_name}")
        self._update_tools_sensitivity()

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

    def _refresh_tts_voices(self, provider: str) -> None:
        base_url = self.kokoro_url_row.get_text().strip() if provider == "kokoro" else ""
        pending_secret_key = ""
        pending_secret_val = ""
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
        self._toast(f"Fetching {provider} voices...")

        def fetch_task() -> ApiResult:
            if pending_secret_key and pending_secret_val:
                sec_res = self.client.put_secrets({pending_secret_key: pending_secret_val})
                if sec_res.ok:
                    self._secrets_configured[pending_secret_key] = True
            return self.client.list_tts_voices(provider, base_url)

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
                self._toast(f"Voice fetch failed: {res.error}")
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

        pos_group = Adw.PreferencesGroup(title="Island Placement")
        self.position_mode_row = _make_string_combo("Position Mode", "", POSITION_MODES)
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

    def _on_position_mode_changed(self, *_: object) -> None:
        mode = _combo_selected_text(self.position_mode_row)
        self._sync_position_groups(mode)
        if not self._loading:
            self._patch_fields({"position_mode": mode}, f"Position mode: {mode}")

    def _sync_position_groups(self, mode: str) -> None:
        is_fixed = mode == "fixed"
        self.fixed_group.set_sensitive(is_fixed)
        self.floating_group.set_sensitive(not is_fixed)

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
        self._secrets_configured = (
            {str(k): bool(v) for k, v in secrets_meta.items()} if isinstance(secrets_meta, dict) else {}
        )
        self._populate_fields()

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
            _set_combo_text(self.position_mode_row, pos_mode)
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
