// SPDX-License-Identifier: GPL-3.0-or-later

import Adw from 'gi://Adw';
import Gtk from 'gi://Gtk';

import {
    DEFAULT_ELEVENLABS_VOICES,
    DEFAULT_PIPER_MODEL,
    PIPER_VOICE_PRESETS,
    STT_BACKENDS,
    TTS_PROVIDERS,
    formatPiperLanguageLabel,
    inferPiperLanguageCode,
    matchPiperPresetIndex,
    normalizePiperVoiceEntry,
    sampleTextForLanguage,
} from './constants.js';
import {
    comboText,
    makeComboRow,
    makeFloatSpinRow,
    makeSearchableComboRow,
    makeSecretRow,
    setComboText,
    setSecretRowState,
} from './widgets.js';

export function buildVoicePage(ctx) {
    const page = new Adw.PreferencesPage({
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
    page.add(sttGroup);

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
    page.add(ttsGroup);

    let piperVoices = [];
    let piperFilteredVoices = [];
    let piperLanguageOptions = [{code: '', label: 'All Languages'}];
    let currentPiperLanguage = '';
    let currentPiperModel = DEFAULT_PIPER_MODEL;
    let piperFetchSeq = 0;
    let piperDownloading = false;
    let elevenlabsVoices = [...DEFAULT_ELEVENLABS_VOICES];

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

        ctx.withLoading(() => {
            piperLangRow.set_model(Gtk.StringList.new(piperLanguageOptions.map(o => o.label)));
            piperLangRow.set_selected(selIdx);
        });
    };

    const rebuildPiperModelCombo = () => {
        if (piperVoices.length === 0) {
            piperFilteredVoices = [];
            const fallbackLabel = currentPiperModel || DEFAULT_PIPER_MODEL;
            ctx.withLoading(() => {
                piperModelRow.set_model(Gtk.StringList.new([fallbackLabel]));
                piperModelRow.set_selected(0);
                piperModelRow.set_sensitive(true);
                piperModelRow.set_subtitle('Local CPU neural voice model');
            });
            syncPiperDownloadButton();
            return;
        }

        piperFilteredVoices = currentPiperLanguage
            ? piperVoices.filter(v => v.languageCode === currentPiperLanguage)
            : [...piperVoices];

        if (piperFilteredVoices.length === 0) {
            ctx.withLoading(() => {
                piperModelRow.set_model(Gtk.StringList.new(['No voices for selected language']));
                piperModelRow.set_selected(0);
                piperModelRow.set_sensitive(false);
                piperModelRow.set_subtitle('0 voice(s) match language filter');
            });
            syncPiperDownloadButton();
            return;
        }

        const labels = piperFilteredVoices.map(v => `${v.id}${v.installed ? ' ✓' : ''}`);
        const matchIdx = currentPiperModel
            ? piperFilteredVoices.findIndex(v => v.id === currentPiperModel)
            : -1;
        const targetIdx = matchIdx >= 0 ? matchIdx : 0;

        ctx.withLoading(() => {
            piperModelRow.set_model(Gtk.StringList.new(labels));
            piperModelRow.set_selected(targetIdx);
            piperModelRow.set_sensitive(true);
        });
        syncPiperDownloadButton();
    };

    const fetchPiperVoices = async ({refresh = false, showResultToast = false} = {}) => {
        if (ctx.cancellable.is_cancelled())
            return;
        const seq = ++piperFetchSeq;
        piperModelRow.set_subtitle('Loading Piper voice catalog...');
        piperRefreshBtn.set_sensitive(false);

        const queryPath = `/api/v1/tts/voices?provider=piper${refresh ? '&refresh=1' : ''}`;
        let resp = await ctx.httpJson('GET', queryPath, undefined, {longTimeout: refresh});
        if (ctx.cancellable.is_cancelled() || seq !== piperFetchSeq)
            return;

        if (resp.status !== 0 && (!resp.ok || (Array.isArray(resp.data?.voices) && resp.data.voices.length === 0))) {
            const altPath = `/api/v1/tts/piper/voices${refresh ? '?refresh=1' : ''}`;
            const altResp = await ctx.httpJson('GET', altPath, undefined, {longTimeout: refresh});
            if (ctx.cancellable.is_cancelled() || seq !== piperFetchSeq)
                return;
            if (altResp.ok)
                resp = altResp;
        }

        piperRefreshBtn.set_sensitive(true);

        if (!resp.ok || !resp.data) {
            if (resp.status === 0)
                ctx.setOfflineState(true);
            piperModelRow.set_subtitle('Unable to load Piper voice catalog');
            syncPiperDownloadButton();
            if (showResultToast)
                ctx.showToast('Unable to fetch Piper voice catalog');
            return;
        }
        ctx.setOfflineState(false);

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
            ctx.showToast(
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
        ctx.withLoading(() => piperPresetRow.set_selected(matchedIdx));
    };

    const rebuildElevenLabsVoiceCombo = (selectedId = '') => {
        const targetId = String(selectedId || elevenlabsVoiceIdRow.get_text() || '').trim();
        ctx.withLoading(() => {
            const labels = elevenlabsVoices.map(v => `${v.name} — ${v.id}`);
            elevenlabsVoiceRow.set_model(Gtk.StringList.new(labels.length > 0 ? labels : ['No voices available']));
            const idx = targetId ? elevenlabsVoices.findIndex(v => v.id === targetId) : -1;
            if (idx >= 0)
                elevenlabsVoiceRow.set_selected(idx);
        });
    };

    const refreshElevenLabsVoices = async ({silent = false} = {}) => {
        if (ctx.cancellable.is_cancelled())
            return;
        const resp = await ctx.httpJson('GET', '/api/v1/tts/voices?provider=elevenlabs');
        if (ctx.cancellable.is_cancelled())
            return;
        if (!resp.ok || !resp.data || !Array.isArray(resp.data.voices)) {
            if (!silent)
                ctx.showToast(resp.data?.detail || resp.data?.error || 'Failed to fetch ElevenLabs voices');
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
            ctx.showToast(`Loaded ${parsed.length} ElevenLabs voices`);
    };

    micMuteRow.connect('notify::active', () => ctx.onUserChange(() => {
        ctx.patchConfig({voice_in: {mic_muted: micMuteRow.get_active()}}, 'Updated microphone mute');
    }));

    sttBackendRow.connect('notify::selected', () => ctx.onUserChange(() => {
        syncSttVisibility();
        ctx.patchConfig({voice_in: {stt_backend: comboText(sttBackendRow)}}, 'Saved STT backend');
    }));

    sttApiUrlRow.connect('apply', () => ctx.onUserChange(() => {
        ctx.patchConfig({voice_in: {stt_api_url: sttApiUrlRow.get_text().trim()}}, 'Saved STT API URL');
    }));

    sttApiKeyRow.connect('apply', () => ctx.onUserChange(() => {
        ctx.saveSecret('STT_API_KEY', sttApiKeyRow, sttApiKeyBadge).catch(() => {});
    }));

    ttsMuteRow.connect('notify::active', () => ctx.onUserChange(() => {
        ctx.patchConfig({voice_out: {tts_muted: ttsMuteRow.get_active()}}, 'Updated voice mute');
    }));

    ttsProviderRow.connect('notify::selected', () => ctx.onUserChange(() => {
        const nextProvider = comboText(ttsProviderRow);
        syncTtsVisibility();
        ctx.patchConfig({voice_out: {tts_provider: nextProvider}}, 'Saved TTS provider');
        if (nextProvider === 'piper')
            fetchPiperVoices({refresh: false, showResultToast: false}).catch(() => {});
    }));

    kokoroBaseUrlRow.connect('apply', () => ctx.onUserChange(() => {
        ctx.patchConfig({voice_out: {kokoro_base_url: kokoroBaseUrlRow.get_text().trim()}}, 'Saved Kokoro server URL');
    }));

    kokoroApiKeyRow.connect('apply', () => ctx.onUserChange(() => {
        ctx.saveSecret('KOKORO_API_KEY', kokoroApiKeyRow, kokoroApiKeyBadge).catch(() => {});
    }));

    elevenlabsApiKeyRow.connect('apply', () => ctx.onUserChange(() => {
        ctx.saveSecret('ELEVENLABS_API_KEY', elevenlabsApiKeyRow, elevenlabsApiKeyBadge)
            .then(saved => {
                if (saved)
                    refreshElevenLabsVoices({silent: true}).catch(() => {});
            })
            .catch(() => {});
    }));

    elevenlabsRefreshBtn.connect('clicked', () => {
        refreshElevenLabsVoices({silent: false}).catch(() => {});
    });

    elevenlabsVoiceRow.connect('notify::selected', () => ctx.onUserChange(() => {
        const idx = elevenlabsVoiceRow.get_selected();
        const chosen = elevenlabsVoices[idx];
        if (!chosen)
            return;
        ctx.withLoading(() => elevenlabsVoiceIdRow.set_text(chosen.id));
        ctx.patchConfig({voice_out: {elevenlabs_voice_id: chosen.id}}, `Saved ElevenLabs voice: ${chosen.name}`);
    }));

    elevenlabsVoiceIdRow.connect('apply', () => ctx.onUserChange(() => {
        const voiceId = elevenlabsVoiceIdRow.get_text().trim();
        if (!voiceId) {
            ctx.showToast('Voice ID cannot be empty');
            return;
        }
        rebuildElevenLabsVoiceCombo(voiceId);
        ctx.patchConfig({voice_out: {elevenlabs_voice_id: voiceId}}, `Saved ElevenLabs voice ID: ${voiceId}`);
    }));

    elevenlabsModelRow.connect('apply', () => ctx.onUserChange(() => {
        const modelId = elevenlabsModelRow.get_text().trim();
        if (!modelId) {
            ctx.showToast('Model ID cannot be empty');
            return;
        }
        ctx.patchConfig({voice_out: {elevenlabs_model_id: modelId}}, `Saved ElevenLabs model: ${modelId}`);
    }));

    piperLangRow.connect('notify::selected', () => ctx.onUserChange(() => {
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
            ctx.withLoading(() => piperCustomModelRow.set_text(chosen.id));
            ctx.patchConfig({voice_out: {piper_model: chosen.id}}, `Saved Piper voice: ${chosen.id}`);
        }
    }));

    piperModelRow.connect('notify::selected', () => ctx.onUserChange(() => {
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
        ctx.withLoading(() => piperCustomModelRow.set_text(chosen.id));
        ctx.patchConfig({voice_out: {piper_model: chosen.id}}, `Saved Piper voice: ${chosen.id}`);
    }));

    piperRefreshBtn.connect('clicked', () => ctx.onUserChange(() => {
        fetchPiperVoices({refresh: true, showResultToast: true}).catch(() => {});
    }));

    piperDownloadBtn.connect('clicked', () => ctx.onUserChange(async () => {
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
            ctx.showToast('Select a Piper voice model first');
            return;
        }

        piperDownloading = true;
        syncPiperDownloadButton();
        ctx.showToast(`Downloading Piper voice ${selectedId}...`);

        const resp = await ctx.httpJson(
            'POST',
            '/api/v1/tts/piper/download',
            {model: selectedId},
            {longTimeout: true}
        );
        piperDownloading = false;
        if (ctx.cancellable.is_cancelled())
            return;

        if (!resp.ok || resp.data?.ok === false) {
            if (resp.status === 0)
                ctx.setOfflineState(true);
            const errMsg = resp.data?.error
                ? String(resp.data.error)
                : `HTTP ${resp.status || 'offline'}`;
            syncPiperDownloadButton();
            ctx.showToast(`Piper download failed: ${errMsg}`);
            return;
        }
        ctx.setOfflineState(false);

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
        ctx.withLoading(() => piperCustomModelRow.set_text(selectedId));
        rebuildPiperModelCombo();
        await ctx.patchConfig({voice_out: {piper_model: selectedId}}, `Downloaded Piper voice ${selectedId}`);
    }));

    piperCustomModelRow.connect('apply', () => ctx.onUserChange(() => {
        const modelVal = piperCustomModelRow.get_text().trim();
        if (!modelVal) {
            ctx.showToast('Enter a Piper model ID or .onnx path');
            ctx.withLoading(() => piperCustomModelRow.set_text(currentPiperModel || DEFAULT_PIPER_MODEL));
            return;
        }
        currentPiperModel = modelVal;
        const inCatalog = piperVoices.find(v => v.id === modelVal);
        if (inCatalog && currentPiperLanguage && inCatalog.languageCode !== currentPiperLanguage) {
            currentPiperLanguage = '';
            ctx.withLoading(() => piperLangRow.set_selected(0));
        }
        rebuildPiperModelCombo();
        ctx.patchConfig({voice_out: {piper_model: modelVal}}, `Saved Piper model: ${modelVal}`);
    }));

    piperPresetRow.connect('notify::selected', () => ctx.onUserChange(() => {
        const idx = piperPresetRow.get_selected();
        const preset = PIPER_VOICE_PRESETS[idx];
        if (!preset || preset.id === 'custom' || preset.speed === null)
            return;
        ctx.withLoading(() => {
            piperSpeedRow.set_value(preset.speed);
            piperPitchRow.set_value(preset.pitch);
            piperNoiseRow.set_value(preset.noise_scale);
        });
        ctx.patchConfig(
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

    piperSpeedRow.connect('notify::value', () => ctx.onUserChange(() => {
        syncPiperPresetFromControls();
        const val = Number(piperSpeedRow.get_value().toFixed(2));
        ctx.patchConfig({voice_out: {piper_speed: val}}, 'Saved Piper speed');
    }));

    piperPitchRow.connect('notify::value', () => ctx.onUserChange(() => {
        syncPiperPresetFromControls();
        const val = Number(piperPitchRow.get_value().toFixed(1));
        ctx.patchConfig({voice_out: {piper_pitch: val}}, 'Saved Piper pitch');
    }));

    piperNoiseRow.connect('notify::value', () => ctx.onUserChange(() => {
        syncPiperPresetFromControls();
        const val = Number(piperNoiseRow.get_value().toFixed(3));
        ctx.patchConfig({voice_out: {piper_noise_scale: val}}, 'Saved Piper expressiveness');
    }));

    piperTestBtn.connect('clicked', () => ctx.onUserChange(async () => {
        if (ttsMuteRow.get_active()) {
            ctx.showToast('Voice output (TTS) is muted — unmute to preview');
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
        const resp = await ctx.httpJson('POST', '/api/v1/tts/replay', {text}, {longTimeout: true});
        if (ctx.cancellable.is_cancelled())
            return;
        piperTestBtn.set_sensitive(true);
        if (!resp.ok || resp.data?.ok === false) {
            if (resp.status === 0)
                ctx.setOfflineState(true);
            const errMsg = resp.data?.error
                ? String(resp.data.error)
                : `HTTP ${resp.status || 'offline'}`;
            ctx.showToast(`Voice preview failed: ${errMsg}`);
            return;
        }
        ctx.setOfflineState(false);
        ctx.showToast('Playing Piper voice preview');
    }));

    const applyConfig = (voiceIn, voiceOut, secretsConfigured) => {
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
    };

    const syncVisibility = () => {
        syncSttVisibility();
        syncTtsVisibility();
        rebuildPiperModelCombo();
        if (comboText(ttsProviderRow) === 'piper')
            fetchPiperVoices({refresh: false, showResultToast: false}).catch(() => {});
    };

    return {page, applyConfig, syncVisibility, syncInitialVisibility: () => {
        syncSttVisibility();
        syncTtsVisibility();
        syncPiperDownloadButton();
    }};
}
