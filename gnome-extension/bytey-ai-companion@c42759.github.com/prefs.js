// SPDX-License-Identifier: GPL-3.0-or-later

import Adw from 'gi://Adw';
import {ExtensionPreferences} from 'resource:///org/gnome/Shell/Extensions/js/extensions/prefs.js';

import {buildAiPage} from './prefs/aiPage.js';
import {isMaskedPlaceholder} from './prefs/constants.js';
import {buildGeneralPage} from './prefs/generalPage.js';
import {buildGodModePage} from './prefs/godModePage.js';
import {createPrefsHttpClient} from './prefs/httpClient.js';
import {buildImportExportPage} from './prefs/importExportPage.js';
import {buildSkillsPage} from './prefs/skillsPage.js';
import {buildVisualsPage} from './prefs/visualsPage.js';
import {buildVoicePage} from './prefs/voicePage.js';
import {expandPreferencesPage, setSecretRowState} from './prefs/widgets.js';
import {buildWorkspacePage} from './prefs/workspacePage.js';

/**
 * Libadwaita preferences window for Bytey AI Companion.
 */
export default class ByteyPreferences extends ExtensionPreferences {
    fillPreferencesWindow(window) {
        window.set_default_size(680, 620);
        window.set_search_enabled(true);

        const settings = this.getSettings();
        const {cancellable, resolveBaseUrl, httpJson, connectConfigSocket, abortAll} = createPrefsHttpClient(settings);
        let loading = false;
        let secretsConfigured = {};

        const showToast = title => {
            if (!cancellable.is_cancelled())
                window.add_toast(new Adw.Toast({title}));
        };

        const withLoading = fn => {
            const prev = loading;
            loading = true;
            try {
                fn();
            } finally {
                loading = prev;
            }
        };

        const onUserChange = handler => {
            if (cancellable.is_cancelled() || loading)
                return;
            handler();
        };

        let general = null;
        let visuals = null;
        let voice = null;
        let ai = null;
        let godMode = null;
        let skills = null;
        let workspace = null;
        let importExport = null;

        const setOfflineState = offline => general?.setOfflineState(offline);

        const applyConfig = cfg => {
            withLoading(() => {
                secretsConfigured = cfg.secrets_configured && typeof cfg.secrets_configured === 'object'
                    ? {...cfg.secrets_configured}
                    : {};
                general.applyConfig(cfg);
                visuals.applyConfig(cfg.visuals ?? {});
                voice.applyConfig(
                    cfg.voice_in ?? cfg.voice_input ?? {},
                    cfg.voice_out ?? cfg.voice_output ?? {},
                    secretsConfigured
                );
                ai.applyConfig(cfg, secretsConfigured);
                skills.applyConfig(cfg.skills && typeof cfg.skills === 'object' ? cfg.skills : {});
                workspace.applyConfig(cfg.workspace && typeof cfg.workspace === 'object' ? cfg.workspace : {});
                godMode.applyConfig(cfg.agents && typeof cfg.agents === 'object' ? cfg.agents : {});
            });
            visuals.syncVisibility();
            voice.syncVisibility();
            ai.syncVisibility();
        };

        const loadConfig = async () => {
            if (cancellable.is_cancelled())
                return;
            general.setCheckingStatus();
            const resp = await httpJson('GET', '/api/v1/config');
            if (cancellable.is_cancelled())
                return;
            if (resp.ok && resp.data && typeof resp.data === 'object') {
                setOfflineState(false);
                applyConfig(resp.data);
                skills.loadSkills().catch(() => {});
                godMode.loadAgents().catch(() => {});
            } else {
                setOfflineState(true);
            }
        };

        const checkHealth = async () => {
            if (cancellable.is_cancelled())
                return;
            general.setCheckingStatus();
            const resp = await httpJson('GET', '/api/v1/health');
            if (cancellable.is_cancelled())
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
            if (cancellable.is_cancelled() || loading)
                return null;
            const resp = await httpJson('PATCH', '/api/v1/config', patch);
            if (cancellable.is_cancelled())
                return null;
            if (resp.ok) {
                setOfflineState(false);
                ai.onPatchLlmMetadata(patch, resp.data?.llm);
                showToast(toastMsg);
                return resp.data && typeof resp.data === 'object' ? resp.data : null;
            }
            setOfflineState(true);
            showToast('Daemon offline — changes not saved');
            return null;
        };

        const getSecret = async secretKey => {
            if (cancellable.is_cancelled())
                return {ok: false, offline: false, value: ''};
            const resp = await httpJson('GET', `/api/v1/secrets/${encodeURIComponent(secretKey)}`);
            if (cancellable.is_cancelled())
                return {ok: false, offline: false, value: ''};
            if (!resp.ok || !resp.data || typeof resp.data !== 'object') {
                setOfflineState(true);
                return {ok: false, offline: true, value: ''};
            }
            setOfflineState(false);
            return {ok: true, offline: false, value: String(resp.data.value ?? '')};
        };

        const saveSecret = async (secretKey, row, badge) => {
            if (cancellable.is_cancelled() || loading)
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
            if (cancellable.is_cancelled())
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
            withLoading(() => setSecretRowState(row, badge, true));
            showToast(`Saved ${secretKey}`);
            return true;
        };

        const ctx = {
            window,
            settings,
            dir: this.dir,
            metadata: this.metadata,
            cancellable,
            resolveBaseUrl,
            httpJson,
            showToast,
            withLoading,
            isLoading: () => loading,
            onUserChange,
            setOfflineState,
            applyConfig,
            loadConfig,
            checkHealth,
            patchConfig,
            saveSecret,
            getSecret,
        };

        general = buildGeneralPage(ctx);
        visuals = buildVisualsPage(ctx);
        voice = buildVoicePage(ctx);
        ai = buildAiPage(ctx);
        godMode = buildGodModePage(ctx);
        skills = buildSkillsPage(ctx);
        workspace = buildWorkspacePage(ctx);
        importExport = buildImportExportPage(ctx);

        window.connect('close-request', () => {
            general.cleanup();
            abortAll();
            return false;
        });

        for (const p of [
            general.page,
            visuals.page,
            voice.page,
            ai.page,
            godMode.page,
            skills.page,
            workspace.page,
            importExport.page,
        ]) {
            expandPreferencesPage(p);
            window.add(p);
        }

        visuals.syncVisibility();
        voice.syncInitialVisibility();
        ai.syncInitialVisibility();
        loadConfig();
        connectConfigSocket(cfg => {
            if (cancellable.is_cancelled())
                return;
            const nextSecrets = cfg.secrets_configured && typeof cfg.secrets_configured === 'object'
                ? {...cfg.secrets_configured}
                : secretsConfigured;
            secretsConfigured = nextSecrets;
            ai.syncFromRemoteConfig(cfg, secretsConfigured);
        });
    }
}
