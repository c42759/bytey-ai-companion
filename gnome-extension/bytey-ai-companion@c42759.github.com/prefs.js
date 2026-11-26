// SPDX-License-Identifier: GPL-3.0-or-later

import Adw from 'gi://Adw';
import Gdk from 'gi://Gdk';
import GLib from 'gi://GLib';
import Gio from 'gi://Gio';
import Gtk from 'gi://Gtk';
import Soup from 'gi://Soup?version=3.0';
import {ExtensionPreferences} from 'resource:///org/gnome/Shell/Extensions/js/extensions/prefs.js';

const DEFAULT_DAEMON_URL = 'http://127.0.0.1:8765';
const DEFAULT_EYE_COLOR = '#F7FAFF';
const DEFAULT_BG_COLOR = '#0A0A0C';
const DEFAULT_BORDER_COLOR = '#2A2C34';
const ANIMATION_MODES = ['full', 'state_only', 'disabled'];
const CHAT_DENSITIES = ['compact', 'comfortable', 'spacious'];
const POSITION_MODE_OPTIONS = [
    {label: 'Fixed', value: 'fixed'},
    {label: 'Floating', value: 'floating'},
    {label: 'Notch', value: 'notch'},
];
const FIXED_ALIGNMENTS = [
    'top-left',
    'top-center',
    'top-right',
    'center-left',
    'center',
    'center-right',
    'bottom-left',
    'bottom-center',
    'bottom-right',
];
const STT_BACKENDS = ['local_whisper', 'openai_whisper', 'custom_whisper'];
const TTS_PROVIDERS = ['kokoro', 'piper', 'elevenlabs', 'disabled'];
const DEFAULT_PIPER_MODEL = 'en_US-lessac-medium';
const DEFAULT_ELEVENLABS_VOICES = [
    {id: 'JBFqnCBsd6RMkjVDRZzb', name: 'George (premade)'},
    {id: '21m00Tcm4TlvDq8ikWAM', name: 'Rachel (premade)'},
    {id: 'pNInz6obpgDQGcFmaJgB', name: 'Adam (premade)'},
];
const PIPER_VOICE_PRESETS = [
    {id: 'normal', label: 'Normal', speed: 1.0, pitch: 0.0, noise_scale: 0.667},
    {id: 'chipmunk', label: 'Chipmunk', speed: 1.25, pitch: 8.0, noise_scale: 0.8},
    {id: 'helium', label: 'Helium Squeak', speed: 1.15, pitch: 10.5, noise_scale: 0.9},
    {id: 'deep_monster', label: 'Deep Monster', speed: 0.82, pitch: -8.0, noise_scale: 0.667},
    {id: 'sleepy_giant', label: 'Sleepy Giant', speed: 0.68, pitch: -5.5, noise_scale: 0.55},
    {id: 'hyper_gremlin', label: 'Hyper Gremlin', speed: 1.55, pitch: 5.5, noise_scale: 1.0},
    {id: 'flat_robot', label: 'Flat Robot', speed: 1.05, pitch: -2.5, noise_scale: 0.1},
    {id: 'custom', label: 'Custom', speed: null, pitch: null, noise_scale: null},
];
const AI_PROVIDERS = ['ollama', 'gemini', 'openai', 'anthropic', 'custom'];
const TEST_ANIMATION_STATES = [
    'IDLE',
    'LISTENING',
    'THINKING',
    'TOOL_CALLING',
    'SPEAKING',
    'MUTED',
    'ERROR',
];
const TEST_ANIMATION_EMOTIONS = [
    'neutral',
    'happy',
    'love',
    'annoyed',
    'sleepy',
    'focus',
    'astonished',
    'curious',
    'surprised',
    'error',
];
const MCP_TRANSPORTS = ['stdio', 'http', 'sse'];
const SECRET_MASK_PLACEHOLDER = '••••••••••••••••';
const PROVIDER_SECRET_MAP = {
    gemini: 'GEMINI_API_KEY',
    openai: 'OPENAI_API_KEY',
    anthropic: 'ANTHROPIC_API_KEY',
    custom: 'CUSTOM_LLM_API_KEY',
};
const PROVIDER_DEFAULT_URLS = {
    ollama: 'http://localhost:11434',
    gemini: '',
    openai: 'https://api.openai.com/v1',
    anthropic: 'https://api.anthropic.com',
    custom: 'http://localhost:8000/v1',
};
const DEFAULT_CONTEXT_FALLBACK_MAX = 131072;

function normalizeUrl(raw) {
    if (typeof raw !== 'string')
        return null;
    const trimmed = raw.trim();
    if (!/^https?:\/\/\S+$/i.test(trimmed))
        return null;
    const stripped = trimmed.replace(/\/+$/, '');
    return /^https?:\/\/\S+$/i.test(stripped) ? stripped : null;
}

function makeSearchableComboRow(props) {
    return new Adw.ComboRow({
        enable_search: true,
        expression: Gtk.PropertyExpression.new(Gtk.StringObject, null, 'string'),
        ...props,
    });
}

function makeComboRow(title, items) {
    return makeSearchableComboRow({
        title,
        model: Gtk.StringList.new(items),
    });
}

function comboText(row) {
    const item = row.get_selected_item();
    return item ? item.get_string() : '';
}

function setComboText(row, value) {
    const model = row.get_model();
    if (!model)
        return;
    const target = String(value ?? '');
    const count = model.get_n_items();
    for (let i = 0; i < count; i++) {
        if (model.get_string(i) === target) {
            row.set_selected(i);
            return;
        }
    }
}

function positionModeValueForIndex(idx) {
    return (POSITION_MODE_OPTIONS[idx] ?? POSITION_MODE_OPTIONS[0]).value;
}

function positionModeIndexForValue(mode) {
    const normalized = String(mode ?? 'fixed').toLowerCase().trim();
    const idx = POSITION_MODE_OPTIONS.findIndex(o => o.value === normalized);
    return idx >= 0 ? idx : 0;
}

function makeSpinRow(title, min, max, step) {
    return new Adw.SpinRow({
        title,
        digits: 0,
        adjustment: new Gtk.Adjustment({
            lower: min,
            upper: max,
            step_increment: step,
            page_increment: step * 10,
            value: min,
        }),
    });
}

function makeFloatSpinRow(title, subtitle, min, max, step, digits) {
    return new Adw.SpinRow({
        title,
        subtitle,
        digits,
        adjustment: new Gtk.Adjustment({
            lower: min,
            upper: max,
            step_increment: step,
            page_increment: step * 10,
            value: min,
        }),
    });
}

function matchPiperPresetIndex(speed, pitch, noiseScale) {
    const s = Number(speed);
    const p = Number(pitch);
    const n = Number(noiseScale);
    const customIdx = PIPER_VOICE_PRESETS.length - 1;
    if (!Number.isFinite(s) || !Number.isFinite(p) || !Number.isFinite(n))
        return customIdx;
    for (let i = 0; i < PIPER_VOICE_PRESETS.length; i++) {
        const preset = PIPER_VOICE_PRESETS[i];
        if (preset.speed === null || preset.pitch === null || preset.noise_scale === null)
            continue;
        if (
            Math.abs(s - preset.speed) < 0.015 &&
            Math.abs(p - preset.pitch) < 0.1 &&
            Math.abs(n - preset.noise_scale) < 0.015
        )
            return i;
    }
    return customIdx;
}

function sampleTextForLanguage(langCode) {
    const raw = String(langCode ?? '').trim().toLowerCase().replace('-', '_');
    const prefix = raw.split('_')[0];
    const samples = {
        en: 'Hello! I am Bytey, your desktop AI companion.',
        es: '¡Hola! Soy Bytey, tu asistente de escritorio.',
        fr: 'Bonjour ! Je suis Bytey, votre assistant vocal.',
        de: 'Hallo! Ich bin Bytey, dein digitaler Begleiter.',
        it: 'Ciao! Sono Bytey, il tuo assistente sul desktop.',
        pt: 'Olá! Eu sou o Bytey, seu assistente de voz.',
        nl: 'Hallo! Ik ben Bytey, jouw desktop-assistent.',
        pl: 'Cześć! Jestem Bytey, twój asystent głosowy.',
        ru: 'Привет! Я Байти, твой голосовой помощник.',
        uk: 'Привіт! Я Байті, твій голосовий помічник.',
        zh: '你好！我是 Bytey，你的桌面语音助手。',
        ja: 'こんにちは！私はデスクトップアシスタントのバイティです。',
        ko: '안녕하세요! 저는 데스크톱 AI 비서 바이티입니다.',
        ar: 'مرحباً! أنا بايتي، مساعدك الصوتي.',
        hi: 'नमस्ते! मैं बायटी हूँ, आपका डे스크टॉप सहायक।',
        tr: 'Merhaba! Ben Bytey, masaüstü sesli asistanınım.',
        sv: 'Hej! Jag är Bytey, din röstassistent.',
        da: 'Hej! Jeg er Bytey, din skrivebordsassistent.',
        no: 'Hei! Jeg er Bytey, din skrivebordsassistent.',
        nb: 'Hei! Jeg er Bytey, din skrivebordsassistent.',
        fi: 'Hei! Olen Bytey, työpöytäavustajasi.',
        cs: 'Ahoj! Jsem Bytey, tvůj hlasový asistent.',
        el: 'Γεια σου! Είμαι ο Bytey, ο βοηθός σου.',
        ro: 'Salut! Sunt Bytey, asistentul tău vocal.',
        hu: 'Szia! Bytey vagyok, az asztali hangasszisztensed.',
        ca: 'Hola! Soc Bytey, el teu assistent d’escriptori.',
        vi: 'Xin chào! Tôi là Bytey, trợ lý giọng nói của bạn.',
    };
    return samples[prefix] ?? samples.en;
}

function inferPiperLanguageCode(modelId) {
    const match = String(modelId ?? '').trim().match(/^([a-z]{2,3}(?:[_-][A-Za-z0-9]+)?)-/);
    return match ? match[1] : '';
}

function formatPiperLanguageLabel(code, rawName = '') {
    const cleanCode = String(code ?? '').trim();
    const cleanName = String(rawName ?? '').trim();
    if (cleanName && cleanName !== cleanCode)
        return cleanName.includes(cleanCode) ? cleanName : `${cleanName} (${cleanCode})`;
    if (!cleanCode)
        return 'Unknown';
    try {
        const display = new Intl.DisplayNames(['en'], {type: 'language'});
        const resolved = display.of(cleanCode.replace('_', '-'));
        if (resolved && resolved !== cleanCode)
            return `${resolved} (${cleanCode})`;
    } catch (_e) {
        // Fallback to raw code when DisplayNames rejects non-standard tags.
    }
    return cleanCode;
}

function normalizePiperVoiceEntry(entry) {
    if (typeof entry === 'string') {
        const id = entry.trim();
        if (!id)
            return null;
        return {
            id,
            languageCode: inferPiperLanguageCode(id),
            languageName: '',
            quality: '',
            installed: false,
        };
    }
    if (!entry || typeof entry !== 'object')
        return null;
    const id = String(entry.id ?? entry.key ?? entry.model ?? entry.name ?? '').trim();
    if (!id)
        return null;
    let languageCode = '';
    let languageName = '';
    if (entry.language && typeof entry.language === 'object') {
        languageCode = String(entry.language.code ?? entry.language_code ?? entry.lang ?? '').trim();
        const eng = String(entry.language_name ?? entry.language.name_english ?? entry.language.name_native ?? '').trim();
        const country = String(entry.language.country_english ?? '').trim();
        languageName = eng && country ? `${eng} (${country})` : eng;
    } else {
        languageCode = String(entry.language_code ?? entry.language ?? entry.lang ?? '').trim();
        languageName = String(entry.language_name ?? entry.lang_name ?? '').trim();
    }
    if (!languageCode)
        languageCode = inferPiperLanguageCode(id);
    return {
        id,
        languageCode,
        languageName,
        quality: String(entry.quality ?? '').trim(),
        installed: Boolean(entry.installed ?? entry.downloaded ?? entry.local ?? false),
    };
}

function parseHexChannels(hexStr) {
    if (typeof hexStr !== 'string')
        return null;
    const cleaned = hexStr.trim();
    if (!cleaned.startsWith('#'))
        return null;
    let raw = cleaned.slice(1);
    if (!/^[0-9a-fA-F]+$/.test(raw))
        return null;
    if (raw.length === 3)
        raw = `${raw[0]}${raw[0]}${raw[1]}${raw[1]}${raw[2]}${raw[2]}FF`;
    else if (raw.length === 6)
        raw = `${raw}FF`;
    else if (raw.length !== 8)
        return null;
    return [
        parseInt(raw.slice(0, 2), 16) / 255.0,
        parseInt(raw.slice(2, 4), 16) / 255.0,
        parseInt(raw.slice(4, 6), 16) / 255.0,
        parseInt(raw.slice(6, 8), 16) / 255.0,
    ];
}

function hexToRgba(hexStr, fallback = '#FFFFFFFF') {
    const channels = parseHexChannels(hexStr) ?? parseHexChannels(fallback) ?? [1.0, 1.0, 1.0, 1.0];
    const [red, green, blue, alpha] = channels;
    return new Gdk.RGBA({red, green, blue, alpha});
}

function rgbaToHex(rgba) {
    const toByte = val => Math.max(0, Math.min(255, Math.round(val * 255)));
    const hexByte = n => n.toString(16).padStart(2, '0').toUpperCase();
    const r = toByte(rgba.red);
    const g = toByte(rgba.green);
    const b = toByte(rgba.blue);
    const a = toByte(rgba.alpha);
    if (a >= 255)
        return `#${hexByte(r)}${hexByte(g)}${hexByte(b)}`;
    return `#${hexByte(r)}${hexByte(g)}${hexByte(b)}${hexByte(a)}`;
}

function isMaskedPlaceholder(value) {
    const cleaned = String(value ?? '').trim();
    return cleaned.length > 0 && (cleaned === SECRET_MASK_PLACEHOLDER || /^[•]+$/.test(cleaned));
}

function makeSecretRow(title) {
    const row = new Adw.PasswordEntryRow({
        title,
        show_apply_button: true,
    });
    const badge = new Gtk.Label({
        label: 'Not set',
        valign: Gtk.Align.CENTER,
        css_classes: ['dim-label', 'caption'],
    });
    row.add_suffix(badge);
    return {row, badge};
}

function setSecretRowState(row, badge, configured) {
    if (configured) {
        badge.set_label('Configured');
        badge.remove_css_class('dim-label');
        badge.add_css_class('success');
        row.set_text(SECRET_MASK_PLACEHOLDER);
    } else {
        badge.set_label('Not set');
        badge.remove_css_class('success');
        badge.add_css_class('dim-label');
        row.set_text('');
    }
}

function readTextBuffer(view) {
    const buf = view.get_buffer();
    const [start, end] = buf.get_bounds();
    return buf.get_text(start, end, true).trim();
}

function makeTextViewBox(labelText, view, minHeight = 96) {
    const box = new Gtk.Box({
        orientation: Gtk.Orientation.VERTICAL,
        spacing: 6,
        margin_top: 6,
        margin_bottom: 6,
    });
    const lbl = new Gtk.Label({
        label: labelText,
        xalign: 0,
        css_classes: ['caption', 'dim-label'],
    });
    const scroll = new Gtk.ScrolledWindow({min_content_height: minHeight});
    scroll.set_child(view);
    const frame = new Gtk.Frame();
    frame.set_child(scroll);
    box.append(lbl);
    box.append(frame);
    return box;
}

function cloneJson(obj) {
    return JSON.parse(JSON.stringify(obj ?? {}));
}

function formatNumberCommas(n) {
    return String(n).replace(/\B(?=(\d{3})+(?!\d))/g, ',');
}

function formatContextTokens(tokens) {
    if (tokens >= 1048576 && tokens % 1048576 === 0)
        return `${tokens / 1048576}M (${formatNumberCommas(tokens)})`;
    if (tokens >= 1024 && tokens % 1024 === 0)
        return `${tokens / 1024}K (${formatNumberCommas(tokens)})`;
    return formatNumberCommas(tokens);
}

function formatContextShort(tokens) {
    if (tokens >= 1048576 && tokens % 1048576 === 0)
        return `${tokens / 1048576}M`;
    if (tokens >= 1024 && tokens % 1024 === 0)
        return `${tokens / 1024}K`;
    return formatNumberCommas(tokens);
}

function normalizePositiveInt(value) {
    if (typeof value === 'number')
        return Number.isInteger(value) && value > 0 ? value : null;
    if (typeof value === 'string') {
        const trimmed = value.trim();
        if (!/^\d+$/.test(trimmed))
            return null;
        const parsed = Number(trimmed);
        return Number.isInteger(parsed) && parsed > 0 ? parsed : null;
    }
    return null;
}

function buildContextPresets(maxContext, currentValue = null) {
    const maxCtx = normalizePositiveInt(maxContext);
    const curVal = normalizePositiveInt(currentValue);
    const values = [];

    if (maxCtx !== null && maxCtx < 4096) {
        for (let p = 2048; p >= 512; p /= 2) {
            if (p <= maxCtx) {
                values.push(p);
                break;
            }
        }
        if (values.length === 0)
            values.push(maxCtx);
    } else {
        const limit = maxCtx ?? DEFAULT_CONTEXT_FALLBACK_MAX;
        for (let v = 4096; v <= limit && v <= 16777216; v *= 2)
            values.push(v);
    }

    if (curVal !== null && (maxCtx === null || curVal <= maxCtx) && !values.includes(curVal)) {
        values.push(curVal);
        values.sort((a, b) => a - b);
    }

    return [
        {label: 'Auto (Default)', value: null},
        ...values.map(v => ({label: formatContextTokens(v), value: v})),
    ];
}

function clampContextValue(value, presets) {
    const cur = normalizePositiveInt(value);
    if (cur === null)
        return null;
    if (presets.some(p => p.value === cur))
        return cur;
    const numeric = presets.map(p => p.value).filter(v => v !== null);
    if (numeric.length === 0)
        return null;
    let best = null;
    for (const v of numeric) {
        if (v <= cur && (best === null || v > best))
            best = v;
    }
    return best !== null ? best : numeric[numeric.length - 1];
}

/**
 * Libadwaita preferences window for Bytey AI Companion.
 */
export default class ByteyPreferences extends ExtensionPreferences {
    fillPreferencesWindow(window) {
        window.set_default_size(680, 620);
        window.set_search_enabled(true);

        const settings = this.getSettings();
        const cancellable = new Gio.Cancellable();
        const httpSession = new Soup.Session({timeout: 8});
        const longHttpSession = new Soup.Session({timeout: 300});
        let destroyed = false;
        let loading = false;
        let settingsChangedId = 0;
        let secretsConfigured = {};
        let availableModels = [];
        let currentConfiguredModel = '';
        let modelFetchSeq = 0;
        let piperVoices = [];
        let piperFilteredVoices = [];
        let piperLanguageOptions = [{code: '', label: 'All Languages'}];
        let currentPiperLanguage = '';
        let currentPiperModel = DEFAULT_PIPER_MODEL;
        let piperFetchSeq = 0;
        let piperDownloading = false;
        let detectedToolsSupport = false;
        const modelToolMap = new Map();
        let currentContextSize = null;
        let detectedMaxContext = null;
        let contextPresetOptions = buildContextPresets(null, null);
        const modelContextMap = new Map();
        let mcpServers = {};
        let mcpRows = [];
        let skillsList = [];
        let skillRows = [];
        let configuredSkillsDir = '';

        const getDaemonUrl = () => normalizeUrl(settings.get_string('daemon-url')) ?? DEFAULT_DAEMON_URL;

        const showToast = title => {
            if (!destroyed)
                window.add_toast(new Adw.Toast({title, timeout: 3}));
        };

        const httpJson = (method, path, body = undefined, {longTimeout = false} = {}) => new Promise(resolve => {
            if (destroyed) {
                resolve({ok: false, status: 0, data: null});
                return;
            }

            const url = `${getDaemonUrl()}${path}`;
            const msg = Soup.Message.new(method, url);
            if (!msg) {
                resolve({ok: false, status: 0, data: null});
                return;
            }

            if (body !== undefined) {
                const payload = new GLib.Bytes(new TextEncoder().encode(JSON.stringify(body)));
                msg.set_request_body_from_bytes('application/json', payload);
            }

            const sessionToUse = longTimeout ? longHttpSession : httpSession;
            sessionToUse.send_and_read_async(
                msg,
                GLib.PRIORITY_DEFAULT,
                cancellable,
                (session, res) => {
                    if (destroyed) {
                        resolve({ok: false, status: 0, data: null});
                        return;
                    }
                    try {
                        const bytes = session.send_and_read_finish(res);
                        const status = msg.get_status();
                        const arr = bytes ? bytes.toArray() : new Uint8Array(0);
                        const rawText = arr.length > 0 ? new TextDecoder().decode(arr) : '';
                        let data = null;
                        if (rawText) {
                            try {
                                data = JSON.parse(rawText);
                            } catch (_parseErr) {
                                data = rawText;
                            }
                        }
                        resolve({
                            ok: status >= 200 && status < 300,
                            status,
                            data,
                        });
                    } catch (_err) {
                        resolve({ok: false, status: 0, data: null});
                    }
                }
            );
        });

        window.connect('close-request', () => {
            destroyed = true;
            if (settingsChangedId) {
                settings.disconnect(settingsChangedId);
                settingsChangedId = 0;
            }
            cancellable.cancel();
            try {
                httpSession.abort();
            } catch (_e) {
                // Ignore abort errors on close.
            }
            try {
                longHttpSession.abort();
            } catch (_e) {
                // Ignore abort errors on close.
            }
            return false;
        });

        // 1. General Page
        const generalPage = new Adw.PreferencesPage({
            title: 'General',
            icon_name: 'preferences-system-symbolic',
        });

        const offlineBanner = new Adw.Banner({
            title: 'Bytey daemon offline — start core daemon to edit live settings',
            button_label: 'Retry',
            revealed: false,
        });
        if (typeof generalPage.set_banner === 'function') {
            generalPage.set_banner(offlineBanner);
        } else {
            const bannerGroup = new Adw.PreferencesGroup();
            bannerGroup.add(offlineBanner);
            generalPage.add(bannerGroup);
        }

        const brandGroup = new Adw.PreferencesGroup();
        const versionLabel = this.metadata?.['version-name'] ?? '0.1.0';
        const brandRow = new Adw.ActionRow({
            title: 'Bytey AI Companion',
            subtitle: `Dynamic Island desktop AI assistant · v${versionLabel}`,
        });
        const iconFile = this.dir.get_child('icons').get_child('bytey-ai-companion.svg');
        if (iconFile.query_exists(null)) {
            const brandIcon = new Gtk.Image({
                gicon: new Gio.FileIcon({file: iconFile}),
                pixel_size: 48,
                valign: Gtk.Align.CENTER,
            });
            brandRow.add_prefix(brandIcon);
        }
        brandGroup.add(brandRow);
        generalPage.add(brandGroup);

        const connectionGroup = new Adw.PreferencesGroup({title: 'Connection'});
        const daemonUrlRow = new Adw.EntryRow({
            title: 'Daemon URL',
            show_apply_button: true,
        });
        daemonUrlRow.set_text(getDaemonUrl());
        connectionGroup.add(daemonUrlRow);

        const statusRow = new Adw.ActionRow({
            title: 'Daemon Status',
            subtitle: 'Checking...',
        });
        const testBtn = new Gtk.Button({
            label: 'Test Connection',
            valign: Gtk.Align.CENTER,
        });
        statusRow.add_suffix(testBtn);
        connectionGroup.add(statusRow);
        generalPage.add(connectionGroup);

        const identityGroup = new Adw.PreferencesGroup({title: 'Companion Identity'});
        const petNameRow = new Adw.EntryRow({
            title: 'Companion Name',
            show_apply_button: true,
        });
        const wakePhraseRow = new Adw.EntryRow({
            title: 'Custom Wake Phrase',
            show_apply_button: true,
        });
        identityGroup.add(petNameRow);
        identityGroup.add(wakePhraseRow);

        const promptTitleLabel = new Gtk.Label({
            label: 'Companion System Prompt',
            xalign: 0,
            margin_top: 12,
            margin_bottom: 6,
            css_classes: ['heading'],
        });
        identityGroup.add(promptTitleLabel);

        const systemPromptView = new Gtk.TextView({
            wrap_mode: Gtk.WrapMode.WORD_CHAR,
            top_margin: 8,
            bottom_margin: 8,
            left_margin: 10,
            right_margin: 10,
        });
        const systemPromptScroll = new Gtk.ScrolledWindow({min_content_height: 150});
        systemPromptScroll.set_child(systemPromptView);
        const systemPromptFrame = new Gtk.Frame();
        systemPromptFrame.set_child(systemPromptScroll);
        identityGroup.add(systemPromptFrame);

        const promptBtnBox = new Gtk.Box({
            orientation: Gtk.Orientation.HORIZONTAL,
            spacing: 8,
            halign: Gtk.Align.END,
            margin_top: 8,
        });
        const resetSystemPromptBtn = new Gtk.Button({label: 'Reset Default'});
        const saveSystemPromptBtn = new Gtk.Button({
            label: 'Save Prompt',
            css_classes: ['suggested-action'],
        });
        promptBtnBox.append(resetSystemPromptBtn);
        promptBtnBox.append(saveSystemPromptBtn);
        identityGroup.add(promptBtnBox);

        generalPage.add(identityGroup);

        // 2. Visuals & Position Page
        const visualsPage = new Adw.PreferencesPage({
            title: 'Visuals & Position',
            icon_name: 'preferences-desktop-display-symbolic',
        });

        const placementGroup = new Adw.PreferencesGroup({title: 'Island Placement'});
        const posModeRow = makeComboRow('Mode', POSITION_MODE_OPTIONS.map(o => o.label));
        const monitorRow = makeSpinRow('Monitor Index', 0, 15, 1);
        const fixedAlignRow = makeComboRow('Fixed Alignment', FIXED_ALIGNMENTS);
        const offsetXRow = makeSpinRow('Offset X', -4000, 4000, 4);
        const offsetYRow = makeSpinRow('Offset Y', -4000, 4000, 4);
        const floatXRow = makeSpinRow('Floating X', 0, 10000, 10);
        const floatYRow = makeSpinRow('Floating Y', 0, 10000, 10);
        placementGroup.add(posModeRow);
        placementGroup.add(monitorRow);
        placementGroup.add(fixedAlignRow);
        placementGroup.add(offsetXRow);
        placementGroup.add(offsetYRow);
        placementGroup.add(floatXRow);
        placementGroup.add(floatYRow);
        visualsPage.add(placementGroup);

        const eyesGroup = new Adw.PreferencesGroup({title: 'Eye Animations'});
        const animModeRow = makeComboRow('Animation Mode', ANIMATION_MODES);
        const cursorTrackRow = new Adw.SwitchRow({
            title: 'Cursor Tracking',
            active: true,
        });
        eyesGroup.add(animModeRow);
        eyesGroup.add(cursorTrackRow);
        visualsPage.add(eyesGroup);

        const colorControls = {};
        const makeColorRow = (title, fieldKey, defaultHex) => {
            const row = new Adw.ActionRow({
                title,
                subtitle: defaultHex,
            });
            const btn = new Gtk.ColorDialogButton({
                dialog: new Gtk.ColorDialog({with_alpha: true}),
                valign: Gtk.Align.CENTER,
            });
            btn.set_rgba(hexToRgba(defaultHex, defaultHex));
            btn.connect('notify::rgba', () => onUserChange(() => {
                const hexVal = rgbaToHex(btn.get_rgba());
                row.set_subtitle(hexVal);
                patchConfig({visuals: {[fieldKey]: hexVal}}, `Saved ${title.toLowerCase()}`);
            }));
            row.add_suffix(btn);
            colorControls[fieldKey] = {row, btn, defaultHex};
            return row;
        };

        const colorsGroup = new Adw.PreferencesGroup({
            title: 'Colors',
            description: 'Customize eye, island background, and border colors.',
        });
        const resetColorsBtn = new Gtk.Button({
            label: 'Reset Defaults',
            valign: Gtk.Align.CENTER,
            css_classes: ['flat'],
        });
        colorsGroup.set_header_suffix(resetColorsBtn);
        colorsGroup.add(makeColorRow('Eye Color', 'eye_color', DEFAULT_EYE_COLOR));
        colorsGroup.add(makeColorRow('Background Color', 'background_color', DEFAULT_BG_COLOR));
        colorsGroup.add(makeColorRow('Border Color', 'border_color', DEFAULT_BORDER_COLOR));
        visualsPage.add(colorsGroup);

        const drawerGroup = new Adw.PreferencesGroup({title: 'Chat Drawer'});
        const densityRow = makeComboRow('Chat Density', CHAT_DENSITIES);
        const islandWidthRow = makeSpinRow('Island Width (px)', 140, 450, 4);
        drawerGroup.add(islandWidthRow);

        const chatWidthRow = makeSpinRow('Chat Width (px)', 260, 3000, 10);
        const chatHeightRow = makeSpinRow('Chat Height (px)', 220, 900, 10);
        const chatOpacityRow = makeSpinRow('Chat Opacity (%)', 10, 100, 5);
        drawerGroup.add(densityRow);
        drawerGroup.add(chatWidthRow);
        drawerGroup.add(chatHeightRow);
        drawerGroup.add(chatOpacityRow);
        visualsPage.add(drawerGroup);

        const testAnimGroup = new Adw.PreferencesGroup({
            title: 'Test Animations',
            description: 'Preview eye emotions and island states live on the companion.',
        });
        const testStateRow = makeComboRow('Animation State', TEST_ANIMATION_STATES);
        const testEmotionRow = makeComboRow('Emotion', TEST_ANIMATION_EMOTIONS);
        const testAnimActionRow = new Adw.ActionRow({
            title: 'Live Preview',
            subtitle: 'Trigger the selected state and emotion on the island.',
        });
        const testAnimBtnBox = new Gtk.Box({
            orientation: Gtk.Orientation.HORIZONTAL,
            spacing: 8,
            valign: Gtk.Align.CENTER,
        });
        const resetAnimBtn = new Gtk.Button({
            label: 'Reset',
            valign: Gtk.Align.CENTER,
        });
        const previewAnimBtn = new Gtk.Button({
            label: 'Preview',
            valign: Gtk.Align.CENTER,
            css_classes: ['suggested-action'],
        });
        testAnimBtnBox.append(resetAnimBtn);
        testAnimBtnBox.append(previewAnimBtn);
        testAnimActionRow.add_suffix(testAnimBtnBox);
        testAnimGroup.add(testStateRow);
        testAnimGroup.add(testEmotionRow);
        testAnimGroup.add(testAnimActionRow);
        visualsPage.add(testAnimGroup);

        // 3. Voice Page
        const voicePage = new Adw.PreferencesPage({
            title: 'Voice',
            icon_name: 'audio-input-microphone-symbolic',
        });

        const sttGroup = new Adw.PreferencesGroup({
            title: 'STT Controls',
            description: 'Speech-to-text microphone and transcription backend settings.',
        });
        const micMuteRow = new Adw.SwitchRow({title: 'Mute Microphone'});
        const sttBackendRow = makeComboRow('STT Backend', STT_BACKENDS);
        const sttApiUrlRow = new Adw.EntryRow({
            title: 'STT API URL',
            show_apply_button: true,
        });
        const {row: sttApiKeyRow, badge: sttApiKeyBadge} = makeSecretRow('API Key');
        sttGroup.add(micMuteRow);
        sttGroup.add(sttBackendRow);
        sttGroup.add(sttApiUrlRow);
        sttGroup.add(sttApiKeyRow);
        voicePage.add(sttGroup);

        const ttsGroup = new Adw.PreferencesGroup({
            title: 'TTS Controls',
            description: 'Text-to-speech playback and provider settings.',
        });
        const ttsMuteRow = new Adw.SwitchRow({title: 'Mute Voice (TTS)'});
        const ttsProviderRow = makeComboRow('TTS Provider', TTS_PROVIDERS);
        const kokoroBaseUrlRow = new Adw.EntryRow({
            title: 'Kokoro Server URL',
            show_apply_button: true,
        });
        const {row: kokoroApiKeyRow, badge: kokoroApiKeyBadge} = makeSecretRow('Kokoro API Key');
        const {row: elevenlabsApiKeyRow, badge: elevenlabsApiKeyBadge} = makeSecretRow('ElevenLabs API Key');
        const elevenlabsVoiceRow = makeSearchableComboRow({
            title: 'ElevenLabs Voice',
            subtitle: 'Select a voice or refresh from your ElevenLabs account',
            model: Gtk.StringList.new(DEFAULT_ELEVENLABS_VOICES.map(v => `${v.name} — ${v.id}`)),
        });
        const elevenlabsRefreshBtn = new Gtk.Button({
            icon_name: 'view-refresh-symbolic',
            valign: Gtk.Align.CENTER,
            tooltip_text: 'Refresh ElevenLabs voices',
            css_classes: ['flat'],
        });
        elevenlabsVoiceRow.add_suffix(elevenlabsRefreshBtn);
        const elevenlabsVoiceIdRow = new Adw.EntryRow({
            title: 'ElevenLabs Voice ID',
            show_apply_button: true,
        });
        const elevenlabsModelRow = new Adw.EntryRow({
            title: 'ElevenLabs Model ID',
            show_apply_button: true,
        });
        elevenlabsModelRow.set_text('eleven_flash_v2_5');

        const piperLangRow = makeSearchableComboRow({
            title: 'Piper Language',
            subtitle: 'Filter voice models by language',
            model: Gtk.StringList.new(['All Languages']),
        });
        const piperModelRow = makeSearchableComboRow({
            title: 'Piper Voice Model',
            subtitle: 'Local CPU neural voice model',
            model: Gtk.StringList.new(['en_US-lessac-medium']),
        });
        const piperRefreshBtn = new Gtk.Button({
            icon_name: 'view-refresh-symbolic',
            valign: Gtk.Align.CENTER,
            tooltip_text: 'Refresh Piper voice catalog',
            css_classes: ['flat'],
        });
        const piperDownloadBtn = new Gtk.Button({
            label: 'Download',
            valign: Gtk.Align.CENTER,
        });
        piperModelRow.add_suffix(piperRefreshBtn);
        piperModelRow.add_suffix(piperDownloadBtn);

        const piperCustomModelRow = new Adw.EntryRow({
            title: 'Active Model ID / .onnx Path',
            show_apply_button: true,
        });
        piperCustomModelRow.set_text(DEFAULT_PIPER_MODEL);

        const piperPresetRow = makeSearchableComboRow({
            title: 'Voice Style (Funny Presets)',
            subtitle: 'Quick presets for normal or funny voices',
            model: Gtk.StringList.new(PIPER_VOICE_PRESETS.map(p => p.label)),
        });
        const piperSpeedRow = makeFloatSpinRow(
            'Piper Speed',
            'Speech rate (0.40x slow – 2.50x fast)',
            0.4,
            2.5,
            0.05,
            2
        );
        piperSpeedRow.set_value(1.0);
        const piperPitchRow = makeFloatSpinRow(
            'Piper Pitch (Semitones)',
            'Voice pitch (-12 deep monster, 0 normal, +12 chipmunk)',
            -12.0,
            12.0,
            0.5,
            1
        );
        piperPitchRow.set_value(0.0);
        const piperNoiseRow = makeFloatSpinRow(
            'Piper Expressiveness',
            'Tone variation (0.10 robotic – 0.67 natural – 1.20 animated)',
            0.0,
            1.5,
            0.05,
            2
        );
        piperNoiseRow.set_value(0.667);

        const piperTestRow = new Adw.ActionRow({
            title: 'Preview Piper Voice',
            subtitle: 'Speak a short sample with current model, speed, and pitch',
        });
        const piperTestBtn = new Gtk.Button({
            label: 'Test Voice',
            valign: Gtk.Align.CENTER,
        });
        piperTestRow.add_suffix(piperTestBtn);

        ttsGroup.add(ttsMuteRow);
        ttsGroup.add(ttsProviderRow);
        ttsGroup.add(kokoroBaseUrlRow);
        ttsGroup.add(kokoroApiKeyRow);
        ttsGroup.add(elevenlabsApiKeyRow);
        ttsGroup.add(elevenlabsVoiceRow);
        ttsGroup.add(elevenlabsVoiceIdRow);
        ttsGroup.add(elevenlabsModelRow);
        ttsGroup.add(piperLangRow);
        ttsGroup.add(piperModelRow);
        ttsGroup.add(piperCustomModelRow);
        ttsGroup.add(piperPresetRow);
        ttsGroup.add(piperSpeedRow);
        ttsGroup.add(piperPitchRow);
        ttsGroup.add(piperNoiseRow);
        ttsGroup.add(piperTestRow);
        voicePage.add(ttsGroup);

        // 4. AI Page
        const aiPage = new Adw.PreferencesPage({
            title: 'AI',
            icon_name: 'system-run-symbolic',
        });

        const aiGroup = new Adw.PreferencesGroup({
            title: 'AI Provider',
            description: 'API keys stay securely in your OS keyring.',
        });
        const refreshModelsBtn = new Gtk.Button({
            icon_name: 'view-refresh-symbolic',
            valign: Gtk.Align.CENTER,
            tooltip_text: 'Refresh available models',
            css_classes: ['flat'],
        });
        aiGroup.set_header_suffix(refreshModelsBtn);
        const providerRow = makeComboRow('Provider', AI_PROVIDERS);
        const baseUrlRow = new Adw.EntryRow({
            title: 'Base URL',
            show_apply_button: true,
        });
        const {row: aiApiKeyRow, badge: aiApiKeyBadge} = makeSecretRow('API Key');
        const modelRow = makeSearchableComboRow({
            title: 'Model',
            subtitle: 'Select a model from the active provider',
            model: Gtk.StringList.new(['No models available']),
            sensitive: false,
        });
        const contextSizeRow = makeSearchableComboRow({
            title: 'Context Size',
            subtitle: 'Context window tokens (Auto uses provider default)',
            model: Gtk.StringList.new(contextPresetOptions.map(o => o.label)),
        });
        const focusWindowRow = new Adw.SpinRow({
            title: 'Focus Window',
            subtitle: 'Latest history messages sent to LLM + user message (0 = full history)',
            adjustment: new Gtk.Adjustment({
                lower: 0,
                upper: 500,
                step_increment: 1,
                page_increment: 5,
                value: 0,
            }),
            digits: 0,
        });
        const maxToolRoundsRow = new Adw.SpinRow({
            title: 'Max Tool Loops',
            subtitle: 'Max tool-call rounds per request before wrapping up (1-100)',
            adjustment: new Gtk.Adjustment({
                lower: 1,
                upper: 100,
                step_increment: 1,
                page_increment: 5,
                value: 25,
            }),
            digits: 0,
        });
        aiGroup.add(providerRow);
        aiGroup.add(baseUrlRow);
        aiGroup.add(aiApiKeyRow);
        aiGroup.add(modelRow);
        aiGroup.add(contextSizeRow);
        aiGroup.add(focusWindowRow);
        aiGroup.add(maxToolRoundsRow);

        const toolsStatusRow = new Adw.ActionRow({
            title: 'Tools Support',
            subtitle: 'Auto-detected capability for the selected model',
        });
        const toolsBadge = new Gtk.Label({
            label: 'Unknown',
            valign: Gtk.Align.CENTER,
            css_classes: ['caption', 'dim-label'],
        });
        toolsStatusRow.add_suffix(toolsBadge);
        aiGroup.add(toolsStatusRow);

        const forceToolsRow = new Adw.SwitchRow({
            title: 'Force Enable Tools',
            subtitle: 'Override detection and expose MCP tools to this model',
        });
        aiGroup.add(forceToolsRow);
        aiPage.add(aiGroup);

        const mcpGroup = new Adw.PreferencesGroup({
            title: 'MCP Servers',
            description: 'Enabled when active model supports tools or Force Enable Tools is active.',
        });
        const addMcpBtn = new Gtk.Button({
            label: 'Add MCP Server',
            valign: Gtk.Align.CENTER,
            css_classes: ['flat'],
        });
        mcpGroup.set_header_suffix(addMcpBtn);
        aiPage.add(mcpGroup);

        const mcpJsonGroup = new Adw.PreferencesGroup({
            title: 'Raw mcpServers JSON',
            description: 'Two-way synchronized with the structured MCP server list above.',
        });
        const mcpJsonView = new Gtk.TextView({
            wrap_mode: Gtk.WrapMode.NONE,
            top_margin: 8,
            bottom_margin: 8,
            left_margin: 10,
            right_margin: 10,
            css_classes: ['monospace'],
        });
        const mcpJsonScroll = new Gtk.ScrolledWindow({min_content_height: 180});
        mcpJsonScroll.set_child(mcpJsonView);
        const mcpJsonFrame = new Gtk.Frame();
        mcpJsonFrame.set_child(mcpJsonScroll);
        mcpJsonGroup.add(mcpJsonFrame);

        const applyMcpJsonBox = new Gtk.Box({
            halign: Gtk.Align.END,
            margin_top: 8,
        });
        const applyMcpJsonBtn = new Gtk.Button({
            label: 'Validate & Apply JSON',
            css_classes: ['suggested-action'],
        });
        applyMcpJsonBox.append(applyMcpJsonBtn);
        mcpJsonGroup.add(applyMcpJsonBox);
        aiPage.add(mcpJsonGroup);

        // 3b. God Mode Page (after AI)
        const godModePage = new Adw.PreferencesPage({
            title: 'God Mode',
            icon_name: 'applications-engineering-symbolic',
        });

        const godModeGroup = new Adw.PreferencesGroup({
            title: 'God Mode',
            description:
                'When enabled, Bytey acts as Prompt Architect and delegates implementation ' +
                'non-interactively to an enabled external AI CLI.',
        });

        const godModeEnabledRow = new Adw.SwitchRow({
            title: 'Enable God Mode',
            subtitle: 'Use external AI CLIs instead of direct workspace edits',
        });
        godModeGroup.add(godModeEnabledRow);

        const godModeApprovalRow = new Adw.SwitchRow({
            title: 'Require Approval Before Running CLI',
            subtitle: 'Preview target CLI, command, and prepared prompt before execution',
        });
        godModeGroup.add(godModeApprovalRow);

        const godModeTimeoutRow = new Adw.SpinRow({
            title: 'Inactivity Timeout (seconds)',
            subtitle: 'Kill CLI only if idle with no output for this many seconds (10–1800s)',
            adjustment: new Gtk.Adjustment({
                lower: 10,
                upper: 1800,
                step_increment: 10,
                page_increment: 60,
                value: 300,
            }),
        });
        godModeGroup.add(godModeTimeoutRow);
        godModePage.add(godModeGroup);

        const agentsGroup = new Adw.PreferencesGroup({
            title: 'Supported CLI Agents',
            description:
                'Detected external AI CLIs can be toggled individually and assigned a model. ' +
                'Missing CLIs remain disabled.',
        });
        const detectAgentsBtn = new Gtk.Button({
            label: 'Detect CLIs',
            valign: Gtk.Align.CENTER,
        });
        agentsGroup.set_header_suffix(detectAgentsBtn);
        godModePage.add(agentsGroup);

        // 4. Import / Export Page
        const importExportPage = new Adw.PreferencesPage({
            title: 'Import / Export',
            icon_name: 'document-save-symbolic',
        });

        const backupGroup = new Adw.PreferencesGroup({
            title: 'Backup & Restore',
            description: 'Export all configuration to a JSON file, or import a JSON file to replace all current settings.',
        });

        const exportRow = new Adw.ActionRow({
            title: 'Export Configuration',
            subtitle: 'Save all non-secret settings (general, AI provider, voice, visuals, MCP) to JSON',
        });
        const exportBtn = new Gtk.Button({
            label: 'Export JSON…',
            valign: Gtk.Align.CENTER,
        });
        exportRow.add_suffix(exportBtn);
        backupGroup.add(exportRow);

        const importRow = new Adw.ActionRow({
            title: 'Import Configuration',
            subtitle: 'Replace all existing settings from a previously exported JSON file',
        });
        const importBtn = new Gtk.Button({
            label: 'Import JSON…',
            valign: Gtk.Align.CENTER,
            css_classes: ['destructive-action'],
        });
        importRow.add_suffix(importBtn);
        backupGroup.add(importRow);

        importExportPage.add(backupGroup);

        // 5. Skills Page
        const skillsPage = new Adw.PreferencesPage({
            title: 'Skills',
            icon_name: 'applications-science-symbolic',
        });

        const skillsGeneralGroup = new Adw.PreferencesGroup({
            title: 'General & Storage',
        });

        const skillsEnabledRow = new Adw.SwitchRow({
            title: 'Enable Skills',
            subtitle: 'Expose discovered skills and slash commands to the AI assistant',
        });
        skillsGeneralGroup.add(skillsEnabledRow);

        const skillsAllowScriptsRow = new Adw.SwitchRow({
            title: 'Allow Skill Scripts',
            subtitle: 'Warning: allows skills to execute bundled local scripts on your system',
        });
        skillsGeneralGroup.add(skillsAllowScriptsRow);

        const skillsDirRow = new Adw.ActionRow({
            title: 'Skills Storage Folder',
            subtitle: 'Default (~/.config/bytey-ai-companion/skills)',
        });
        const skillsDirBtnBox = new Gtk.Box({
            orientation: Gtk.Orientation.HORIZONTAL,
            spacing: 6,
            valign: Gtk.Align.CENTER,
        });
        const skillsBrowseBtn = new Gtk.Button({label: 'Browse'});
        const skillsResetBtn = new Gtk.Button({label: 'Reset'});
        const skillsOpenBtn = new Gtk.Button({label: 'Open Folder'});
        skillsDirBtnBox.append(skillsBrowseBtn);
        skillsDirBtnBox.append(skillsResetBtn);
        skillsDirBtnBox.append(skillsOpenBtn);
        skillsDirRow.add_suffix(skillsDirBtnBox);
        skillsGeneralGroup.add(skillsDirRow);

        const skillsExtraDirsRow = new Adw.EntryRow({
            title: 'Extra Skill Directories (comma-separated)',
            show_apply_button: true,
        });
        skillsGeneralGroup.add(skillsExtraDirsRow);

        const skillsRescanRow = new Adw.ActionRow({
            title: 'Rescan Skills',
            subtitle: 'Reload skills from storage and extra directories',
        });
        const skillsRescanBtn = new Gtk.Button({
            label: 'Rescan Skills',
            valign: Gtk.Align.CENTER,
        });
        skillsRescanRow.add_suffix(skillsRescanBtn);
        skillsGeneralGroup.add(skillsRescanRow);
        skillsPage.add(skillsGeneralGroup);

        const skillsListGroup = new Adw.PreferencesGroup({
            title: 'Detected Skills',
            description: 'Manage user, extra, and built-in skills.',
        });
        const skillsHeaderBox = new Gtk.Box({
            orientation: Gtk.Orientation.HORIZONTAL,
            spacing: 6,
            valign: Gtk.Align.CENTER,
        });
        const addSkillBtn = new Gtk.Button({
            label: '+ Add Skill',
            css_classes: ['suggested-action'],
        });
        const importSkillBtn = new Gtk.Button({label: 'Import'});
        skillsHeaderBox.append(addSkillBtn);
        skillsHeaderBox.append(importSkillBtn);
        skillsListGroup.set_header_suffix(skillsHeaderBox);
        skillsPage.add(skillsListGroup);

        // 6. Workspace / Coding Page
        const workspacePage = new Adw.PreferencesPage({
            title: 'Workspace',
            icon_name: 'folder-symbolic',
        });

        const workspaceFolderGroup = new Adw.PreferencesGroup({
            title: 'Work Folder',
            description: 'Bind a project folder for workspace skills, project rules, and coding tools.',
        });

        const workspaceActiveRow = new Adw.ActionRow({
            title: 'Active Work Folder',
            subtitle: 'None (General Assistant)',
        });
        const workspaceBtnBox = new Gtk.Box({
            orientation: Gtk.Orientation.HORIZONTAL,
            spacing: 6,
            valign: Gtk.Align.CENTER,
        });
        const workspaceChooseBtn = new Gtk.Button({
            label: 'Choose Folder',
            css_classes: ['suggested-action'],
        });
        const workspaceClearBtn = new Gtk.Button({
            label: 'Clear',
            sensitive: false,
        });
        workspaceBtnBox.append(workspaceChooseBtn);
        workspaceBtnBox.append(workspaceClearBtn);
        workspaceActiveRow.add_suffix(workspaceBtnBox);
        workspaceFolderGroup.add(workspaceActiveRow);

        const workspaceRecentRow = makeSearchableComboRow({
            title: 'Recent Work Folders',
            subtitle: 'Quickly switch to a previously used folder',
            model: Gtk.StringList.new(['None (General Assistant)']),
        });
        workspaceFolderGroup.add(workspaceRecentRow);
        workspacePage.add(workspaceFolderGroup);

        const workspacePolicyGroup = new Adw.PreferencesGroup({
            title: 'Coding & Agent Policy',
        });

        const workspaceAutoSkillsRow = new Adw.SwitchRow({
            title: 'Auto-Load Workspace Skills',
            subtitle: 'Discover skills in .agents/skills and .bytey/skills inside the active folder',
        });
        workspacePolicyGroup.add(workspaceAutoSkillsRow);

        const workspaceAutoRulesRow = new Adw.SwitchRow({
            title: 'Auto-Load Project Rules',
            subtitle: 'Inject AGENTS.md or .agents/GEMINI.md conventions into context',
        });
        workspacePolicyGroup.add(workspaceAutoRulesRow);

        const workspaceWriteApprovalRow = new Adw.SwitchRow({
            title: 'Require Approval for File Edits',
            subtitle: 'Show diff preview and require confirmation before modifying files',
        });
        workspacePolicyGroup.add(workspaceWriteApprovalRow);

        const workspaceCmdApprovalRow = new Adw.SwitchRow({
            title: 'Require Approval for Commands',
            subtitle: 'Require confirmation before running shell commands in the workspace',
        });
        workspacePolicyGroup.add(workspaceCmdApprovalRow);
        workspacePage.add(workspacePolicyGroup);

        const expandPreferencesPage = widget => {
            if (widget instanceof Adw.Clamp) {
                widget.set_maximum_size(100000);
                widget.set_tightening_threshold(100000);
                return;
            }
            let child = widget.get_first_child?.();
            while (child) {
                expandPreferencesPage(child);
                child = child.get_next_sibling?.();
            }
        };
        for (const page of [
            generalPage,
            visualsPage,
            voicePage,
            aiPage,
            godModePage,
            skillsPage,
            workspacePage,
            importExportPage,
        ]) {
            expandPreferencesPage(page);
            window.add(page);
        }

        let workspaceRecentPaths = [];
        let workspaceUpdating = false;

        const syncWorkspaceControls = (activePath, recentPaths) => {
            const active = String(activePath ?? '').trim();
            const recent = Array.isArray(recentPaths)
                ? recentPaths.map(p => String(p).trim()).filter(Boolean)
                : [];
            workspaceActiveRow.set_subtitle(active || 'None (General Assistant)');
            workspaceClearBtn.set_sensitive(Boolean(active));
            workspaceUpdating = true;
            try {
                workspaceRecentPaths = recent;
                workspaceRecentRow.set_model(
                    Gtk.StringList.new(['None (General Assistant)', ...recent])
                );
                if (active && recent.includes(active))
                    workspaceRecentRow.set_selected(recent.indexOf(active) + 1);
                else
                    workspaceRecentRow.set_selected(0);
            } finally {
                workspaceUpdating = false;
            }
        };

        const setWorkspacePath = async path => {
            const cleaned = String(path ?? '').trim();
            const resp = await httpJson('PUT', '/api/v1/workspace', {path: cleaned});
            if (destroyed)
                return;
            if (!resp.ok) {
                if (resp.status === 0)
                    setOfflineState(true);
                showToast(`Workspace update failed: ${resp.error || 'Daemon unreachable'}`);
                return;
            }
            setOfflineState(false);
            const data = resp.data && typeof resp.data === 'object' ? resp.data : {};
            const nextActive = String(data.active_path ?? cleaned).trim();
            const nextRecent = Array.isArray(data.recent_paths)
                ? data.recent_paths
                : (nextActive
                    ? [nextActive, ...workspaceRecentPaths.filter(p => p !== nextActive)].slice(0, 10)
                    : workspaceRecentPaths);
            syncWorkspaceControls(nextActive, nextRecent);
            showToast(nextActive ? `Workspace set to ${nextActive}` : 'Workspace cleared');
        };

        const syncPlacementSensitivity = () => {
            const mode = positionModeValueForIndex(posModeRow.get_selected());
            const isFixed = mode === 'fixed';
            const isFloating = mode === 'floating';
            fixedAlignRow.set_sensitive(isFixed);
            offsetXRow.set_sensitive(isFixed);
            offsetYRow.set_sensitive(isFixed);
            floatXRow.set_sensitive(isFloating);
            floatYRow.set_sensitive(isFloating);
        };

        const syncSttVisibility = () => {
            const backend = comboText(sttBackendRow);
            sttApiUrlRow.set_visible(backend === 'custom_whisper');
            sttApiKeyRow.set_visible(backend !== 'local_whisper');
            sttApiKeyRow.set_title(backend === 'openai_whisper' ? 'OpenAI Whisper API Key' : 'STT API Key');
        };

        const syncTtsVisibility = () => {
            const ttsProvider = comboText(ttsProviderRow);
            const isKokoro = ttsProvider === 'kokoro';
            kokoroBaseUrlRow.set_visible(isKokoro);
            kokoroApiKeyRow.set_visible(isKokoro);
            const isElevenLabs = ttsProvider === 'elevenlabs';
            elevenlabsApiKeyRow.set_visible(isElevenLabs);
            elevenlabsVoiceRow.set_visible(isElevenLabs);
            elevenlabsVoiceIdRow.set_visible(isElevenLabs);
            elevenlabsModelRow.set_visible(isElevenLabs);
            const isPiper = ttsProvider === 'piper';
            piperLangRow.set_visible(isPiper);
            piperModelRow.set_visible(isPiper);
            piperCustomModelRow.set_visible(isPiper);
            piperPresetRow.set_visible(isPiper);
            piperSpeedRow.set_visible(isPiper);
            piperPitchRow.set_visible(isPiper);
            piperNoiseRow.set_visible(isPiper);
            piperTestRow.set_visible(isPiper);
        };

        const syncProviderVisibility = () => {
            const prov = comboText(providerRow);
            baseUrlRow.set_visible(prov !== 'gemini');
            const secretKey = PROVIDER_SECRET_MAP[prov];
            if (secretKey) {
                aiApiKeyRow.set_visible(true);
                setSecretRowState(aiApiKeyRow, aiApiKeyBadge, Boolean(secretsConfigured[secretKey]));
            } else {
                aiApiKeyRow.set_visible(false);
            }
        };

        const updateToolsSensitivity = () => {
            const detected = modelToolMap.has(currentConfiguredModel)
                ? Boolean(modelToolMap.get(currentConfiguredModel))
                : Boolean(detectedToolsSupport);
            const forced = forceToolsRow.get_active();
            const active = Boolean(detected || forced);

            toolsBadge.remove_css_class('success');
            toolsBadge.remove_css_class('warning');
            toolsBadge.remove_css_class('dim-label');
            if (detected) {
                toolsBadge.set_label('Supported');
                toolsBadge.add_css_class('success');
            } else if (forced) {
                toolsBadge.set_label('Forced On');
                toolsBadge.add_css_class('warning');
            } else {
                toolsBadge.set_label('Not Supported');
                toolsBadge.add_css_class('dim-label');
            }

            mcpGroup.set_sensitive(active);
            mcpJsonGroup.set_sensitive(active);
        };

        const refreshContextSizeRow = ({clampAndReturnChanged = false} = {}) => {
            const maxCtx = modelContextMap.has(currentConfiguredModel)
                ? modelContextMap.get(currentConfiguredModel)
                : detectedMaxContext;
            contextPresetOptions = buildContextPresets(maxCtx, currentContextSize);
            let changed = false;
            if (clampAndReturnChanged) {
                const nextVal = clampContextValue(currentContextSize, contextPresetOptions);
                changed = nextVal !== currentContextSize;
                currentContextSize = nextVal;
            }
            const selIdx = Math.max(0, contextPresetOptions.findIndex(o => o.value === currentContextSize));
            const prevLoading = loading;
            loading = true;
            try {
                contextSizeRow.set_model(Gtk.StringList.new(contextPresetOptions.map(o => o.label)));
                contextSizeRow.set_selected(selIdx);
                contextSizeRow.set_subtitle(
                    maxCtx
                        ? `Max supported by model: ${formatContextShort(maxCtx)} (${formatNumberCommas(maxCtx)} tokens)`
                        : 'Context window tokens (Auto uses provider default)'
                );
            } finally {
                loading = prevLoading;
            }
            return changed;
        };

        const normalizeMcpMap = rawMap => {
            const normalized = {};
            if (!rawMap || typeof rawMap !== 'object' || Array.isArray(rawMap))
                return normalized;
            for (const [name, entry] of Object.entries(rawMap)) {
                if (!entry || typeof entry !== 'object' || Array.isArray(entry))
                    continue;
                const item = cloneJson(entry);
                const disabled = Boolean(item.disabled ?? (item.enabled === false));
                item.disabled = disabled;
                item.enabled = !disabled;
                normalized[String(name)] = item;
            }
            return normalized;
        };

        const syncMcpJsonText = (rawJsonOverride = null) => {
            const text = rawJsonOverride !== null
                ? String(rawJsonOverride)
                : JSON.stringify({mcpServers}, null, 2);
            mcpJsonView.get_buffer().set_text(text, -1);
        };

        const setOfflineState = offline => {
            offlineBanner.set_revealed(offline);
            statusRow.set_subtitle(offline ? 'Offline' : 'Connected');
        };

        const setEmptyModelsState = message => {
            loading = true;
            try {
                availableModels = [];
                modelToolMap.clear();
                modelContextMap.clear();
                modelRow.set_model(Gtk.StringList.new(['No models available']));
                modelRow.set_selected(0);
                modelRow.set_sensitive(false);
                modelRow.set_subtitle(message);
            } finally {
                loading = false;
            }
            updateToolsSensitivity();
            refreshContextSizeRow();
        };

        const fetchModels = async (
            provider,
            baseUrl = '',
            {autoSelectIfMissing = false, showResultToast = false} = {}
        ) => {
            if (destroyed)
                return;
            const seq = ++modelFetchSeq;
            const prov = String(provider || '').trim().toLowerCase();
            if (!prov)
                return;

            modelRow.set_sensitive(false);
            modelRow.set_subtitle(`Loading models from ${prov}...`);

            let path = `/api/v1/providers/${encodeURIComponent(prov)}/models`;
            const cleanUrl = String(baseUrl || '').trim();
            if (prov !== 'gemini' && cleanUrl)
                path += `?base_url=${encodeURIComponent(cleanUrl)}`;

            const resp = await httpJson('GET', path);
            if (destroyed || seq !== modelFetchSeq)
                return;

            if (!resp.ok || !resp.data || typeof resp.data !== 'object') {
                setOfflineState(true);
                setEmptyModelsState('Daemon offline — unable to fetch models');
                if (showResultToast)
                    showToast('Unable to fetch models — daemon offline');
                return;
            }
            setOfflineState(false);

            const rawModels = Array.isArray(resp.data.models) ? resp.data.models : [];
            const parsedModels = [];
            const seenIds = new Set();
            for (const entry of rawModels) {
                let id = '';
                let name = '';
                let supportsTools;
                let contextLength = null;
                if (typeof entry === 'string') {
                    id = entry.trim();
                    name = id;
                } else if (entry && typeof entry === 'object') {
                    id = String(entry.id ?? entry.name ?? '').trim();
                    name = String(entry.name ?? id).trim();
                    if (typeof entry.supports_tools === 'boolean')
                        supportsTools = entry.supports_tools;
                    contextLength = normalizePositiveInt(entry.context_length);
                }
                if (id && !seenIds.has(id)) {
                    seenIds.add(id);
                    parsedModels.push({
                        id,
                        name: name || id,
                        supports_tools: supportsTools,
                        context_length: contextLength,
                    });
                }
            }

            if (parsedModels.length === 0) {
                const errMsg = resp.data.error
                    ? String(resp.data.error)
                    : `No models returned by ${prov}`;
                setEmptyModelsState(errMsg);
                if (showResultToast)
                    showToast(errMsg);
                return;
            }

            parsedModels.sort((a, b) => a.id.localeCompare(b.id, undefined, {sensitivity: 'base'}));
            availableModels = parsedModels;
            modelToolMap.clear();
            modelContextMap.clear();
            for (const m of availableModels) {
                if (typeof m.supports_tools === 'boolean')
                    modelToolMap.set(m.id, m.supports_tools);
                if (m.context_length !== null)
                    modelContextMap.set(m.id, m.context_length);
            }
            const modelIds = availableModels.map(m => m.id);
            const matchIdx = currentConfiguredModel ? modelIds.indexOf(currentConfiguredModel) : -1;
            const targetIdx = matchIdx >= 0 ? matchIdx : 0;
            const chosen = availableModels[targetIdx];

            loading = true;
            try {
                modelRow.set_model(Gtk.StringList.new(modelIds));
                modelRow.set_selected(targetIdx);
                modelRow.set_sensitive(true);
                const errHint = resp.data.error ? ` (fallback: ${resp.data.error})` : '';
                modelRow.set_subtitle(`${availableModels.length} model(s) available${errHint}`);
            } finally {
                loading = false;
            }

            if (matchIdx < 0 && autoSelectIfMissing && chosen) {
                currentConfiguredModel = chosen.id;
                updateToolsSensitivity();
                const ctxClamped = refreshContextSizeRow({clampAndReturnChanged: true});
                const patch = {provider: {model: chosen.id}};
                if (ctxClamped)
                    patch.provider.context_size = currentContextSize;
                if (typeof chosen.supports_tools === 'boolean')
                    patch.supports_tools = chosen.supports_tools;
                await patchConfig(patch, `Selected model ${chosen.id}`);
            } else {
                updateToolsSensitivity();
                const ctxClamped = refreshContextSizeRow({clampAndReturnChanged: true});
                if (ctxClamped) {
                    await patchConfig(
                        {provider: {context_size: currentContextSize}},
                        `Context size clamped to ${formatContextShort(currentContextSize)}`
                    );
                } else if (showResultToast) {
                    showToast(`Loaded ${availableModels.length} model(s) from ${prov}`);
                }
            }
        };

        const syncPiperDownloadButton = () => {
            if (piperDownloading) {
                piperDownloadBtn.set_label('Downloading...');
                piperDownloadBtn.set_sensitive(false);
                piperDownloadBtn.set_tooltip_text('Downloading selected Piper voice model...');
                return;
            }
            const idx = piperModelRow.get_selected();
            const selectedVoice = idx >= 0 && idx < piperFilteredVoices.length
                ? piperFilteredVoices[idx]
                : null;

            if (!selectedVoice) {
                if (piperVoices.length > 0 && piperFilteredVoices.length === 0) {
                    piperDownloadBtn.set_label('Download');
                    piperDownloadBtn.set_sensitive(false);
                    piperDownloadBtn.set_tooltip_text('No matching Piper voice selected');
                    return;
                }
                const customVal = currentPiperModel || DEFAULT_PIPER_MODEL;
                const looksLikePath = customVal.includes('/') || customVal.endsWith('.onnx');
                piperDownloadBtn.set_label('Download');
                piperDownloadBtn.set_sensitive(!looksLikePath && Boolean(customVal));
                piperDownloadBtn.set_tooltip_text(
                    looksLikePath
                        ? 'Custom .onnx path configured'
                        : 'Download selected Piper voice model'
                );
                return;
            }

            const countPrefix = `${piperFilteredVoices.length} voice(s)`;
            const qualityTag = selectedVoice.quality ? ` • ${selectedVoice.quality}` : '';
            if (selectedVoice.installed) {
                piperDownloadBtn.set_label('Installed');
                piperDownloadBtn.set_sensitive(false);
                piperDownloadBtn.set_tooltip_text(`${selectedVoice.id} is already downloaded`);
                piperModelRow.set_subtitle(`${countPrefix} • Installed locally${qualityTag}`);
            } else {
                piperDownloadBtn.set_label('Download');
                piperDownloadBtn.set_sensitive(true);
                piperDownloadBtn.set_tooltip_text(`Download ${selectedVoice.id}`);
                piperModelRow.set_subtitle(`${countPrefix} • Click Download to install${qualityTag}`);
            }
        };

        const rebuildPiperLanguageFilter = () => {
            const langMap = new Map();
            for (const v of piperVoices) {
                if (v.languageCode && !langMap.has(v.languageCode))
                    langMap.set(v.languageCode, formatPiperLanguageLabel(v.languageCode, v.languageName));
            }
            const entries = Array.from(langMap.entries())
                .map(([code, label]) => ({code, label}))
                .sort((a, b) => a.label.localeCompare(b.label));
            piperLanguageOptions = [{code: '', label: 'All Languages'}, ...entries];
            if (currentPiperLanguage && !langMap.has(currentPiperLanguage))
                currentPiperLanguage = '';
            const selIdx = Math.max(0, piperLanguageOptions.findIndex(o => o.code === currentPiperLanguage));

            const prevLoading = loading;
            loading = true;
            try {
                piperLangRow.set_model(Gtk.StringList.new(piperLanguageOptions.map(o => o.label)));
                piperLangRow.set_selected(selIdx);
            } finally {
                loading = prevLoading;
            }
        };

        const rebuildPiperModelCombo = () => {
            if (piperVoices.length === 0) {
                piperFilteredVoices = [];
                const fallbackLabel = currentPiperModel || DEFAULT_PIPER_MODEL;
                const prevLoading = loading;
                loading = true;
                try {
                    piperModelRow.set_model(Gtk.StringList.new([fallbackLabel]));
                    piperModelRow.set_selected(0);
                    piperModelRow.set_sensitive(true);
                    piperModelRow.set_subtitle('Local CPU neural voice model');
                } finally {
                    loading = prevLoading;
                }
                syncPiperDownloadButton();
                return;
            }

            piperFilteredVoices = currentPiperLanguage
                ? piperVoices.filter(v => v.languageCode === currentPiperLanguage)
                : [...piperVoices];

            if (piperFilteredVoices.length === 0) {
                const prevLoading = loading;
                loading = true;
                try {
                    piperModelRow.set_model(Gtk.StringList.new(['No voices for selected language']));
                    piperModelRow.set_selected(0);
                    piperModelRow.set_sensitive(false);
                    piperModelRow.set_subtitle('0 voice(s) match language filter');
                } finally {
                    loading = prevLoading;
                }
                syncPiperDownloadButton();
                return;
            }

            const labels = piperFilteredVoices.map(v => `${v.id}${v.installed ? ' ✓' : ''}`);
            const matchIdx = currentPiperModel
                ? piperFilteredVoices.findIndex(v => v.id === currentPiperModel)
                : -1;
            const targetIdx = matchIdx >= 0 ? matchIdx : 0;

            const prevLoading = loading;
            loading = true;
            try {
                piperModelRow.set_model(Gtk.StringList.new(labels));
                piperModelRow.set_selected(targetIdx);
                piperModelRow.set_sensitive(true);
            } finally {
                loading = prevLoading;
            }
            syncPiperDownloadButton();
        };

        const fetchPiperVoices = async ({refresh = false, showResultToast = false} = {}) => {
            if (destroyed)
                return;
            const seq = ++piperFetchSeq;
            piperModelRow.set_subtitle('Loading Piper voice catalog...');
            piperRefreshBtn.set_sensitive(false);

            const queryPath = `/api/v1/tts/voices?provider=piper${refresh ? '&refresh=1' : ''}`;
            let resp = await httpJson('GET', queryPath, undefined, {longTimeout: refresh});
            if (destroyed || seq !== piperFetchSeq)
                return;

            if (resp.status !== 0 && (!resp.ok || (Array.isArray(resp.data?.voices) && resp.data.voices.length === 0))) {
                const altPath = `/api/v1/tts/piper/voices${refresh ? '?refresh=1' : ''}`;
                const altResp = await httpJson('GET', altPath, undefined, {longTimeout: refresh});
                if (destroyed || seq !== piperFetchSeq)
                    return;
                if (altResp.ok)
                    resp = altResp;
            }

            piperRefreshBtn.set_sensitive(true);

            if (!resp.ok || !resp.data) {
                if (resp.status === 0)
                    setOfflineState(true);
                piperModelRow.set_subtitle('Unable to load Piper voice catalog');
                syncPiperDownloadButton();
                if (showResultToast)
                    showToast('Unable to fetch Piper voice catalog');
                return;
            }
            setOfflineState(false);

            const rawList = Array.isArray(resp.data)
                ? resp.data
                : (Array.isArray(resp.data.voices)
                    ? resp.data.voices
                    : (Array.isArray(resp.data.models) ? resp.data.models : []));
            const parsed = [];
            const seen = new Set();
            for (const entry of rawList) {
                const normalized = normalizePiperVoiceEntry(entry);
                if (normalized && !seen.has(normalized.id)) {
                    seen.add(normalized.id);
                    parsed.push(normalized);
                }
            }

            piperVoices = parsed;
            rebuildPiperLanguageFilter();
            rebuildPiperModelCombo();
            if (piperVoices.length === 0) {
                const errHint = resp.data?.error
                    ? String(resp.data.error)
                    : 'No catalog voices returned (enter Model ID below)';
                piperModelRow.set_subtitle(errHint);
            }
            if (showResultToast) {
                showToast(
                    piperVoices.length > 0
                        ? `Loaded ${piperVoices.length} Piper voice(s)`
                        : 'No Piper voices returned by daemon'
                );
            }
        };

        const syncPiperPresetFromControls = () => {
            const matchedIdx = matchPiperPresetIndex(
                piperSpeedRow.get_value(),
                piperPitchRow.get_value(),
                piperNoiseRow.get_value()
            );
            const prevLoading = loading;
            loading = true;
            try {
                piperPresetRow.set_selected(matchedIdx);
            } finally {
                loading = prevLoading;
            }
        };

        const rebuildMcpList = () => {
            for (const widget of mcpRows)
                mcpGroup.remove(widget);
            mcpRows = [];

            const hostRow = new Adw.ActionRow({
                title: 'Bytey MCP Host',
                subtitle: 'Built-in MCP host • Workspace & Skills tools (always enabled)',
            });
            const hostBadge = new Gtk.Label({
                label: 'Built-in',
                valign: Gtk.Align.CENTER,
                css_classes: ['caption', 'dim-label'],
            });
            const hostSwitch = new Gtk.Switch({
                active: true,
                sensitive: false,
                valign: Gtk.Align.CENTER,
                tooltip_text: 'Built-in MCP Host cannot be disabled',
            });
            const hostInfoBtn = new Gtk.Button({
                icon_name: 'dialog-information-symbolic',
                valign: Gtk.Align.CENTER,
                tooltip_text: 'Inspect MCP Host & tools',
                css_classes: ['flat'],
            });
            hostInfoBtn.connect('clicked', () => onUserChange(() => {
                showMcpInfoDialog(null).catch(() => {});
            }));
            hostRow.add_suffix(hostBadge);
            hostRow.add_suffix(hostSwitch);
            hostRow.add_suffix(hostInfoBtn);
            mcpGroup.add(hostRow);
            mcpRows.push(hostRow);

            const names = Object.keys(mcpServers).sort();
            if (names.length === 0) {
                const emptyRow = new Adw.ActionRow({
                    title: 'No external MCP servers configured',
                    subtitle: "Click 'Add MCP Server' or edit the raw mcpServers JSON below.",
                });
                mcpGroup.add(emptyRow);
                mcpRows.push(emptyRow);
                return;
            }

            for (const name of names) {
                const cfg = mcpServers[name] ?? {};
                const transport = String(cfg.transport || (cfg.url ? 'http' : 'stdio'));
                const target = transport !== 'stdio'
                    ? String(cfg.url ?? '')
                    : [String(cfg.command ?? ''), ...(Array.isArray(cfg.args) ? cfg.args.map(a => String(a)) : [])]
                        .join(' ')
                        .trim();
                const confirmTag = cfg.require_confirmation ? ' • Confirm required' : '';
                const row = new Adw.ActionRow({
                    title: name,
                    subtitle: `${transport}: ${target || '(unconfigured)'}${confirmTag}`,
                });

                const enabledSwitch = new Gtk.Switch({
                    active: !Boolean(cfg.disabled),
                    valign: Gtk.Align.CENTER,
                    tooltip_text: 'Enable or disable server',
                });
                enabledSwitch.connect('notify::active', () => onUserChange(() => {
                    if (!mcpServers[name])
                        return;
                    const active = enabledSwitch.get_active();
                    mcpServers[name].disabled = !active;
                    mcpServers[name].enabled = active;
                    pushMcpChanges(`${active ? 'Enabled' : 'Disabled'} ${name}`).catch(() => {});
                }));

                const testMcpBtn = new Gtk.Button({
                    label: 'Test Connection',
                    valign: Gtk.Align.CENTER,
                });
                testMcpBtn.connect('clicked', () => onUserChange(() => {
                    testMcpServer(name).catch(() => {});
                }));

                const infoMcpBtn = new Gtk.Button({
                    icon_name: 'dialog-information-symbolic',
                    valign: Gtk.Align.CENTER,
                    tooltip_text: 'Inspect MCP details & tools',
                    css_classes: ['flat'],
                });
                infoMcpBtn.connect('clicked', () => onUserChange(() => {
                    showMcpInfoDialog(name).catch(() => {});
                }));

                const editMcpBtn = new Gtk.Button({
                    icon_name: 'document-edit-symbolic',
                    valign: Gtk.Align.CENTER,
                    tooltip_text: 'Edit',
                    css_classes: ['flat'],
                });
                editMcpBtn.connect('clicked', () => onUserChange(() => {
                    openMcpDialog(name);
                }));

                const delMcpBtn = new Gtk.Button({
                    icon_name: 'user-trash-symbolic',
                    valign: Gtk.Align.CENTER,
                    tooltip_text: 'Delete',
                    css_classes: ['flat', 'destructive-action'],
                });
                delMcpBtn.connect('clicked', () => onUserChange(() => {
                    if (!(name in mcpServers))
                        return;
                    delete mcpServers[name];
                    pushMcpChanges(`Removed MCP server '${name}'`).catch(() => {});
                }));

                row.add_suffix(enabledSwitch);
                row.add_suffix(testMcpBtn);
                row.add_suffix(infoMcpBtn);
                row.add_suffix(editMcpBtn);
                row.add_suffix(delMcpBtn);
                mcpGroup.add(row);
                mcpRows.push(row);
            }
        };

        const pushMcpChanges = async toastMsg => {
            if (destroyed)
                return;
            const raw = JSON.stringify({mcpServers}, null, 2);
            syncMcpJsonText(raw);
            rebuildMcpList();

            const resp = await httpJson('PUT', '/api/v1/mcp/json', {raw_json: raw});
            if (destroyed)
                return;
            if (!resp.ok) {
                if (resp.status === 0)
                    setOfflineState(true);
                const errDetail = resp.data?.error ? `: ${resp.data.error}` : '';
                showToast(`MCP update failed${errDetail}`);
                return;
            }
            setOfflineState(false);
            if (resp.data?.mcpServers && typeof resp.data.mcpServers === 'object') {
                mcpServers = normalizeMcpMap(resp.data.mcpServers);
                rebuildMcpList();
            }
            syncMcpJsonText(resp.data?.raw_json ?? null);
            showToast(toastMsg);
        };

        const applyRawMcpJson = async () => {
            if (destroyed)
                return;
            const raw = readTextBuffer(mcpJsonView);
            let parsed;
            try {
                parsed = JSON.parse(raw);
            } catch (err) {
                showToast(`Invalid JSON: ${err.message}`);
                return;
            }
            if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
                showToast('Root JSON value must be an object');
                return;
            }
            const servers = Object.prototype.hasOwnProperty.call(parsed, 'mcpServers')
                ? parsed.mcpServers
                : parsed;
            if (!servers || typeof servers !== 'object' || Array.isArray(servers)) {
                showToast('"mcpServers" must be a JSON object');
                return;
            }

            const resp = await httpJson('PUT', '/api/v1/mcp/json', {raw_json: raw});
            if (destroyed)
                return;
            if (!resp.ok) {
                if (resp.status === 0)
                    setOfflineState(true);
                const errDetail = resp.data?.error ? `: ${resp.data.error}` : '';
                showToast(`MCP apply failed${errDetail}`);
                return;
            }
            setOfflineState(false);
            mcpServers = normalizeMcpMap(resp.data?.mcpServers ?? servers);
            syncMcpJsonText(resp.data?.raw_json ?? null);
            rebuildMcpList();
            showToast(`Applied mcpServers JSON (${Object.keys(mcpServers).length} server(s))`);
        };

        const testMcpServer = async name => {
            const cfg = mcpServers[name];
            if (!cfg || destroyed)
                return;
            showToast(`Testing MCP server '${name}'...`);
            const resp = await httpJson('POST', '/api/v1/mcp/test', {name, config: cfg});
            if (destroyed)
                return;
            if (!resp.ok) {
                if (resp.status === 0)
                    setOfflineState(true);
                const errMsg = resp.data?.error
                    ? String(resp.data.error)
                    : `HTTP ${resp.status || 'offline'}`;
                showToast(`Test failed for ${name}: ${errMsg}`);
                const alert = Adw.AlertDialog.new(`MCP Test Failed: ${name}`, errMsg);
                alert.add_response('close', 'Close');
                alert.present(window);
                return;
            }
            setOfflineState(false);
            const payload = resp.data && typeof resp.data === 'object' ? resp.data : {};
            if (payload.ok === false) {
                const err = String(payload.error ?? 'Connection failed');
                showToast(`${name}: ${err}`);
                const alert = Adw.AlertDialog.new(`MCP Test Failed: ${name}`, err);
                alert.add_response('close', 'Close');
                alert.present(window);
                return;
            }
            const tools = Array.isArray(payload.tools) ? payload.tools : [];
            const lines = tools.map(t => {
                if (t && typeof t === 'object') {
                    const tName = String(t.name ?? 'unknown');
                    const tDesc = t.description ? ` — ${t.description}` : '';
                    return `• ${tName}${tDesc}`;
                }
                return `• ${String(t)}`;
            });
            const summary = lines.length > 0
                ? lines.join('\n')
                : 'Connected successfully (0 tools advertised).';
            showToast(`${name}: discovered ${lines.length} tool(s)`);
            const alert = Adw.AlertDialog.new(`MCP Connected: ${name}`, summary);
            alert.add_response('close', 'Close');
            alert.present(window);
        };

        const showMcpInfoDialog = async name => {
            if (destroyed)
                return;
            const isBuiltin = name === null;
            const cfg = isBuiltin ? null : (mcpServers[name] ?? {});
            const mcpTitle = isBuiltin ? 'Bytey MCP Host' : String(name);
            let mcpDesc = 'Built-in MCP host providing native Workspace and Skills tools for Bytey.';
            if (!isBuiltin) {
                const transport = String(cfg.transport || (cfg.url ? 'http' : 'stdio'));
                const target = transport !== 'stdio'
                    ? String(cfg.url ?? '')
                    : [String(cfg.command ?? ''), ...(Array.isArray(cfg.args) ? cfg.args.map(a => String(a)) : [])]
                        .join(' ')
                        .trim();
                mcpDesc = cfg.description
                    ? String(cfg.description)
                    : `External ${transport} MCP server${target ? `: ${target}` : ''}`;
            }

            const dialog = new Adw.Dialog({
                title: mcpTitle,
                content_width: 540,
                content_height: 520,
            });
            const toolbar = new Adw.ToolbarView();
            const header = new Adw.HeaderBar({
                show_start_title_buttons: false,
                show_end_title_buttons: false,
            });
            const closeBtn = new Gtk.Button({label: 'Close'});
            closeBtn.connect('clicked', () => dialog.close());
            header.pack_end(closeBtn);
            toolbar.add_top_bar(header);

            const page = new Adw.PreferencesPage();
            const infoGroup = new Adw.PreferencesGroup({title: 'MCP Details'});
            const titleRow = new Adw.ActionRow({
                title: 'Title',
                subtitle: mcpTitle,
                subtitle_selectable: true,
            });
            const descRow = new Adw.ActionRow({
                title: 'Description',
                subtitle: mcpDesc,
                subtitle_lines: 0,
                subtitle_selectable: true,
            });
            infoGroup.add(titleRow);
            infoGroup.add(descRow);
            page.add(infoGroup);

            const toolsGroup = new Adw.PreferencesGroup({
                title: 'Tools',
                description: 'Discovering available tools...',
            });
            const loadingRow = new Adw.ActionRow({
                title: 'Loading tools...',
                subtitle: isBuiltin ? 'Fetching built-in Bytey MCP Host tools' : `Connecting to ${mcpTitle}`,
            });
            toolsGroup.add(loadingRow);
            page.add(toolsGroup);

            const scroll = new Gtk.ScrolledWindow({vexpand: true});
            scroll.set_child(page);
            toolbar.set_content(scroll);
            dialog.set_child(toolbar);
            dialog.present(window);

            const resp = isBuiltin
                ? await httpJson('GET', '/api/v1/mcp/host')
                : await httpJson('POST', '/api/v1/mcp/test', {name: mcpTitle, config: cfg});
            if (destroyed)
                return;

            toolsGroup.remove(loadingRow);
            if (!resp.ok) {
                if (resp.status === 0)
                    setOfflineState(true);
                const errMsg = resp.data?.error
                    ? String(resp.data.error)
                    : `HTTP ${resp.status || 'offline'}`;
                toolsGroup.set_description('Failed to load tools');
                toolsGroup.add(new Adw.ActionRow({
                    title: 'Error',
                    subtitle: errMsg,
                    subtitle_lines: 0,
                }));
                return;
            }

            setOfflineState(false);
            const payload = resp.data && typeof resp.data === 'object' ? resp.data : {};
            if (payload.title)
                titleRow.set_subtitle(String(payload.title));
            if (payload.description)
                descRow.set_subtitle(String(payload.description));
            if (payload.ok === false) {
                const err = String(payload.error ?? 'Connection failed');
                toolsGroup.set_description('Failed to load tools');
                toolsGroup.add(new Adw.ActionRow({
                    title: 'Connection Failed',
                    subtitle: err,
                    subtitle_lines: 0,
                }));
                return;
            }

            const tools = Array.isArray(payload.tools) ? payload.tools : [];
            toolsGroup.set_description(`${tools.length} tool(s) available`);
            if (tools.length === 0) {
                toolsGroup.add(new Adw.ActionRow({
                    title: 'No tools exposed',
                    subtitle: 'This MCP server did not report any tools.',
                }));
                return;
            }
            for (const tool of tools) {
                const tName = typeof tool === 'object' ? String(tool.name ?? 'unnamed') : String(tool);
                const tDesc = typeof tool === 'object' && tool.description
                    ? String(tool.description)
                    : 'No description provided.';
                toolsGroup.add(new Adw.ActionRow({
                    title: tName,
                    subtitle: tDesc,
                    subtitle_lines: 0,
                    subtitle_selectable: true,
                }));
            }
        };

        const openMcpDialog = (existingName = null) => {
            if (destroyed)
                return;
            const cfg = existingName ? cloneJson(mcpServers[existingName] ?? {}) : {};
            const dialog = new Adw.Dialog({
                title: existingName ? `Edit MCP Server: ${existingName}` : 'Add MCP Server',
                content_width: 520,
                content_height: 600,
            });

            const toolbar = new Adw.ToolbarView();
            const header = new Adw.HeaderBar({
                show_end_title_buttons: false,
                show_start_title_buttons: false,
            });
            const cancelBtn = new Gtk.Button({label: 'Cancel'});
            cancelBtn.connect('clicked', () => dialog.close());
            const saveBtn = new Gtk.Button({
                label: 'Save',
                css_classes: ['suggested-action'],
            });
            header.pack_start(cancelBtn);
            header.pack_end(saveBtn);
            toolbar.add_top_bar(header);

            const dlgPage = new Adw.PreferencesPage();
            const coreGroup = new Adw.PreferencesGroup({title: 'Server Identity & Transport'});

            const nameRow = new Adw.EntryRow({title: 'Server Name'});
            if (existingName)
                nameRow.set_text(existingName);
            coreGroup.add(nameRow);

            const transportRow = makeComboRow('Transport', MCP_TRANSPORTS);
            let transport = String(cfg.transport || (cfg.url ? 'http' : 'stdio')).toLowerCase();
            if (!MCP_TRANSPORTS.includes(transport))
                transport = 'stdio';
            setComboText(transportRow, transport);
            coreGroup.add(transportRow);

            const confirmRow = new Adw.SwitchRow({
                title: 'Require Confirmation',
                subtitle: 'Prompt in island drawer before executing tools on this server',
                active: Boolean(cfg.require_confirmation ?? false),
            });
            coreGroup.add(confirmRow);
            dlgPage.add(coreGroup);

            const stdioGroup = new Adw.PreferencesGroup({title: 'Stdio Process'});
            const commandRow = new Adw.EntryRow({title: 'Command'});
            commandRow.set_text(String(cfg.command ?? ''));
            stdioGroup.add(commandRow);

            const argsRow = new Adw.EntryRow({title: 'Args (JSON array or space-separated)'});
            const rawArgsCfg = cfg.args ?? [];
            if (Array.isArray(rawArgsCfg))
                argsRow.set_text(rawArgsCfg.length > 0 ? JSON.stringify(rawArgsCfg) : '');
            else
                argsRow.set_text(String(rawArgsCfg));
            stdioGroup.add(argsRow);

            const envView = new Gtk.TextView({
                wrap_mode: Gtk.WrapMode.WORD_CHAR,
                css_classes: ['monospace'],
            });
            const envObj = cfg.env && typeof cfg.env === 'object' && !Array.isArray(cfg.env) ? cfg.env : {};
            envView.get_buffer().set_text(
                Object.keys(envObj).length > 0 ? JSON.stringify(envObj, null, 2) : '{}',
                -1
            );
            stdioGroup.add(makeTextViewBox('Environment Variables (JSON object)', envView, 96));
            dlgPage.add(stdioGroup);

            const httpGroup = new Adw.PreferencesGroup({title: 'HTTP / SSE Endpoint'});
            const urlRow = new Adw.EntryRow({title: 'URL'});
            urlRow.set_text(String(cfg.url ?? ''));
            httpGroup.add(urlRow);

            const headersView = new Gtk.TextView({
                wrap_mode: Gtk.WrapMode.WORD_CHAR,
                css_classes: ['monospace'],
            });
            const headersObj = cfg.headers && typeof cfg.headers === 'object' && !Array.isArray(cfg.headers)
                ? cfg.headers
                : {};
            headersView.get_buffer().set_text(
                Object.keys(headersObj).length > 0 ? JSON.stringify(headersObj, null, 2) : '{}',
                -1
            );
            httpGroup.add(makeTextViewBox('HTTP Headers (JSON object)', headersView, 96));
            dlgPage.add(httpGroup);

            const errorLabel = new Gtk.Label({
                xalign: 0,
                wrap: true,
                visible: false,
                css_classes: ['error'],
            });
            const errGroup = new Adw.PreferencesGroup();
            errGroup.add(errorLabel);
            dlgPage.add(errGroup);

            const syncDlgTransport = () => {
                const isStdio = comboText(transportRow) === 'stdio';
                stdioGroup.set_visible(isStdio);
                httpGroup.set_visible(!isStdio);
            };
            transportRow.connect('notify::selected', syncDlgTransport);
            syncDlgTransport();

            const showDlgError = msg => {
                errorLabel.set_text(msg);
                errorLabel.set_visible(true);
            };

            saveBtn.connect('clicked', () => {
                const name = nameRow.get_text().trim();
                if (!name) {
                    showDlgError('Server name is required.');
                    return;
                }
                const nextTransport = comboText(transportRow);
                const nextCfg = {
                    transport: nextTransport,
                    require_confirmation: confirmRow.get_active(),
                };

                if (nextTransport === 'stdio') {
                    const command = commandRow.get_text().trim();
                    if (!command) {
                        showDlgError('Command is required for stdio transport.');
                        return;
                    }
                    nextCfg.command = command;

                    const rawArgs = argsRow.get_text().trim();
                    if (rawArgs.startsWith('[')) {
                        let parsedArgs;
                        try {
                            parsedArgs = JSON.parse(rawArgs);
                        } catch (err) {
                            showDlgError(`Invalid args JSON: ${err.message}`);
                            return;
                        }
                        if (!Array.isArray(parsedArgs)) {
                            showDlgError('Args JSON must be a list.');
                            return;
                        }
                        nextCfg.args = parsedArgs.map(x => String(x));
                    } else if (rawArgs) {
                        nextCfg.args = rawArgs.split(/\s+/).filter(Boolean);
                    } else {
                        nextCfg.args = [];
                    }

                    const rawEnv = readTextBuffer(envView);
                    if (rawEnv) {
                        let parsedEnv;
                        try {
                            parsedEnv = JSON.parse(rawEnv);
                        } catch (err) {
                            showDlgError(`Invalid env JSON: ${err.message}`);
                            return;
                        }
                        if (!parsedEnv || typeof parsedEnv !== 'object' || Array.isArray(parsedEnv)) {
                            showDlgError('Environment variables must be a JSON object.');
                            return;
                        }
                        if (Object.keys(parsedEnv).length > 0) {
                            nextCfg.env = Object.fromEntries(
                                Object.entries(parsedEnv).map(([k, v]) => [String(k), String(v)])
                            );
                        }
                    }
                } else {
                    const url = urlRow.get_text().trim();
                    if (!url) {
                        showDlgError('URL is required for HTTP/SSE transport.');
                        return;
                    }
                    const normalizeUrl = raw => {
                        const trimmed = String(raw || '').trim();
                        if (!trimmed)
                            return '';
                        try {
                            const parsed = new URL(trimmed);
                            const path = parsed.pathname.replace(/\/+$/, '');
                            return `${parsed.protocol.toLowerCase()}//${parsed.host.toLowerCase()}${path}${parsed.search}`;
                        } catch (_e) {
                            return trimmed.replace(/\/+$/, '');
                        }
                    };
                    const targetNorm = normalizeUrl(url);
                    for (const [otherName, otherCfg] of Object.entries(mcpServers)) {
                        if (existingName && otherName === existingName)
                            continue;
                        const otherUrl = normalizeUrl(otherCfg?.url ?? '');
                        if (otherUrl && otherUrl === targetNorm) {
                            showDlgError(`URL matches existing MCP server '${otherName}'. Each server must use a unique URL.`);
                            return;
                        }
                    }
                    nextCfg.url = url;

                    const rawHeaders = readTextBuffer(headersView);
                    if (rawHeaders) {
                        let parsedHeaders;
                        try {
                            parsedHeaders = JSON.parse(rawHeaders);
                        } catch (err) {
                            showDlgError(`Invalid headers JSON: ${err.message}`);
                            return;
                        }
                        if (!parsedHeaders || typeof parsedHeaders !== 'object' || Array.isArray(parsedHeaders)) {
                            showDlgError('Headers must be a JSON object.');
                            return;
                        }
                        if (Object.keys(parsedHeaders).length > 0) {
                            nextCfg.headers = Object.fromEntries(
                                Object.entries(parsedHeaders).map(([k, v]) => [String(k), String(v)])
                            );
                        }
                    }
                }

                const prevDisabled = existingName && mcpServers[existingName]
                    ? Boolean(mcpServers[existingName].disabled)
                    : false;
                if (existingName && existingName !== name)
                    delete mcpServers[existingName];
                nextCfg.disabled = prevDisabled;
                nextCfg.enabled = !prevDisabled;
                mcpServers[name] = nextCfg;
                dialog.close();
                pushMcpChanges(`Saved MCP server '${name}'`).catch(() => {});
            });

            const scrolled = new Gtk.ScrolledWindow({vexpand: true, hexpand: true});
            scrolled.set_child(dlgPage);
            toolbar.set_content(scrolled);
            dialog.set_child(toolbar);
            dialog.present(window);
        };

        let defaultSystemPrompt = '';

        const applyConfig = cfg => {
            loading = true;
            try {
                const general = cfg.general ?? {};
                const visuals = cfg.visuals ?? {};
                const voiceIn = cfg.voice_in ?? cfg.voice_input ?? {};
                const voiceOut = cfg.voice_out ?? cfg.voice_output ?? {};
                const provider = cfg.provider ?? {};
                const llm = cfg.llm ?? {};
                secretsConfigured = cfg.secrets_configured && typeof cfg.secrets_configured === 'object'
                    ? {...cfg.secrets_configured}
                    : {};

                if (typeof cfg.default_system_prompt === 'string' && cfg.default_system_prompt.trim())
                    defaultSystemPrompt = cfg.default_system_prompt;

                petNameRow.set_text(String(general.pet_name ?? cfg.pet_name ?? 'Bytey'));
                wakePhraseRow.set_text(String(general.wake_phrase ?? cfg.wake_phrase ?? 'Hey Bytey'));
                const promptText = String(general.system_prompt ?? cfg.system_prompt ?? defaultSystemPrompt ?? '');
                systemPromptView.get_buffer().set_text(promptText, -1);

                setComboText(animModeRow, visuals.animation_mode ?? 'full');
                cursorTrackRow.set_active(Boolean(visuals.cursor_tracking ?? true));
                for (const [key, {row, btn, defaultHex}] of Object.entries(colorControls)) {
                    const rawVal = visuals[key] ?? defaultHex;
                    const rgba = hexToRgba(String(rawVal), defaultHex);
                    btn.set_rgba(rgba);
                    row.set_subtitle(rgbaToHex(rgba));
                }

                setComboText(densityRow, visuals.chat_density ?? 'comfortable');
                islandWidthRow.set_value(Number(visuals.island_width ?? 140));
                chatWidthRow.set_value(Number(visuals.chat_width ?? 380));
                chatHeightRow.set_value(Number(visuals.chat_height ?? 420));
                chatOpacityRow.set_value(Number(visuals.chat_opacity ?? 96));

                posModeRow.set_selected(positionModeIndexForValue(visuals.position_mode ?? 'fixed'));
                monitorRow.set_value(Number(visuals.monitor_index ?? 0));
                setComboText(fixedAlignRow, visuals.fixed_alignment ?? 'top-center');
                offsetXRow.set_value(Number(visuals.offset_x ?? 0));
                offsetYRow.set_value(Number(visuals.offset_y ?? 12));
                floatXRow.set_value(Number(visuals.float_x ?? 500));
                floatYRow.set_value(Number(visuals.float_y ?? 40));

                micMuteRow.set_active(Boolean(voiceIn.mic_muted ?? false));
                setComboText(sttBackendRow, voiceIn.stt_backend ?? 'local_whisper');
                sttApiUrlRow.set_text(String(voiceIn.stt_api_url ?? ''));
                setSecretRowState(sttApiKeyRow, sttApiKeyBadge, Boolean(secretsConfigured.STT_API_KEY));

                ttsMuteRow.set_active(Boolean(voiceOut.tts_muted ?? false));
                setComboText(ttsProviderRow, voiceOut.tts_provider ?? voiceOut.provider ?? 'kokoro');
                kokoroBaseUrlRow.set_text(String(voiceOut.kokoro_base_url ?? 'http://localhost:8880/v1'));
                setSecretRowState(kokoroApiKeyRow, kokoroApiKeyBadge, Boolean(secretsConfigured.KOKORO_API_KEY));
                setSecretRowState(elevenlabsApiKeyRow, elevenlabsApiKeyBadge, Boolean(secretsConfigured.ELEVENLABS_API_KEY));
                const elVoiceId = String(voiceOut.elevenlabs_voice_id ?? '21m00Tcm4TlvDq8ikWAM').trim();
                const elModelId = String(voiceOut.elevenlabs_model_id ?? 'eleven_flash_v2_5').trim() || 'eleven_flash_v2_5';
                elevenlabsVoiceIdRow.set_text(elVoiceId);
                elevenlabsModelRow.set_text(elModelId);
                rebuildElevenLabsVoiceCombo(elVoiceId);

                const piperModel = String(voiceOut.piper_model ?? DEFAULT_PIPER_MODEL).trim() || DEFAULT_PIPER_MODEL;
                const rawPiperSpeed = Number(voiceOut.piper_speed ?? 1.0);
                const piperSpeed = Number.isFinite(rawPiperSpeed)
                    ? Math.min(2.5, Math.max(0.4, rawPiperSpeed))
                    : 1.0;
                const rawPiperPitch = Number(voiceOut.piper_pitch ?? 0.0);
                const piperPitch = Number.isFinite(rawPiperPitch)
                    ? Math.min(12.0, Math.max(-12.0, rawPiperPitch))
                    : 0.0;
                const rawPiperNoise = Number(voiceOut.piper_noise_scale ?? 0.667);
                const piperNoise = Number.isFinite(rawPiperNoise)
                    ? Math.min(1.5, Math.max(0.0, rawPiperNoise))
                    : 0.667;
                currentPiperModel = piperModel;
                piperCustomModelRow.set_text(piperModel);
                piperSpeedRow.set_value(piperSpeed);
                piperPitchRow.set_value(piperPitch);
                piperNoiseRow.set_value(piperNoise);
                piperPresetRow.set_selected(matchPiperPresetIndex(piperSpeed, piperPitch, piperNoise));

                setComboText(providerRow, provider.active_provider ?? llm.provider ?? 'ollama');
                currentConfiguredModel = String(provider.model ?? llm.model ?? '').trim();
                baseUrlRow.set_text(String(provider.base_url ?? llm.base_url ?? ''));
                detectedToolsSupport = Boolean(llm.supports_tools ?? false);
                forceToolsRow.set_active(Boolean(
                    llm.force_enable_tools ?? (provider.supports_tools_override === true)
                ));
                currentContextSize = normalizePositiveInt(provider.context_size ?? llm.context_size);
                detectedMaxContext = normalizePositiveInt(llm.max_context_size);
                const rawFocusWindow = Number(provider.focus_window ?? llm.focus_window ?? 0);
                focusWindowRow.set_value(
                    Number.isFinite(rawFocusWindow)
                        ? Math.max(0, Math.min(500, Math.round(rawFocusWindow)))
                        : 0
                );
                const rawMaxRounds = Number(provider.max_tool_rounds ?? llm.max_tool_rounds ?? 25);
                maxToolRoundsRow.set_value(
                    Number.isFinite(rawMaxRounds)
                        ? Math.max(1, Math.min(100, Math.round(rawMaxRounds)))
                        : 25
                );
                mcpServers = normalizeMcpMap(cfg.mcpServers ?? cfg.mcp_servers ?? {});

                const skillsCfg = cfg.skills && typeof cfg.skills === 'object' ? cfg.skills : {};
                skillsEnabledRow.set_active(Boolean(skillsCfg.enabled ?? true));
                skillsAllowScriptsRow.set_active(Boolean(skillsCfg.allow_script_execution ?? false));
                configuredSkillsDir = String(skillsCfg.skills_dir ?? '').trim();
                skillsDirRow.set_subtitle(
                    configuredSkillsDir || 'Default (~/.config/bytey-ai-companion/skills)'
                );
                const extraDirs = Array.isArray(skillsCfg.extra_dirs) ? skillsCfg.extra_dirs : [];
                skillsExtraDirsRow.set_text(
                    extraDirs.map(d => String(d).trim()).filter(Boolean).join(', ')
                );

                const wsCfg = cfg.workspace && typeof cfg.workspace === 'object' ? cfg.workspace : {};
                workspaceAutoSkillsRow.set_active(Boolean(wsCfg.auto_load_skills ?? true));
                workspaceAutoRulesRow.set_active(Boolean(wsCfg.auto_load_rules ?? true));
                workspaceWriteApprovalRow.set_active(Boolean(wsCfg.require_write_approval ?? true));
                workspaceCmdApprovalRow.set_active(Boolean(wsCfg.require_command_approval ?? true));
                syncWorkspaceControls(
                    String(wsCfg.active_path ?? '').trim(),
                    Array.isArray(wsCfg.recent_paths) ? wsCfg.recent_paths : []
                );

                const agentsCfg = cfg.agents && typeof cfg.agents === 'object' ? cfg.agents : {};
                godModeEnabledRow.set_active(Boolean(agentsCfg.god_mode_enabled ?? false));
                godModeApprovalRow.set_active(Boolean(agentsCfg.require_run_approval ?? true));
                godModeTimeoutRow.set_value(Number(agentsCfg.timeout_seconds ?? 300));
            } finally {
                loading = false;
            }
            syncPlacementSensitivity();
            syncSttVisibility();
            syncTtsVisibility();
            syncProviderVisibility();
            rebuildPiperModelCombo();
            syncMcpJsonText();
            rebuildMcpList();
            updateToolsSensitivity();
            refreshContextSizeRow();
            if (comboText(ttsProviderRow) === 'piper')
                fetchPiperVoices({refresh: false, showResultToast: false}).catch(() => {});
            fetchModels(comboText(providerRow), baseUrlRow.get_text().trim(), {
                autoSelectIfMissing: false,
                showResultToast: false,
            }).catch(() => {});
        };

        const loadConfig = async () => {
            if (destroyed)
                return;
            statusRow.set_subtitle('Checking...');
            const resp = await httpJson('GET', '/api/v1/config');
            if (destroyed)
                return;
            if (resp.ok && resp.data && typeof resp.data === 'object') {
                setOfflineState(false);
                applyConfig(resp.data);
                loadSkills().catch(() => {});
                loadAgents().catch(() => {});
            } else {
                setOfflineState(true);
            }
        };

        const checkHealth = async () => {
            if (destroyed)
                return;
            statusRow.set_subtitle('Checking...');
            const resp = await httpJson('GET', '/api/v1/health');
            if (destroyed)
                return;
            if (resp.ok) {
                setOfflineState(false);
                showToast('Connected to Bytey daemon');
            } else {
                setOfflineState(true);
                showToast('Bytey daemon is offline');
            }
        };

        const patchConfig = async (patch, toastMsg = 'Saved') => {
            if (destroyed || loading)
                return;
            const resp = await httpJson('PATCH', '/api/v1/config', patch);
            if (destroyed)
                return;
            if (resp.ok) {
                setOfflineState(false);
                const llmData = resp.data?.llm;
                if (typeof llmData?.supports_tools === 'boolean' && !forceToolsRow.get_active()) {
                    detectedToolsSupport = llmData.supports_tools;
                    updateToolsSensitivity();
                }
                if (llmData && typeof llmData === 'object' && Object.prototype.hasOwnProperty.call(llmData, 'max_context_size')) {
                    detectedMaxContext = normalizePositiveInt(llmData.max_context_size);
                    if (currentConfiguredModel && detectedMaxContext !== null)
                        modelContextMap.set(currentConfiguredModel, detectedMaxContext);
                    const ctxClamped = refreshContextSizeRow({clampAndReturnChanged: true});
                    if (
                        ctxClamped &&
                        (!patch?.provider || !Object.prototype.hasOwnProperty.call(patch.provider, 'context_size'))
                    ) {
                        httpJson('PATCH', '/api/v1/config', {provider: {context_size: currentContextSize}}).catch(() => {});
                    }
                }
                showToast(toastMsg);
            } else {
                setOfflineState(true);
                showToast('Daemon offline — changes not saved');
            }
        };

        const saveSecret = async (secretKey, row, badge) => {
            if (destroyed || loading)
                return false;
            const value = row.get_text().trim();
            if (!value) {
                showToast('Enter an API key before applying');
                return false;
            }
            if (isMaskedPlaceholder(value)) {
                showToast(`${secretKey} already configured — enter a new key to replace`);
                return false;
            }
            const resp = await httpJson('PUT', '/api/v1/secrets', {secrets: {[secretKey]: value}});
            if (destroyed)
                return false;
            if (!resp.ok) {
                setOfflineState(true);
                showToast(`Failed to save ${secretKey}`);
                return false;
            }
            setOfflineState(false);
            if (resp.data?.secrets_configured && typeof resp.data.secrets_configured === 'object')
                secretsConfigured = {...resp.data.secrets_configured};
            else
                secretsConfigured = {...secretsConfigured, [secretKey]: true};
            loading = true;
            try {
                setSecretRowState(row, badge, true);
            } finally {
                loading = false;
            }
            showToast(`Saved ${secretKey}`);
            return true;
        };

        const onUserChange = handler => {
            if (destroyed || loading)
                return;
            handler();
        };

        offlineBanner.connect('button-clicked', () => {
            loadConfig();
        });

        testBtn.connect('clicked', () => {
            checkHealth();
        });

        daemonUrlRow.connect('apply', () => onUserChange(() => {
            const validUrl = normalizeUrl(daemonUrlRow.get_text());
            if (!validUrl) {
                showToast('Invalid Daemon URL — expected http:// or https://');
                loading = true;
                try {
                    daemonUrlRow.set_text(getDaemonUrl());
                } finally {
                    loading = false;
                }
                return;
            }
            const prevUrl = settings.get_string('daemon-url');
            loading = true;
            try {
                daemonUrlRow.set_text(validUrl);
            } finally {
                loading = false;
            }
            settings.set_string('daemon-url', validUrl);
            showToast('Saved Daemon URL');
            if (prevUrl === validUrl)
                loadConfig();
        }));

        settingsChangedId = settings.connect('changed::daemon-url', () => {
            if (destroyed)
                return;
            loading = true;
            try {
                daemonUrlRow.set_text(getDaemonUrl());
            } finally {
                loading = false;
            }
            loadConfig();
        });

        petNameRow.connect('apply', () => onUserChange(() => {
            const petName = petNameRow.get_text().trim();
            if (petName)
                patchConfig({general: {pet_name: petName}}, 'Saved companion name');
        }));

        wakePhraseRow.connect('apply', () => onUserChange(() => {
            const wakePhrase = wakePhraseRow.get_text().trim();
            if (wakePhrase)
                patchConfig({general: {wake_phrase: wakePhrase}}, 'Saved wake phrase');
        }));

        resetSystemPromptBtn.connect('clicked', () => onUserChange(() => {
            if (!defaultSystemPrompt) {
                showToast('Default prompt unavailable — check daemon connection');
                return;
            }
            systemPromptView.get_buffer().set_text(defaultSystemPrompt, -1);
            showToast('System prompt reset (click Save Prompt to apply)');
        }));

        saveSystemPromptBtn.connect('clicked', () => onUserChange(() => {
            const buf = systemPromptView.get_buffer();
            const [start, end] = buf.get_bounds();
            const prompt = buf.get_text(start, end, true).trim();
            const finalPrompt = prompt || defaultSystemPrompt;
            if (!finalPrompt) {
                showToast('System prompt cannot be empty');
                return;
            }
            patchConfig({general: {system_prompt: finalPrompt}}, 'Saved system prompt');
        }));

        const buildExportPayload = rawCfg => {
            const exportKeys = ['general', 'provider', 'voice_in', 'voice_out', 'visuals', 'mcp_servers'];
            const out = {};
            for (const key of exportKeys) {
                if (rawCfg && Object.prototype.hasOwnProperty.call(rawCfg, key))
                    out[key] = rawCfg[key];
            }
            return out;
        };

        const confirmReplaceImport = parsedConfig => {
            const runImport = async () => {
                const resp = await httpJson('PUT', '/api/v1/config', parsedConfig);
                if (destroyed)
                    return;
                if (resp.ok && resp.data && typeof resp.data === 'object') {
                    setOfflineState(false);
                    applyConfig(resp.data);
                    showToast('Configuration imported and replaced');
                } else {
                    const errMsg = resp.data?.detail || resp.data?.error || 'Failed to import configuration';
                    showToast(errMsg);
                }
            };

            if (typeof Adw.AlertDialog === 'function') {
                const dialog = new Adw.AlertDialog({
                    heading: 'Replace All Configuration?',
                    body: 'Importing this JSON file will replace all existing Bytey configurations. This cannot be undone.',
                });
                dialog.add_response('cancel', 'Cancel');
                dialog.add_response('replace', 'Replace All');
                dialog.set_response_appearance('replace', Adw.ResponseAppearance.DESTRUCTIVE);
                dialog.set_default_response('cancel');
                dialog.set_close_response('cancel');
                dialog.connect('response', (_dlg, response) => {
                    if (response === 'replace')
                        runImport();
                });
                dialog.present(window);
            } else {
                const dialog = new Adw.MessageDialog({
                    transient_for: window,
                    modal: true,
                    heading: 'Replace All Configuration?',
                    body: 'Importing this JSON file will replace all existing Bytey configurations. This cannot be undone.',
                });
                dialog.add_response('cancel', 'Cancel');
                dialog.add_response('replace', 'Replace All');
                dialog.set_response_appearance('replace', Adw.ResponseAppearance.DESTRUCTIVE);
                dialog.set_default_response('cancel');
                dialog.connect('response', (_dlg, response) => {
                    if (response === 'replace')
                        runImport();
                });
                dialog.present();
            }
        };

        exportBtn.connect('clicked', async () => {
            if (destroyed)
                return;
            const resp = await httpJson('GET', '/api/v1/config');
            if (destroyed)
                return;
            if (!resp.ok || !resp.data || typeof resp.data !== 'object') {
                setOfflineState(true);
                showToast('Daemon offline — cannot export configuration');
                return;
            }
            const payload = JSON.stringify(buildExportPayload(resp.data), null, 2) + '\n';
            const fileDialog = new Gtk.FileDialog({
                title: 'Export Bytey Configuration',
                initial_name: 'bytey-config.json',
            });
            const filter = new Gtk.FileFilter();
            filter.set_name('JSON files');
            filter.add_mime_type('application/json');
            filter.add_pattern('*.json');
            const filters = new Gio.ListStore({item_type: Gtk.FileFilter});
            filters.append(filter);
            fileDialog.set_filters(filters);

            fileDialog.save(window, null, (_dlg, result) => {
                try {
                    const file = fileDialog.save_finish(result);
                    if (!file)
                        return;
                    const bytes = new GLib.Bytes(new TextEncoder().encode(payload));
                    file.replace_contents_bytes_async(
                        bytes,
                        null,
                        false,
                        Gio.FileCreateFlags.REPLACE_DESTINATION,
                        null,
                        (source, res) => {
                            try {
                                source.replace_contents_finish(res);
                                showToast(`Exported to ${file.get_basename()}`);
                            } catch (e) {
                                showToast(`Export failed: ${e.message}`);
                            }
                        }
                    );
                } catch (e) {
                    if (!(e instanceof GLib.Error && e.matches(Gtk.DialogError, Gtk.DialogError.DISMISSED)))
                        showToast(`Export failed: ${e.message}`);
                }
            });
        });

        importBtn.connect('clicked', () => {
            if (destroyed)
                return;
            const fileDialog = new Gtk.FileDialog({
                title: 'Import Bytey Configuration',
            });
            const filter = new Gtk.FileFilter();
            filter.set_name('JSON files');
            filter.add_mime_type('application/json');
            filter.add_pattern('*.json');
            const filters = new Gio.ListStore({item_type: Gtk.FileFilter});
            filters.append(filter);
            fileDialog.set_filters(filters);

            fileDialog.open(window, null, (_dlg, result) => {
                try {
                    const file = fileDialog.open_finish(result);
                    if (!file)
                        return;
                    file.load_contents_async(null, (source, res) => {
                        try {
                            const [ok, contents] = source.load_contents_finish(res);
                            if (!ok) {
                                showToast('Failed to read selected file');
                                return;
                            }
                            const text = new TextDecoder().decode(contents);
                            const parsed = JSON.parse(text);
                            if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
                                showToast('Invalid JSON: expected a configuration object');
                                return;
                            }
                            confirmReplaceImport(parsed);
                        } catch (e) {
                            showToast(`Import failed: ${e.message}`);
                        }
                    });
                } catch (e) {
                    if (!(e instanceof GLib.Error && e.matches(Gtk.DialogError, Gtk.DialogError.DISMISSED)))
                        showToast(`Import failed: ${e.message}`);
                }
            });
        });

        animModeRow.connect('notify::selected', () => onUserChange(() => {
            patchConfig({visuals: {animation_mode: comboText(animModeRow)}}, 'Saved animation mode');
        }));

        cursorTrackRow.connect('notify::active', () => onUserChange(() => {
            patchConfig({visuals: {cursor_tracking: cursorTrackRow.get_active()}}, 'Saved cursor tracking');
        }));

        resetColorsBtn.connect('clicked', () => onUserChange(() => {
            loading = true;
            try {
                for (const {row, btn, defaultHex} of Object.values(colorControls)) {
                    btn.set_rgba(hexToRgba(defaultHex, defaultHex));
                    row.set_subtitle(defaultHex);
                }
            } finally {
                loading = false;
            }
            patchConfig(
                {
                    visuals: {
                        eye_color: DEFAULT_EYE_COLOR,
                        background_color: DEFAULT_BG_COLOR,
                        border_color: DEFAULT_BORDER_COLOR,
                    },
                },
                'Reset colors to defaults'
            );
        }));

        densityRow.connect('notify::selected', () => onUserChange(() => {
            patchConfig({visuals: {chat_density: comboText(densityRow)}}, 'Saved chat density');
        }));

        islandWidthRow.connect('notify::value', () => onUserChange(() => {
            patchConfig({visuals: {island_width: Math.round(islandWidthRow.get_value())}}, 'Saved island width');
        }));

        chatWidthRow.connect('notify::value', () => onUserChange(() => {
            patchConfig({visuals: {chat_width: Math.round(chatWidthRow.get_value())}}, 'Saved chat width');
        }));

        chatHeightRow.connect('notify::value', () => onUserChange(() => {
            patchConfig({visuals: {chat_height: Math.round(chatHeightRow.get_value())}}, 'Saved chat height');
        }));

        chatOpacityRow.connect('notify::value', () => onUserChange(() => {
            patchConfig({visuals: {chat_opacity: Math.round(chatOpacityRow.get_value())}}, 'Saved chat opacity');
        }));

        previewAnimBtn.connect('clicked', async () => {
            const state = comboText(testStateRow) || 'IDLE';
            const emotion = comboText(testEmotionRow) || 'neutral';
            try {
                await httpJson('POST', '/api/v1/visuals/preview', {state, emotion});
                showToast(`Previewing ${state} (${emotion})`);
            } catch (err) {
                showToast(`Preview failed: ${err.message}`, true);
            }
        });

        resetAnimBtn.connect('clicked', async () => {
            try {
                await httpJson('POST', '/api/v1/visuals/preview', {reset: true});
                showToast('Reset animation to idle');
            } catch (err) {
                showToast(`Reset failed: ${err.message}`, true);
            }
        });

        posModeRow.connect('notify::selected', () => onUserChange(() => {
            syncPlacementSensitivity();
            const mode = positionModeValueForIndex(posModeRow.get_selected());
            patchConfig({visuals: {position_mode: mode}}, 'Saved mode');
        }));

        monitorRow.connect('notify::value', () => onUserChange(() => {
            patchConfig({visuals: {monitor_index: Math.round(monitorRow.get_value())}}, 'Saved monitor index');
        }));

        fixedAlignRow.connect('notify::selected', () => onUserChange(() => {
            patchConfig({visuals: {fixed_alignment: comboText(fixedAlignRow)}}, 'Saved fixed alignment');
        }));

        offsetXRow.connect('notify::value', () => onUserChange(() => {
            patchConfig({visuals: {offset_x: Math.round(offsetXRow.get_value())}}, 'Saved offset X');
        }));

        offsetYRow.connect('notify::value', () => onUserChange(() => {
            patchConfig({visuals: {offset_y: Math.round(offsetYRow.get_value())}}, 'Saved offset Y');
        }));

        floatXRow.connect('notify::value', () => onUserChange(() => {
            patchConfig({visuals: {float_x: Math.round(floatXRow.get_value())}}, 'Saved floating X');
        }));

        floatYRow.connect('notify::value', () => onUserChange(() => {
            patchConfig({visuals: {float_y: Math.round(floatYRow.get_value())}}, 'Saved floating Y');
        }));

        micMuteRow.connect('notify::active', () => onUserChange(() => {
            patchConfig({voice_in: {mic_muted: micMuteRow.get_active()}}, 'Updated microphone mute');
        }));

        sttBackendRow.connect('notify::selected', () => onUserChange(() => {
            syncSttVisibility();
            patchConfig({voice_in: {stt_backend: comboText(sttBackendRow)}}, 'Saved STT backend');
        }));

        sttApiUrlRow.connect('apply', () => onUserChange(() => {
            patchConfig({voice_in: {stt_api_url: sttApiUrlRow.get_text().trim()}}, 'Saved STT API URL');
        }));

        sttApiKeyRow.connect('apply', () => onUserChange(() => {
            saveSecret('STT_API_KEY', sttApiKeyRow, sttApiKeyBadge).catch(() => {});
        }));

        ttsMuteRow.connect('notify::active', () => onUserChange(() => {
            patchConfig({voice_out: {tts_muted: ttsMuteRow.get_active()}}, 'Updated voice mute');
        }));

        ttsProviderRow.connect('notify::selected', () => onUserChange(() => {
            const nextProvider = comboText(ttsProviderRow);
            syncTtsVisibility();
            patchConfig({voice_out: {tts_provider: nextProvider}}, 'Saved TTS provider');
            if (nextProvider === 'piper')
                fetchPiperVoices({refresh: false, showResultToast: false}).catch(() => {});
        }));

        kokoroBaseUrlRow.connect('apply', () => onUserChange(() => {
            patchConfig({voice_out: {kokoro_base_url: kokoroBaseUrlRow.get_text().trim()}}, 'Saved Kokoro server URL');
        }));

        kokoroApiKeyRow.connect('apply', () => onUserChange(() => {
            saveSecret('KOKORO_API_KEY', kokoroApiKeyRow, kokoroApiKeyBadge).catch(() => {});
        }));

        let elevenlabsVoices = [...DEFAULT_ELEVENLABS_VOICES];

        const rebuildElevenLabsVoiceCombo = (selectedId = '') => {
            const targetId = String(selectedId || elevenlabsVoiceIdRow.get_text() || '').trim();
            const prevLoading = loading;
            loading = true;
            try {
                const labels = elevenlabsVoices.map(v => `${v.name} — ${v.id}`);
                elevenlabsVoiceRow.set_model(Gtk.StringList.new(labels.length > 0 ? labels : ['No voices available']));
                const idx = targetId ? elevenlabsVoices.findIndex(v => v.id === targetId) : -1;
                if (idx >= 0)
                    elevenlabsVoiceRow.set_selected(idx);
            } finally {
                loading = prevLoading;
            }
        };

        const refreshElevenLabsVoices = async ({silent = false} = {}) => {
            if (destroyed)
                return;
            const resp = await httpJson('GET', '/api/v1/tts/voices?provider=elevenlabs');
            if (destroyed)
                return;
            if (!resp.ok || !resp.data || !Array.isArray(resp.data.voices)) {
                if (!silent)
                    showToast(resp.data?.detail || resp.data?.error || 'Failed to fetch ElevenLabs voices');
                return;
            }
            const parsed = resp.data.voices
                .filter(v => v && typeof v === 'object' && String(v.id || v.voice_id || '').trim())
                .map(v => {
                    const id = String(v.id || v.voice_id).trim();
                    const rawName = String(v.name || id).trim();
                    const category = String(v.category || '').trim();
                    const name = category && !rawName.includes(`(${category})`) ? `${rawName} (${category})` : rawName;
                    return {id, name};
                });
            if (parsed.length > 0)
                elevenlabsVoices = parsed;
            rebuildElevenLabsVoiceCombo();
            if (!silent)
                showToast(`Loaded ${parsed.length} ElevenLabs voices`);
        };

        elevenlabsApiKeyRow.connect('apply', () => onUserChange(() => {
            saveSecret('ELEVENLABS_API_KEY', elevenlabsApiKeyRow, elevenlabsApiKeyBadge)
                .then(saved => {
                    if (saved)
                        refreshElevenLabsVoices({silent: true}).catch(() => {});
                })
                .catch(() => {});
        }));

        elevenlabsRefreshBtn.connect('clicked', () => {
            refreshElevenLabsVoices({silent: false}).catch(() => {});
        });

        elevenlabsVoiceRow.connect('notify::selected', () => onUserChange(() => {
            const idx = elevenlabsVoiceRow.get_selected();
            const chosen = elevenlabsVoices[idx];
            if (!chosen)
                return;
            const prevLoading = loading;
            loading = true;
            try {
                elevenlabsVoiceIdRow.set_text(chosen.id);
            } finally {
                loading = prevLoading;
            }
            patchConfig({voice_out: {elevenlabs_voice_id: chosen.id}}, `Saved ElevenLabs voice: ${chosen.name}`);
        }));

        elevenlabsVoiceIdRow.connect('apply', () => onUserChange(() => {
            const voiceId = elevenlabsVoiceIdRow.get_text().trim();
            if (!voiceId) {
                showToast('Voice ID cannot be empty');
                return;
            }
            rebuildElevenLabsVoiceCombo(voiceId);
            patchConfig({voice_out: {elevenlabs_voice_id: voiceId}}, `Saved ElevenLabs voice ID: ${voiceId}`);
        }));

        elevenlabsModelRow.connect('apply', () => onUserChange(() => {
            const modelId = elevenlabsModelRow.get_text().trim();
            if (!modelId) {
                showToast('Model ID cannot be empty');
                return;
            }
            patchConfig({voice_out: {elevenlabs_model_id: modelId}}, `Saved ElevenLabs model: ${modelId}`);
        }));

        piperLangRow.connect('notify::selected', () => onUserChange(() => {
            const idx = piperLangRow.get_selected();
            const opt = piperLanguageOptions[idx];
            currentPiperLanguage = opt ? opt.code : '';
            rebuildPiperModelCombo();
            const matchIdx = currentPiperModel
                ? piperFilteredVoices.findIndex(v => v.id === currentPiperModel)
                : -1;
            if (matchIdx < 0 && piperFilteredVoices.length > 0 && piperFilteredVoices[0].installed) {
                const chosen = piperFilteredVoices[0];
                currentPiperModel = chosen.id;
                loading = true;
                try {
                    piperCustomModelRow.set_text(chosen.id);
                } finally {
                    loading = false;
                }
                patchConfig({voice_out: {piper_model: chosen.id}}, `Saved Piper voice: ${chosen.id}`);
            }
        }));

        piperModelRow.connect('notify::selected', () => onUserChange(() => {
            if (piperFilteredVoices.length === 0) {
                syncPiperDownloadButton();
                return;
            }
            const idx = piperModelRow.get_selected();
            const chosen = idx >= 0 && idx < piperFilteredVoices.length ? piperFilteredVoices[idx] : null;
            syncPiperDownloadButton();
            if (!chosen || !chosen.id || chosen.id === currentPiperModel)
                return;
            currentPiperModel = chosen.id;
            loading = true;
            try {
                piperCustomModelRow.set_text(chosen.id);
            } finally {
                loading = false;
            }
            patchConfig({voice_out: {piper_model: chosen.id}}, `Saved Piper voice: ${chosen.id}`);
        }));

        piperRefreshBtn.connect('clicked', () => onUserChange(() => {
            fetchPiperVoices({refresh: true, showResultToast: true}).catch(() => {});
        }));

        piperDownloadBtn.connect('clicked', () => onUserChange(async () => {
            if (piperDownloading)
                return;
            const idx = piperModelRow.get_selected();
            const selectedVoice = idx >= 0 && idx < piperFilteredVoices.length
                ? piperFilteredVoices[idx]
                : null;
            const selectedId = selectedVoice
                ? selectedVoice.id
                : (piperCustomModelRow.get_text().trim() || currentPiperModel);
            if (!selectedId) {
                showToast('Select a Piper voice model first');
                return;
            }

            piperDownloading = true;
            syncPiperDownloadButton();
            showToast(`Downloading Piper voice ${selectedId}...`);

            const resp = await httpJson(
                'POST',
                '/api/v1/tts/piper/download',
                {model: selectedId},
                {longTimeout: true}
            );
            piperDownloading = false;
            if (destroyed)
                return;

            if (!resp.ok || resp.data?.ok === false) {
                if (resp.status === 0)
                    setOfflineState(true);
                const errMsg = resp.data?.error
                    ? String(resp.data.error)
                    : `HTTP ${resp.status || 'offline'}`;
                syncPiperDownloadButton();
                showToast(`Piper download failed: ${errMsg}`);
                return;
            }
            setOfflineState(false);

            const existing = piperVoices.find(v => v.id === selectedId);
            if (existing) {
                existing.installed = true;
            } else {
                piperVoices.push({
                    id: selectedId,
                    languageCode: inferPiperLanguageCode(selectedId),
                    languageName: '',
                    quality: '',
                    installed: true,
                });
                rebuildPiperLanguageFilter();
            }

            currentPiperModel = selectedId;
            loading = true;
            try {
                piperCustomModelRow.set_text(selectedId);
            } finally {
                loading = false;
            }
            rebuildPiperModelCombo();
            await patchConfig({voice_out: {piper_model: selectedId}}, `Downloaded Piper voice ${selectedId}`);
        }));

        piperCustomModelRow.connect('apply', () => onUserChange(() => {
            const modelVal = piperCustomModelRow.get_text().trim();
            if (!modelVal) {
                showToast('Enter a Piper model ID or .onnx path');
                loading = true;
                try {
                    piperCustomModelRow.set_text(currentPiperModel || DEFAULT_PIPER_MODEL);
                } finally {
                    loading = false;
                }
                return;
            }
            currentPiperModel = modelVal;
            const inCatalog = piperVoices.find(v => v.id === modelVal);
            if (inCatalog && currentPiperLanguage && inCatalog.languageCode !== currentPiperLanguage) {
                currentPiperLanguage = '';
                const prevLoading = loading;
                loading = true;
                try {
                    piperLangRow.set_selected(0);
                } finally {
                    loading = prevLoading;
                }
            }
            rebuildPiperModelCombo();
            patchConfig({voice_out: {piper_model: modelVal}}, `Saved Piper model: ${modelVal}`);
        }));

        piperPresetRow.connect('notify::selected', () => onUserChange(() => {
            const idx = piperPresetRow.get_selected();
            const preset = PIPER_VOICE_PRESETS[idx];
            if (!preset || preset.id === 'custom' || preset.speed === null)
                return;
            loading = true;
            try {
                piperSpeedRow.set_value(preset.speed);
                piperPitchRow.set_value(preset.pitch);
                piperNoiseRow.set_value(preset.noise_scale);
            } finally {
                loading = false;
            }
            patchConfig(
                {
                    voice_out: {
                        piper_speed: preset.speed,
                        piper_pitch: preset.pitch,
                        piper_noise_scale: preset.noise_scale,
                    },
                },
                `Applied voice preset: ${preset.label}`
            );
        }));

        piperSpeedRow.connect('notify::value', () => onUserChange(() => {
            syncPiperPresetFromControls();
            const val = Number(piperSpeedRow.get_value().toFixed(2));
            patchConfig({voice_out: {piper_speed: val}}, 'Saved Piper speed');
        }));

        piperPitchRow.connect('notify::value', () => onUserChange(() => {
            syncPiperPresetFromControls();
            const val = Number(piperPitchRow.get_value().toFixed(1));
            patchConfig({voice_out: {piper_pitch: val}}, 'Saved Piper pitch');
        }));

        piperNoiseRow.connect('notify::value', () => onUserChange(() => {
            syncPiperPresetFromControls();
            const val = Number(piperNoiseRow.get_value().toFixed(3));
            patchConfig({voice_out: {piper_noise_scale: val}}, 'Saved Piper expressiveness');
        }));

        piperTestBtn.connect('clicked', () => onUserChange(async () => {
            if (ttsMuteRow.get_active()) {
                showToast('Voice output (TTS) is muted — unmute to preview');
                return;
            }
            const activeModel = piperCustomModelRow.get_text().trim() || currentPiperModel;
            const voiceObj = piperVoices.find(v => v.id === activeModel);
            const langCode = (voiceObj && voiceObj.languageCode)
                || inferPiperLanguageCode(activeModel)
                || currentPiperLanguage
                || 'en_US';
            const text = sampleTextForLanguage(langCode);

            piperTestBtn.set_sensitive(false);
            const resp = await httpJson('POST', '/api/v1/tts/replay', {text}, {longTimeout: true});
            if (destroyed)
                return;
            piperTestBtn.set_sensitive(true);
            if (!resp.ok || resp.data?.ok === false) {
                if (resp.status === 0)
                    setOfflineState(true);
                const errMsg = resp.data?.error
                    ? String(resp.data.error)
                    : `HTTP ${resp.status || 'offline'}`;
                showToast(`Voice preview failed: ${errMsg}`);
                return;
            }
            setOfflineState(false);
            showToast('Playing Piper voice preview');
        }));

        providerRow.connect('notify::selected', () => onUserChange(async () => {
            const prov = comboText(providerRow);
            const defaultUrl = PROVIDER_DEFAULT_URLS[prov] ?? '';
            modelToolMap.clear();
            modelContextMap.clear();
            detectedMaxContext = null;
            loading = true;
            try {
                baseUrlRow.set_text(defaultUrl);
                syncProviderVisibility();
            } finally {
                loading = false;
            }
            refreshContextSizeRow();
            await patchConfig({provider: {active_provider: prov, base_url: defaultUrl}}, 'Saved AI provider');
            if (!destroyed && comboText(providerRow) === prov)
                await fetchModels(prov, defaultUrl, {autoSelectIfMissing: true});
        }));

        baseUrlRow.connect('apply', () => onUserChange(async () => {
            const prov = comboText(providerRow);
            const nextUrl = baseUrlRow.get_text().trim();
            await patchConfig({provider: {base_url: nextUrl}}, 'Saved base URL');
            if (!destroyed && comboText(providerRow) === prov)
                await fetchModels(prov, nextUrl, {autoSelectIfMissing: false});
        }));

        aiApiKeyRow.connect('apply', () => onUserChange(async () => {
            const prov = comboText(providerRow);
            const secretKey = PROVIDER_SECRET_MAP[prov];
            if (!secretKey)
                return;
            const saved = await saveSecret(secretKey, aiApiKeyRow, aiApiKeyBadge);
            if (saved && !destroyed && comboText(providerRow) === prov)
                await fetchModels(prov, baseUrlRow.get_text().trim(), {autoSelectIfMissing: true});
        }));

        refreshModelsBtn.connect('clicked', () => onUserChange(() => {
            fetchModels(comboText(providerRow), baseUrlRow.get_text().trim(), {
                autoSelectIfMissing: false,
                showResultToast: true,
            }).catch(() => {});
        }));

        modelRow.connect('notify::selected', () => onUserChange(() => {
            if (availableModels.length === 0)
                return;
            const idx = modelRow.get_selected();
            if (idx < 0 || idx >= availableModels.length)
                return;
            const chosen = availableModels[idx];
            if (!chosen || !chosen.id || chosen.id === currentConfiguredModel)
                return;
            currentConfiguredModel = chosen.id;
            updateToolsSensitivity();
            const ctxClamped = refreshContextSizeRow({clampAndReturnChanged: true});
            const patch = {provider: {model: chosen.id}};
            if (ctxClamped)
                patch.provider.context_size = currentContextSize;
            if (typeof chosen.supports_tools === 'boolean')
                patch.supports_tools = chosen.supports_tools;
            patchConfig(patch, `Saved model: ${chosen.id}`);
        }));

        contextSizeRow.connect('notify::selected', () => onUserChange(() => {
            const idx = contextSizeRow.get_selected();
            const opt = contextPresetOptions[idx];
            const nextVal = opt ? opt.value : null;
            if (nextVal === currentContextSize)
                return;
            currentContextSize = nextVal;
            patchConfig(
                {provider: {context_size: currentContextSize}},
                currentContextSize ? `Saved context size: ${opt.label}` : 'Context size set to Auto'
            );
        }));

        focusWindowRow.connect('notify::value', () => onUserChange(() => {
            const windowSize = Math.max(0, Math.min(500, Math.round(focusWindowRow.get_value())));
            patchConfig(
                {provider: {focus_window: windowSize}},
                windowSize === 0 ? 'Focus window set to full history' : `Saved focus window: ${windowSize}`
            );
        }));

        maxToolRoundsRow.connect('notify::value', () => onUserChange(() => {
            const rounds = Math.max(1, Math.min(100, Math.round(maxToolRoundsRow.get_value())));
            patchConfig(
                {provider: {max_tool_rounds: rounds}},
                `Saved max tool loops: ${rounds}`
            );
        }));

        forceToolsRow.connect('notify::active', () => onUserChange(() => {
            updateToolsSensitivity();
            patchConfig(
                {provider: {supports_tools_override: forceToolsRow.get_active() ? true : null}},
                'Updated tool override'
            );
        }));

        addMcpBtn.connect('clicked', () => onUserChange(() => {
            openMcpDialog(null);
        }));

        applyMcpJsonBtn.connect('clicked', () => onUserChange(() => {
            applyRawMcpJson().catch(() => {});
        }));

        // Skills helpers & signal handlers
        const resolveEffectiveSkillsDir = () => {
            if (configuredSkillsDir) {
                if (configuredSkillsDir.startsWith('~/'))
                    return GLib.build_filenamev([GLib.get_home_dir(), configuredSkillsDir.slice(2)]);
                return configuredSkillsDir;
            }
            return GLib.build_filenamev([GLib.get_user_config_dir(), 'bytey-ai-companion', 'skills']);
        };

        const rebuildSkillsList = () => {
            for (const widget of skillRows)
                skillsListGroup.remove(widget);
            skillRows = [];

            if (!Array.isArray(skillsList) || skillsList.length === 0) {
                const emptyRow = new Adw.ActionRow({
                    title: 'No skills detected',
                    subtitle: "Click '+ Add Skill' or 'Import' to add a skill.",
                });
                skillsListGroup.add(emptyRow);
                skillRows.push(emptyRow);
                return;
            }

            const sourceTags = {user: '[User]', extra: '[Extra]', builtin: '[Built-in]'};
            const sorted = [...skillsList].sort((a, b) =>
                String(a?.name ?? '').localeCompare(String(b?.name ?? ''))
            );

            for (const skill of sorted) {
                const name = String(skill?.name ?? '');
                const source = String(skill?.source ?? 'user');
                const sourceTag = sourceTags[source] ?? `[${source}]`;
                const valid = Boolean(skill?.valid ?? true);
                const invalidTag = valid ? '' : ' ⚠ Invalid';
                const editable = Boolean(skill?.editable ?? source === 'user');
                const row = new Adw.ActionRow({
                    title: `${name} ${sourceTag}${invalidTag}`,
                    subtitle: String(skill?.error || skill?.description || ''),
                });

                const enabledSwitch = new Gtk.Switch({
                    active: Boolean(skill?.enabled ?? true),
                    valign: Gtk.Align.CENTER,
                    tooltip_text: 'Enable or disable skill',
                });
                enabledSwitch.connect('notify::active', () => onUserChange(() => {
                    const active = enabledSwitch.get_active();
                    httpJson('PATCH', `/api/v1/skills/${encodeURIComponent(name)}`, {enabled: active})
                        .then(resp => {
                            if (destroyed)
                                return;
                            if (!resp.ok) {
                                showToast(`Failed to update ${name}`);
                                return;
                            }
                            skill.enabled = active;
                            showToast(`${active ? 'Enabled' : 'Disabled'} skill '${name}'`);
                        })
                        .catch(() => {});
                }));
                row.add_suffix(enabledSwitch);

                const editBtn = new Gtk.Button({
                    icon_name: editable ? 'document-edit-symbolic' : 'dialog-information-symbolic',
                    valign: Gtk.Align.CENTER,
                    tooltip_text: editable ? 'Edit SKILL.md' : 'Inspect SKILL.md (read-only)',
                    css_classes: ['flat'],
                });
                editBtn.connect('clicked', () => openSkillEditor(skill));
                row.add_suffix(editBtn);

                if (editable) {
                    const delBtn = new Gtk.Button({
                        icon_name: 'user-trash-symbolic',
                        valign: Gtk.Align.CENTER,
                        tooltip_text: 'Delete skill',
                        css_classes: ['flat', 'destructive-action'],
                    });
                    delBtn.connect('clicked', () => confirmDeleteSkill(name));
                    row.add_suffix(delBtn);
                }

                skillsListGroup.add(row);
                skillRows.push(row);
            }
        };

        let agentRows = [];
        const rebuildAgentsList = agents => {
            for (const row of agentRows)
                agentsGroup.remove(row);
            agentRows = [];

            const list = Array.isArray(agents) ? agents : [];
            if (list.length === 0) {
                const emptyRow = new Adw.ActionRow({
                    title: 'No CLI agents detected yet',
                    subtitle: 'Click Detect CLIs to scan your system PATH.',
                });
                agentsGroup.add(emptyRow);
                agentRows.push(emptyRow);
                return;
            }

            const permModes = ['default', 'accept_edits', 'autonomous'];
            for (const agent of list) {
                const agentId = String(agent.id ?? '');
                const installed = Boolean(agent.installed);
                const enabled = Boolean(agent.enabled && installed);

                if (!installed) {
                    const row = new Adw.ActionRow({
                        title: String(agent.display_name || agentId),
                        subtitle: `Not Found (${agent.binary || agentId})`,
                    });
                    const sw = new Gtk.Switch({
                        active: false,
                        sensitive: false,
                        valign: Gtk.Align.CENTER,
                    });
                    row.add_suffix(sw);
                    agentsGroup.add(row);
                    agentRows.push(row);
                    continue;
                }

                const verPart = agent.version ? `${agent.version} • ` : '';
                const row = new Adw.ExpanderRow({
                    title: String(agent.display_name || agentId),
                    subtitle: `Installed • ${verPart}${agent.resolved_path || agent.binary}`,
                });

                const sw = new Gtk.Switch({
                    active: enabled,
                    valign: Gtk.Align.CENTER,
                });
                sw.connect('notify::active', () => {
                    if (loading)
                        return;
                    httpJson('PATCH', `/api/v1/agents/${encodeURIComponent(agentId)}`, {
                        enabled: sw.get_active(),
                    }).then(r => {
                        if (r.ok && r.data?.agents)
                            rebuildAgentsList(r.data.agents);
                        else if (!r.ok)
                            showToast(r.error || 'Failed to update agent');
                    }).catch(() => {});
                });
                row.add_suffix(sw);

                const presets = Array.isArray(agent.default_models) && agent.default_models.length > 0
                    ? `Presets: ${agent.default_models.join(', ')}`
                    : 'Leave empty to use CLI default model';
                const modelRow = new Adw.EntryRow({
                    title: `Model (${agent.model_flag || '--model'})`,
                    text: String(agent.model || ''),
                });
                modelRow.set_tooltip_text(presets);
                const saveModelBtn = new Gtk.Button({
                    label: 'Apply',
                    valign: Gtk.Align.CENTER,
                });
                saveModelBtn.connect('clicked', () => {
                    httpJson('PATCH', `/api/v1/agents/${encodeURIComponent(agentId)}`, {
                        model: modelRow.get_text().trim(),
                    }).then(r => {
                        if (r.ok)
                            showToast(`${agent.display_name || agentId} model saved`);
                        else
                            showToast(r.error || 'Failed to save model');
                    }).catch(() => {});
                });
                modelRow.add_suffix(saveModelBtn);
                row.add_row(modelRow);

                const permRow = makeSearchableComboRow({
                    title: 'Permission Mode',
                    subtitle: presets,
                    model: new Gtk.StringList({strings: permModes}),
                });
                const curPermIdx = Math.max(0, permModes.indexOf(String(agent.permission_mode || 'accept_edits')));
                permRow.set_selected(curPermIdx);
                permRow.connect('notify::selected', () => {
                    const sel = permModes[permRow.get_selected()] || 'accept_edits';
                    httpJson('PATCH', `/api/v1/agents/${encodeURIComponent(agentId)}`, {
                        permission_mode: sel,
                    }).catch(() => {});
                });
                row.add_row(permRow);

                const autoFlagsRow = new Adw.SwitchRow({
                    title: 'Allow Autonomous Flags',
                    subtitle: 'Opt-in for --dangerously-skip-permissions / --allow-all-tools',
                    active: Boolean(agent.allow_autonomous_flags),
                });
                autoFlagsRow.connect('notify::active', () => {
                    httpJson('PATCH', `/api/v1/agents/${encodeURIComponent(agentId)}`, {
                        allow_autonomous_flags: autoFlagsRow.get_active(),
                    }).catch(() => {});
                });
                row.add_row(autoFlagsRow);

                agentsGroup.add(row);
                agentRows.push(row);
            }
        };

        const loadAgents = async () => {
            const resp = await httpJson('GET', '/api/v1/agents');
            if (destroyed)
                return;
            if (resp.ok && resp.data)
                rebuildAgentsList(resp.data.agents || []);
        };

        detectAgentsBtn.connect('clicked', () => {
            httpJson('POST', '/api/v1/agents/detect', {}).then(r => {
                if (r.ok && r.data) {
                    rebuildAgentsList(r.data.agents || []);
                    showToast('CLI agents scanned');
                } else {
                    showToast(r.error || 'Failed to detect CLI agents');
                }
            }).catch(() => {});
        });

        godModeEnabledRow.connect('notify::active', () => {
            if (!loading) {
                patchConfig({agents: {god_mode_enabled: godModeEnabledRow.get_active()}}, 'Saved')
                    .catch(() => {});
            }
        });
        godModeApprovalRow.connect('notify::active', () => {
            if (!loading) {
                patchConfig({agents: {require_run_approval: godModeApprovalRow.get_active()}}, 'Saved')
                    .catch(() => {});
            }
        });
        godModeTimeoutRow.connect('notify::value', () => {
            if (!loading) {
                patchConfig({agents: {timeout_seconds: Math.round(godModeTimeoutRow.get_value())}}, 'Saved')
                    .catch(() => {});
            }
        });

        const loadSkills = async () => {
            if (destroyed)
                return;
            const resp = await httpJson('GET', '/api/v1/skills');
            if (destroyed || !resp.ok || !resp.data || typeof resp.data !== 'object')
                return;
            skillsList = Array.isArray(resp.data.skills) ? resp.data.skills : [];
            rebuildSkillsList();
        };

        const rescanSkills = async (showResultToast = false) => {
            if (destroyed)
                return;
            const resp = await httpJson('POST', '/api/v1/skills/rescan', {});
            if (destroyed)
                return;
            if (!resp.ok) {
                showToast('Failed to rescan skills');
                return;
            }
            if (resp.data && Array.isArray(resp.data.skills))
                skillsList = resp.data.skills;
            rebuildSkillsList();
            if (showResultToast)
                showToast(`Rescanned skills (${skillsList.length} found)`);
        };

        const openSkillEditor = skill => {
            if (destroyed)
                return;
            const name = String(skill?.name ?? '');
            const source = String(skill?.source ?? 'user');
            const editable = Boolean(skill?.editable ?? source === 'user');
            const dialog = new Adw.Dialog({
                title: editable ? `Edit Skill: ${name}` : `Inspect Skill: ${name} (Read-only)`,
                content_width: 600,
                content_height: 560,
            });
            const toolbar = new Adw.ToolbarView();
            const header = new Adw.HeaderBar({
                show_start_title_buttons: false,
                show_end_title_buttons: false,
            });
            const closeBtn = new Gtk.Button({label: editable ? 'Cancel' : 'Close'});
            closeBtn.connect('clicked', () => dialog.close());
            header.pack_start(closeBtn);

            let saveBtn = null;
            if (editable) {
                saveBtn = new Gtk.Button({
                    label: 'Save',
                    css_classes: ['suggested-action'],
                });
                header.pack_end(saveBtn);
            }
            toolbar.add_top_bar(header);

            const textView = new Gtk.TextView({
                wrap_mode: Gtk.WrapMode.WORD_CHAR,
                editable,
                top_margin: 10,
                bottom_margin: 10,
                left_margin: 12,
                right_margin: 12,
                css_classes: ['monospace'],
            });
            const rawContent = String(skill?.raw_content ?? '');
            textView.get_buffer().set_text(rawContent, -1);

            const scrolled = new Gtk.ScrolledWindow({hexpand: true, vexpand: true});
            scrolled.set_child(textView);
            toolbar.set_content(scrolled);
            dialog.set_child(toolbar);
            dialog.present(window);

            if (!rawContent) {
                httpJson('GET', `/api/v1/skills/${encodeURIComponent(name)}`)
                    .then(resp => {
                        if (!destroyed && resp.ok && resp.data?.raw_content)
                            textView.get_buffer().set_text(String(resp.data.raw_content), -1);
                    })
                    .catch(() => {});
            }

            if (editable && saveBtn) {
                saveBtn.connect('clicked', async () => {
                    const buf = textView.get_buffer();
                    const content = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), true);
                    saveBtn.set_sensitive(false);
                    const resp = await httpJson(
                        'PUT',
                        `/api/v1/skills/${encodeURIComponent(name)}`,
                        {content}
                    );
                    if (destroyed)
                        return;
                    saveBtn.set_sensitive(true);
                    if (!resp.ok) {
                        const err = resp.data?.detail || resp.data?.error || 'Save failed';
                        showToast(String(err));
                        return;
                    }
                    dialog.close();
                    showToast(`Saved skill '${name}'`);
                    loadSkills().catch(() => {});
                });
            }
        };

        const confirmDeleteSkill = name => {
            const doDelete = async () => {
                const resp = await httpJson('DELETE', `/api/v1/skills/${encodeURIComponent(name)}`);
                if (destroyed)
                    return;
                if (!resp.ok) {
                    const err = resp.data?.detail || resp.data?.error || 'Delete failed';
                    showToast(String(err));
                    return;
                }
                showToast(`Deleted skill '${name}'`);
                loadSkills().catch(() => {});
            };

            if (typeof Adw.AlertDialog === 'function') {
                const dlg = new Adw.AlertDialog({
                    heading: `Delete Skill '${name}'?`,
                    body: 'This permanently removes the user skill directory and its files.',
                });
                dlg.add_response('cancel', 'Cancel');
                dlg.add_response('delete', 'Delete');
                dlg.set_response_appearance('delete', Adw.ResponseAppearance.DESTRUCTIVE);
                dlg.set_default_response('cancel');
                dlg.set_close_response('cancel');
                dlg.connect('response', (_d, response) => {
                    if (response === 'delete')
                        doDelete().catch(() => {});
                });
                dlg.present(window);
            } else {
                const dlg = new Adw.MessageDialog({
                    transient_for: window,
                    modal: true,
                    heading: `Delete Skill '${name}'?`,
                    body: 'This permanently removes the user skill directory and its files.',
                });
                dlg.add_response('cancel', 'Cancel');
                dlg.add_response('delete', 'Delete');
                dlg.set_response_appearance('delete', Adw.ResponseAppearance.DESTRUCTIVE);
                dlg.set_default_response('cancel');
                dlg.connect('response', (_d, response) => {
                    if (response === 'delete')
                        doDelete().catch(() => {});
                });
                dlg.present();
            }
        };

        skillsEnabledRow.connect('notify::active', () => onUserChange(() => {
            patchConfig({skills: {enabled: skillsEnabledRow.get_active()}}, 'Saved');
        }));

        skillsAllowScriptsRow.connect('notify::active', () => onUserChange(() => {
            patchConfig(
                {skills: {allow_script_execution: skillsAllowScriptsRow.get_active()}},
                'Saved'
            );
        }));

        skillsBrowseBtn.connect('clicked', () => {
            if (destroyed)
                return;
            const fileDialog = new Gtk.FileDialog({title: 'Select Skills Storage Folder'});
            fileDialog.select_folder(window, null, (_dlg, result) => {
                try {
                    const folder = fileDialog.select_folder_finish(result);
                    const path = folder?.get_path();
                    if (!path)
                        return;
                    configuredSkillsDir = path;
                    skillsDirRow.set_subtitle(path);
                    patchConfig({skills: {skills_dir: path}}, 'Skills folder updated')
                        .then(() => rescanSkills(false))
                        .catch(() => {});
                } catch (e) {
                    if (!(e instanceof GLib.Error && e.matches(Gtk.DialogError, Gtk.DialogError.DISMISSED)))
                        showToast(`Folder selection failed: ${e.message}`);
                }
            });
        });

        skillsResetBtn.connect('clicked', () => {
            configuredSkillsDir = '';
            skillsDirRow.set_subtitle('Default (~/.config/bytey-ai-companion/skills)');
            patchConfig({skills: {skills_dir: ''}}, 'Skills folder reset to default')
                .then(() => rescanSkills(false))
                .catch(() => {});
        });

        workspaceChooseBtn.connect('clicked', () => {
            if (destroyed)
                return;
            const fileDialog = new Gtk.FileDialog({title: 'Select Work Folder'});
            fileDialog.select_folder(window, null, (_dlg, result) => {
                try {
                    const folder = fileDialog.select_folder_finish(result);
                    const path = folder?.get_path();
                    if (path)
                        setWorkspacePath(path).catch(() => {});
                } catch (e) {
                    if (!(e instanceof GLib.Error && e.matches(Gtk.DialogError, Gtk.DialogError.DISMISSED)))
                        showToast(`Folder selection failed: ${e.message}`);
                }
            });
        });

        workspaceClearBtn.connect('clicked', () => {
            setWorkspacePath('').catch(() => {});
        });

        workspaceRecentRow.connect('notify::selected', () => {
            if (loading || workspaceUpdating)
                return;
            const idx = workspaceRecentRow.get_selected();
            if (idx === Gtk.INVALID_LIST_POSITION)
                return;
            if (idx === 0)
                setWorkspacePath('').catch(() => {});
            else if (idx >= 1 && idx <= workspaceRecentPaths.length)
                setWorkspacePath(workspaceRecentPaths[idx - 1]).catch(() => {});
        });

        workspaceAutoSkillsRow.connect('notify::active', () => onUserChange(() => {
            patchConfig(
                {workspace: {auto_load_skills: workspaceAutoSkillsRow.get_active()}},
                'Saved'
            );
        }));

        workspaceAutoRulesRow.connect('notify::active', () => onUserChange(() => {
            patchConfig(
                {workspace: {auto_load_rules: workspaceAutoRulesRow.get_active()}},
                'Saved'
            );
        }));

        workspaceWriteApprovalRow.connect('notify::active', () => onUserChange(() => {
            patchConfig(
                {workspace: {require_write_approval: workspaceWriteApprovalRow.get_active()}},
                'Saved'
            );
        }));

        workspaceCmdApprovalRow.connect('notify::active', () => onUserChange(() => {
            patchConfig(
                {workspace: {require_command_approval: workspaceCmdApprovalRow.get_active()}},
                'Saved'
            );
        }));

        skillsOpenBtn.connect('clicked', () => {
            const targetDir = resolveEffectiveSkillsDir();
            try {
                const dirFile = Gio.File.new_for_path(targetDir);
                try {
                    dirFile.make_directory_with_parents(null);
                } catch (mkErr) {
                    if (!(mkErr instanceof GLib.Error && mkErr.matches(Gio.IOErrorEnum, Gio.IOErrorEnum.EXISTS)))
                        throw mkErr;
                }
                Gio.AppInfo.launch_default_for_uri(dirFile.get_uri(), null);
            } catch (e) {
                showToast(`Could not open folder: ${e.message}`);
            }
        });

        skillsExtraDirsRow.connect('apply', () => onUserChange(() => {
            const dirs = skillsExtraDirsRow.get_text()
                .split(',')
                .map(s => s.trim())
                .filter(Boolean);
            patchConfig({skills: {extra_dirs: dirs}}, 'Saved extra skill directories')
                .then(() => rescanSkills(false))
                .catch(() => {});
        }));

        skillsRescanBtn.connect('clicked', () => {
            rescanSkills(true).catch(() => {});
        });

        addSkillBtn.connect('clicked', () => {
            if (destroyed)
                return;
            const dialog = new Adw.Dialog({
                title: 'Add Skill',
                content_width: 520,
                content_height: 540,
            });
            const toolbar = new Adw.ToolbarView();
            const header = new Adw.HeaderBar({
                show_start_title_buttons: false,
                show_end_title_buttons: false,
            });
            const cancelBtn = new Gtk.Button({label: 'Cancel'});
            cancelBtn.connect('clicked', () => dialog.close());
            const createBtn = new Gtk.Button({
                label: 'Create',
                css_classes: ['suggested-action'],
            });
            header.pack_start(cancelBtn);
            header.pack_end(createBtn);
            toolbar.add_top_bar(header);

            const page = new Adw.PreferencesPage();
            const metaGroup = new Adw.PreferencesGroup({title: 'Skill Metadata'});
            const nameRow = new Adw.EntryRow({title: 'Name (slug, e.g. my-skill)'});
            const descRow = new Adw.EntryRow({title: 'Description'});
            const slashRow = new Adw.EntryRow({title: 'Slash Command (optional, e.g. /myskill)'});
            metaGroup.add(nameRow);
            metaGroup.add(descRow);
            metaGroup.add(slashRow);
            page.add(metaGroup);

            const bodyGroup = new Adw.PreferencesGroup({title: 'Instructions (Markdown)'});
            const bodyView = new Gtk.TextView({
                wrap_mode: Gtk.WrapMode.WORD_CHAR,
                top_margin: 8,
                bottom_margin: 8,
                left_margin: 10,
                right_margin: 10,
            });
            const scrolled = new Gtk.ScrolledWindow({
                min_content_height: 180,
                hexpand: true,
                vexpand: true,
            });
            scrolled.set_child(bodyView);
            const bodyRow = new Adw.PreferencesRow();
            bodyRow.set_child(scrolled);
            bodyGroup.add(bodyRow);
            page.add(bodyGroup);

            createBtn.connect('clicked', async () => {
                const name = nameRow.get_text().trim();
                const description = descRow.get_text().trim();
                const slashCommand = slashRow.get_text().trim();
                const buf = bodyView.get_buffer();
                const body = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), true);
                if (!name || !description) {
                    showToast('Name and description are required');
                    return;
                }
                const payload = {name, description, body};
                if (slashCommand)
                    payload.slash_command = slashCommand;
                createBtn.set_sensitive(false);
                const resp = await httpJson('POST', '/api/v1/skills', payload);
                if (destroyed)
                    return;
                createBtn.set_sensitive(true);
                if (!resp.ok) {
                    const err = resp.data?.detail || resp.data?.error || 'Create failed';
                    showToast(String(err));
                    return;
                }
                dialog.close();
                showToast(`Created skill '${name}'`);
                loadSkills().catch(() => {});
            });

            toolbar.set_content(page);
            dialog.set_child(toolbar);
            dialog.present(window);
        });

        importSkillBtn.connect('clicked', () => {
            if (destroyed)
                return;
            const doImportPath = async path => {
                const resp = await httpJson('POST', '/api/v1/skills/import', {path});
                if (destroyed)
                    return;
                if (!resp.ok) {
                    const err = resp.data?.detail || resp.data?.error || 'Import failed';
                    showToast(String(err));
                    return;
                }
                showToast('Skill imported');
                loadSkills().catch(() => {});
            };

            const pickFolder = () => {
                const fileDialog = new Gtk.FileDialog({title: 'Select Skill Folder'});
                fileDialog.select_folder(window, null, (_dlg, result) => {
                    try {
                        const folder = fileDialog.select_folder_finish(result);
                        const path = folder?.get_path();
                        if (path)
                            doImportPath(path).catch(() => {});
                    } catch (e) {
                        if (!(e instanceof GLib.Error && e.matches(Gtk.DialogError, Gtk.DialogError.DISMISSED)))
                            showToast(`Import failed: ${e.message}`);
                    }
                });
            };

            const pickZip = () => {
                const fileDialog = new Gtk.FileDialog({title: 'Select Skill .zip Archive'});
                const filter = new Gtk.FileFilter();
                filter.set_name('ZIP archives');
                filter.add_mime_type('application/zip');
                filter.add_pattern('*.zip');
                const filters = new Gio.ListStore({item_type: Gtk.FileFilter});
                filters.append(filter);
                fileDialog.set_filters(filters);
                fileDialog.open(window, null, (_dlg, result) => {
                    try {
                        const file = fileDialog.open_finish(result);
                        const path = file?.get_path();
                        if (path)
                            doImportPath(path).catch(() => {});
                    } catch (e) {
                        if (!(e instanceof GLib.Error && e.matches(Gtk.DialogError, Gtk.DialogError.DISMISSED)))
                            showToast(`Import failed: ${e.message}`);
                    }
                });
            };

            if (typeof Adw.AlertDialog === 'function') {
                const dlg = new Adw.AlertDialog({
                    heading: 'Import Skill',
                    body: 'Choose whether to import a skill folder or a .zip archive.',
                });
                dlg.add_response('cancel', 'Cancel');
                dlg.add_response('folder', 'Folder');
                dlg.add_response('zip', '.zip Archive');
                dlg.set_default_response('folder');
                dlg.set_close_response('cancel');
                dlg.connect('response', (_d, response) => {
                    if (response === 'folder')
                        pickFolder();
                    else if (response === 'zip')
                        pickZip();
                });
                dlg.present(window);
            } else {
                pickFolder();
            }
        });

        rebuildSkillsList();

        syncPlacementSensitivity();
        syncSttVisibility();
        syncTtsVisibility();
        syncProviderVisibility();
        syncPiperDownloadButton();
        syncMcpJsonText();
        rebuildMcpList();
        updateToolsSensitivity();
        refreshContextSizeRow();
        loadConfig();
    }
}
