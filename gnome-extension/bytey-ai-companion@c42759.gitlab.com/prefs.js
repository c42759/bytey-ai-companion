// SPDX-License-Identifier: GPL-3.0-or-later

import Adw from 'gi://Adw';
import GLib from 'gi://GLib';
import Gio from 'gi://Gio';
import Gtk from 'gi://Gtk';
import Soup from 'gi://Soup?version=3.0';
import {ExtensionPreferences} from 'resource:///org/gnome/Shell/Extensions/js/extensions/prefs.js';

const DEFAULT_DAEMON_URL = 'http://127.0.0.1:8765';
const ANIMATION_MODES = ['full', 'state_only', 'disabled'];
const CHAT_DENSITIES = ['compact', 'comfortable', 'spacious'];
const POSITION_MODES = ['fixed', 'floating'];
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
const TTS_PROVIDERS = ['kokoro', 'elevenlabs', 'disabled'];
const AI_PROVIDERS = ['ollama', 'gemini', 'openai', 'anthropic', 'custom'];

function normalizeUrl(raw) {
    if (typeof raw !== 'string')
        return null;
    const trimmed = raw.trim();
    if (!/^https?:\/\/\S+$/i.test(trimmed))
        return null;
    const stripped = trimmed.replace(/\/+$/, '');
    return /^https?:\/\/\S+$/i.test(stripped) ? stripped : null;
}

function makeComboRow(title, items) {
    return new Adw.ComboRow({
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
        let destroyed = false;
        let loading = false;
        let settingsChangedId = 0;

        const getDaemonUrl = () => normalizeUrl(settings.get_string('daemon-url')) ?? DEFAULT_DAEMON_URL;

        const showToast = title => {
            if (!destroyed)
                window.add_toast(new Adw.Toast({title, timeout: 3}));
        };

        const httpJson = (method, path, body = undefined) => new Promise(resolve => {
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

            httpSession.send_and_read_async(
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
        generalPage.add(identityGroup);

        // 2. Visuals & Position Page
        const visualsPage = new Adw.PreferencesPage({
            title: 'Visuals & Position',
            icon_name: 'preferences-desktop-display-symbolic',
        });

        const eyesGroup = new Adw.PreferencesGroup({title: 'Eye Animations'});
        const animModeRow = makeComboRow('Animation Mode', ANIMATION_MODES);
        const cursorTrackRow = new Adw.SwitchRow({
            title: 'Cursor Tracking',
            active: true,
        });
        eyesGroup.add(animModeRow);
        eyesGroup.add(cursorTrackRow);
        visualsPage.add(eyesGroup);

        const drawerGroup = new Adw.PreferencesGroup({title: 'Chat Drawer'});
        const densityRow = makeComboRow('Chat Density', CHAT_DENSITIES);
        const chatWidthRow = makeSpinRow('Chat Width (px)', 260, 900, 10);
        const chatHeightRow = makeSpinRow('Chat Height (px)', 220, 900, 10);
        drawerGroup.add(densityRow);
        drawerGroup.add(chatWidthRow);
        drawerGroup.add(chatHeightRow);
        visualsPage.add(drawerGroup);

        const placementGroup = new Adw.PreferencesGroup({title: 'Island Placement'});
        const posModeRow = makeComboRow('Position Mode', POSITION_MODES);
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

        // 3. Voice & AI Page
        const voicePage = new Adw.PreferencesPage({
            title: 'Voice & AI',
            icon_name: 'audio-input-microphone-symbolic',
        });

        const voiceGroup = new Adw.PreferencesGroup({title: 'Voice Controls'});
        const micMuteRow = new Adw.SwitchRow({title: 'Mute Microphone'});
        const ttsMuteRow = new Adw.SwitchRow({title: 'Mute Voice (TTS)'});
        const sttBackendRow = makeComboRow('STT Backend', STT_BACKENDS);
        const ttsProviderRow = makeComboRow('TTS Provider', TTS_PROVIDERS);
        voiceGroup.add(micMuteRow);
        voiceGroup.add(ttsMuteRow);
        voiceGroup.add(sttBackendRow);
        voiceGroup.add(ttsProviderRow);
        voicePage.add(voiceGroup);

        const aiGroup = new Adw.PreferencesGroup({
            title: 'AI Provider',
            description: 'API keys stay securely in your core daemon .env or OS keyring.',
        });
        const providerRow = makeComboRow('Provider', AI_PROVIDERS);
        const modelRow = new Adw.EntryRow({
            title: 'Model',
            show_apply_button: true,
        });
        const baseUrlRow = new Adw.EntryRow({
            title: 'Base URL',
            show_apply_button: true,
        });
        aiGroup.add(providerRow);
        aiGroup.add(modelRow);
        aiGroup.add(baseUrlRow);
        voicePage.add(aiGroup);

        window.add(generalPage);
        window.add(visualsPage);
        window.add(voicePage);

        const syncPlacementSensitivity = () => {
            const isFloating = comboText(posModeRow) === 'floating';
            fixedAlignRow.set_sensitive(!isFloating);
            offsetXRow.set_sensitive(!isFloating);
            offsetYRow.set_sensitive(!isFloating);
            floatXRow.set_sensitive(isFloating);
            floatYRow.set_sensitive(isFloating);
        };

        const syncProviderVisibility = () => {
            baseUrlRow.set_visible(comboText(providerRow) !== 'gemini');
        };

        const setOfflineState = offline => {
            offlineBanner.set_revealed(offline);
            statusRow.set_subtitle(offline ? 'Offline' : 'Connected');
        };

        const applyConfig = cfg => {
            loading = true;
            try {
                const general = cfg.general ?? {};
                const visuals = cfg.visuals ?? {};
                const voiceIn = cfg.voice_in ?? cfg.voice_input ?? {};
                const voiceOut = cfg.voice_out ?? cfg.voice_output ?? {};
                const provider = cfg.provider ?? {};
                const llm = cfg.llm ?? {};

                petNameRow.set_text(String(general.pet_name ?? cfg.pet_name ?? 'Bytey'));
                wakePhraseRow.set_text(String(general.wake_phrase ?? cfg.wake_phrase ?? 'Hey Bytey'));

                setComboText(animModeRow, visuals.animation_mode ?? 'full');
                cursorTrackRow.set_active(Boolean(visuals.cursor_tracking ?? true));

                setComboText(densityRow, visuals.chat_density ?? 'comfortable');
                chatWidthRow.set_value(Number(visuals.chat_width ?? 380));
                chatHeightRow.set_value(Number(visuals.chat_height ?? 420));

                setComboText(posModeRow, visuals.position_mode ?? 'fixed');
                monitorRow.set_value(Number(visuals.monitor_index ?? 0));
                setComboText(fixedAlignRow, visuals.fixed_alignment ?? 'top-center');
                offsetXRow.set_value(Number(visuals.offset_x ?? 0));
                offsetYRow.set_value(Number(visuals.offset_y ?? 12));
                floatXRow.set_value(Number(visuals.float_x ?? 500));
                floatYRow.set_value(Number(visuals.float_y ?? 40));

                micMuteRow.set_active(Boolean(voiceIn.mic_muted ?? false));
                ttsMuteRow.set_active(Boolean(voiceOut.tts_muted ?? false));
                setComboText(sttBackendRow, voiceIn.stt_backend ?? 'local_whisper');
                setComboText(ttsProviderRow, voiceOut.tts_provider ?? voiceOut.provider ?? 'kokoro');

                setComboText(providerRow, provider.active_provider ?? llm.provider ?? 'ollama');
                modelRow.set_text(String(provider.model ?? llm.model ?? ''));
                baseUrlRow.set_text(String(provider.base_url ?? llm.base_url ?? ''));
            } finally {
                loading = false;
            }
            syncPlacementSensitivity();
            syncProviderVisibility();
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
                showToast(toastMsg);
            } else {
                setOfflineState(true);
                showToast('Daemon offline — changes not saved');
            }
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

        animModeRow.connect('notify::selected', () => onUserChange(() => {
            patchConfig({visuals: {animation_mode: comboText(animModeRow)}}, 'Saved animation mode');
        }));

        cursorTrackRow.connect('notify::active', () => onUserChange(() => {
            patchConfig({visuals: {cursor_tracking: cursorTrackRow.get_active()}}, 'Saved cursor tracking');
        }));

        densityRow.connect('notify::selected', () => onUserChange(() => {
            patchConfig({visuals: {chat_density: comboText(densityRow)}}, 'Saved chat density');
        }));

        chatWidthRow.connect('notify::value', () => onUserChange(() => {
            patchConfig({visuals: {chat_width: Math.round(chatWidthRow.get_value())}}, 'Saved chat width');
        }));

        chatHeightRow.connect('notify::value', () => onUserChange(() => {
            patchConfig({visuals: {chat_height: Math.round(chatHeightRow.get_value())}}, 'Saved chat height');
        }));

        posModeRow.connect('notify::selected', () => onUserChange(() => {
            syncPlacementSensitivity();
            patchConfig({visuals: {position_mode: comboText(posModeRow)}}, 'Saved position mode');
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

        ttsMuteRow.connect('notify::active', () => onUserChange(() => {
            patchConfig({voice_out: {tts_muted: ttsMuteRow.get_active()}}, 'Updated voice mute');
        }));

        sttBackendRow.connect('notify::selected', () => onUserChange(() => {
            patchConfig({voice_in: {stt_backend: comboText(sttBackendRow)}}, 'Saved STT backend');
        }));

        ttsProviderRow.connect('notify::selected', () => onUserChange(() => {
            patchConfig({voice_out: {tts_provider: comboText(ttsProviderRow)}}, 'Saved TTS provider');
        }));

        providerRow.connect('notify::selected', () => onUserChange(() => {
            syncProviderVisibility();
            patchConfig({provider: {active_provider: comboText(providerRow)}}, 'Saved AI provider');
        }));

        modelRow.connect('apply', () => onUserChange(() => {
            patchConfig({provider: {model: modelRow.get_text().trim()}}, 'Saved model');
        }));

        baseUrlRow.connect('apply', () => onUserChange(() => {
            patchConfig({provider: {base_url: baseUrlRow.get_text().trim()}}, 'Saved base URL');
        }));

        syncPlacementSensitivity();
        syncProviderVisibility();
        loadConfig();
    }
}
