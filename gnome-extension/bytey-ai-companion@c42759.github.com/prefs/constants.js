// SPDX-License-Identifier: GPL-3.0-or-later

import Gdk from 'gi://Gdk';

import {parseHexColor} from '../colorUtils.js';

export const DEFAULT_DAEMON_URL = 'http://127.0.0.1:8765';
export const DEFAULT_EYE_COLOR = '#F7FAFF';
export const DEFAULT_BG_COLOR = '#0A0A0C';
export const DEFAULT_BORDER_COLOR = '#2A2C34';
export const ANIMATION_MODES = ['full', 'state_only', 'disabled'];
export const CHAT_DENSITIES = ['compact', 'comfortable', 'spacious'];
export const POSITION_MODE_OPTIONS = [
    {label: 'Fixed', value: 'fixed'},
    {label: 'Floating', value: 'floating'},
    {label: 'Notch', value: 'notch'},
];
export const FIXED_ALIGNMENTS = [
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
export const STT_BACKENDS = ['local_whisper', 'openai_whisper', 'custom_whisper'];
export const TTS_PROVIDERS = ['kokoro', 'piper', 'elevenlabs', 'disabled'];
export const DEFAULT_PIPER_MODEL = 'en_US-lessac-medium';
export const DEFAULT_ELEVENLABS_VOICES = [
    {id: 'JBFqnCBsd6RMkjVDRZzb', name: 'George (premade)'},
    {id: '21m00Tcm4TlvDq8ikWAM', name: 'Rachel (premade)'},
    {id: 'pNInz6obpgDQGcFmaJgB', name: 'Adam (premade)'},
];
export const PIPER_VOICE_PRESETS = [
    {id: 'normal', label: 'Normal', speed: 1.0, pitch: 0.0, noise_scale: 0.667},
    {id: 'chipmunk', label: 'Chipmunk', speed: 1.25, pitch: 8.0, noise_scale: 0.8},
    {id: 'helium', label: 'Helium Squeak', speed: 1.15, pitch: 10.5, noise_scale: 0.9},
    {id: 'deep_monster', label: 'Deep Monster', speed: 0.82, pitch: -8.0, noise_scale: 0.667},
    {id: 'sleepy_giant', label: 'Sleepy Giant', speed: 0.68, pitch: -5.5, noise_scale: 0.55},
    {id: 'hyper_gremlin', label: 'Hyper Gremlin', speed: 1.55, pitch: 5.5, noise_scale: 1.0},
    {id: 'flat_robot', label: 'Flat Robot', speed: 1.05, pitch: -2.5, noise_scale: 0.1},
    {id: 'custom', label: 'Custom', speed: null, pitch: null, noise_scale: null},
];
export const AI_PROVIDERS = ['ollama', 'gemini', 'openai', 'anthropic', 'custom'];
export const TEST_ANIMATION_STATES = [
    'IDLE',
    'LISTENING',
    'THINKING',
    'TOOL_CALLING',
    'SPEAKING',
    'MUTED',
    'ERROR',
];
export const TEST_ANIMATION_EMOTIONS = [
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
export const MCP_TRANSPORTS = ['stdio', 'http', 'sse'];
export const SECRET_MASK_PLACEHOLDER = '••••••••••••••••';
export const PROVIDER_SECRET_MAP = {
    gemini: 'GEMINI_API_KEY',
    openai: 'OPENAI_API_KEY',
    anthropic: 'ANTHROPIC_API_KEY',
    custom: 'CUSTOM_LLM_API_KEY',
};
export const PROVIDER_DEFAULT_URLS = {
    ollama: 'http://localhost:11434',
    gemini: '',
    openai: 'https://api.openai.com/v1',
    anthropic: 'https://api.anthropic.com',
    custom: 'http://localhost:8000/v1',
};
export const DEFAULT_CONTEXT_FALLBACK_MAX = 131072;

export function normalizeUrl(raw) {
    if (typeof raw !== 'string')
        return null;
    const trimmed = raw.trim();
    if (!/^https?:\/\/\S+$/i.test(trimmed))
        return null;
    const stripped = trimmed.replace(/\/+$/, '');
    return /^https?:\/\/\S+$/i.test(stripped) ? stripped : null;
}

export function positionModeValueForIndex(idx) {
    return (POSITION_MODE_OPTIONS[idx] ?? POSITION_MODE_OPTIONS[0]).value;
}

export function positionModeIndexForValue(mode) {
    const normalized = String(mode ?? 'fixed').toLowerCase().trim();
    const idx = POSITION_MODE_OPTIONS.findIndex(o => o.value === normalized);
    return idx >= 0 ? idx : 0;
}

export function matchPiperPresetIndex(speed, pitch, noiseScale) {
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

export function sampleTextForLanguage(langCode) {
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

export function inferPiperLanguageCode(modelId) {
    const match = String(modelId ?? '').trim().match(/^([a-z]{2,3}(?:[_-][A-Za-z0-9]+)?)-/);
    return match ? match[1] : '';
}

export function formatPiperLanguageLabel(code, rawName = '') {
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

export function normalizePiperVoiceEntry(entry) {
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

export function hexToRgba(hexStr, fallback = '#FFFFFFFF') {
    const fb = parseHexColor(fallback, [1.0, 1.0, 1.0, 1.0]);
    const [red, green, blue, alpha] = parseHexColor(hexStr, fb);
    return new Gdk.RGBA({red, green, blue, alpha});
}

export function rgbaToHex(rgba) {
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

export function isMaskedPlaceholder(value) {
    const cleaned = String(value ?? '').trim();
    return cleaned.length > 0 && (cleaned === SECRET_MASK_PLACEHOLDER || /^[•]+$/.test(cleaned));
}

export function cloneJson(obj) {
    return JSON.parse(JSON.stringify(obj ?? {}));
}

export function formatNumberCommas(n) {
    return String(n).replace(/\B(?=(\d{3})+(?!\d))/g, ',');
}

export function formatContextTokens(tokens) {
    if (tokens >= 1048576 && tokens % 1048576 === 0)
        return `${tokens / 1048576}M (${formatNumberCommas(tokens)})`;
    if (tokens >= 1024 && tokens % 1024 === 0)
        return `${tokens / 1024}K (${formatNumberCommas(tokens)})`;
    return formatNumberCommas(tokens);
}

export function formatContextShort(tokens) {
    if (tokens >= 1048576 && tokens % 1048576 === 0)
        return `${tokens / 1048576}M`;
    if (tokens >= 1024 && tokens % 1024 === 0)
        return `${tokens / 1024}K`;
    return formatNumberCommas(tokens);
}

export function normalizePositiveInt(value) {
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

export function buildContextPresets(maxContext, currentValue = null) {
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

export function clampContextValue(value, presets) {
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
