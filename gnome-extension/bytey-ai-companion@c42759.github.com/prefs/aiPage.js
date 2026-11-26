// SPDX-License-Identifier: GPL-3.0-or-later

import Adw from 'gi://Adw';
import Gtk from 'gi://Gtk';

import {
    AI_PROVIDERS,
    PROVIDER_DEFAULT_URLS,
    PROVIDER_SECRET_MAP,
    buildContextPresets,
    clampContextValue,
    formatContextShort,
    formatNumberCommas,
    normalizePositiveInt,
} from './constants.js';
import {buildMcpSection} from './mcpSection.js';
import {
    comboText,
    makeComboRow,
    makeSearchableComboRow,
    makeSecretRow,
    setComboText,
    setSecretRowState,
} from './widgets.js';

export function buildAiPage(ctx) {
    const page = new Adw.PreferencesPage({
        title: 'AI',
        icon_name: 'system-run-symbolic',
    });

    let availableModels = [];
    let currentConfiguredModel = '';
    let modelFetchSeq = 0;
    const modelToolMap = new Map();
    const modelContextMap = new Map();
    let detectedToolsSupport = false;
    let currentContextSize = null;
    let detectedMaxContext = null;
    let contextPresetOptions = buildContextPresets(null, null);
    let secretsConfigured = {};

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
    page.add(aiGroup);

    const mcp = buildMcpSection(ctx, page);

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

        mcp.setSensitive(active);
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
        ctx.withLoading(() => {
            contextSizeRow.set_model(Gtk.StringList.new(contextPresetOptions.map(o => o.label)));
            contextSizeRow.set_selected(selIdx);
            contextSizeRow.set_subtitle(
                maxCtx
                    ? `Max supported by model: ${formatContextShort(maxCtx)} (${formatNumberCommas(maxCtx)} tokens)`
                    : 'Context window tokens (Auto uses provider default)'
            );
        });
        return changed;
    };

    const setEmptyModelsState = message => {
        ctx.withLoading(() => {
            availableModels = [];
            modelToolMap.clear();
            modelContextMap.clear();
            modelRow.set_model(Gtk.StringList.new(['No models available']));
            modelRow.set_selected(0);
            modelRow.set_sensitive(false);
            modelRow.set_subtitle(message);
        });
        updateToolsSensitivity();
        refreshContextSizeRow();
    };

    const fetchModels = async (
        provider,
        baseUrl = '',
        {autoSelectIfMissing = false, showResultToast = false} = {}
    ) => {
        if (ctx.cancellable.is_cancelled())
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

        const resp = await ctx.httpJson('GET', path);
        if (ctx.cancellable.is_cancelled() || seq !== modelFetchSeq)
            return;

        if (!resp.ok || !resp.data || typeof resp.data !== 'object') {
            ctx.setOfflineState(true);
            setEmptyModelsState('Daemon offline — unable to fetch models');
            if (showResultToast)
                ctx.showToast('Unable to fetch models — daemon offline');
            return;
        }
        ctx.setOfflineState(false);

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
                ctx.showToast(errMsg);
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

        ctx.withLoading(() => {
            modelRow.set_model(Gtk.StringList.new(modelIds));
            modelRow.set_selected(targetIdx);
            modelRow.set_sensitive(true);
            const errHint = resp.data.error ? ` (fallback: ${resp.data.error})` : '';
            modelRow.set_subtitle(`${availableModels.length} model(s) available${errHint}`);
        });

        if (matchIdx < 0 && autoSelectIfMissing && chosen) {
            currentConfiguredModel = chosen.id;
            updateToolsSensitivity();
            const ctxClamped = refreshContextSizeRow({clampAndReturnChanged: true});
            const patch = {provider: {model: chosen.id}};
            if (ctxClamped)
                patch.provider.context_size = currentContextSize;
            if (typeof chosen.supports_tools === 'boolean')
                patch.supports_tools = chosen.supports_tools;
            await ctx.patchConfig(patch, `Selected model ${chosen.id}`);
        } else {
            updateToolsSensitivity();
            const ctxClamped = refreshContextSizeRow({clampAndReturnChanged: true});
            if (ctxClamped) {
                await ctx.patchConfig(
                    {provider: {context_size: currentContextSize}},
                    `Context size clamped to ${formatContextShort(currentContextSize)}`
                );
            } else if (showResultToast) {
                ctx.showToast(`Loaded ${availableModels.length} model(s) from ${prov}`);
            }
        }
    };

    providerRow.connect('notify::selected', () => ctx.onUserChange(async () => {
        const prov = comboText(providerRow);
        const defaultUrl = PROVIDER_DEFAULT_URLS[prov] ?? '';
        modelToolMap.clear();
        modelContextMap.clear();
        detectedMaxContext = null;
        ctx.withLoading(() => {
            baseUrlRow.set_text(defaultUrl);
            syncProviderVisibility();
        });
        refreshContextSizeRow();
        await ctx.patchConfig({provider: {active_provider: prov, base_url: defaultUrl}}, 'Saved AI provider');
        if (!ctx.cancellable.is_cancelled() && comboText(providerRow) === prov)
            await fetchModels(prov, defaultUrl, {autoSelectIfMissing: true});
    }));

    baseUrlRow.connect('apply', () => ctx.onUserChange(async () => {
        const prov = comboText(providerRow);
        const nextUrl = baseUrlRow.get_text().trim();
        await ctx.patchConfig({provider: {base_url: nextUrl}}, 'Saved base URL');
        if (!ctx.cancellable.is_cancelled() && comboText(providerRow) === prov)
            await fetchModels(prov, nextUrl, {autoSelectIfMissing: false});
    }));

    aiApiKeyRow.connect('apply', () => ctx.onUserChange(async () => {
        const prov = comboText(providerRow);
        const secretKey = PROVIDER_SECRET_MAP[prov];
        if (!secretKey)
            return;
        const saved = await ctx.saveSecret(secretKey, aiApiKeyRow, aiApiKeyBadge);
        if (saved && !ctx.cancellable.is_cancelled() && comboText(providerRow) === prov)
            await fetchModels(prov, baseUrlRow.get_text().trim(), {autoSelectIfMissing: true});
    }));

    refreshModelsBtn.connect('clicked', () => ctx.onUserChange(() => {
        fetchModels(comboText(providerRow), baseUrlRow.get_text().trim(), {
            autoSelectIfMissing: false,
            showResultToast: true,
        }).catch(() => {});
    }));

    modelRow.connect('notify::selected', () => ctx.onUserChange(() => {
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
        ctx.patchConfig(patch, `Saved model: ${chosen.id}`);
    }));

    contextSizeRow.connect('notify::selected', () => ctx.onUserChange(() => {
        const idx = contextSizeRow.get_selected();
        const opt = contextPresetOptions[idx];
        const nextVal = opt ? opt.value : null;
        if (nextVal === currentContextSize)
            return;
        currentContextSize = nextVal;
        ctx.patchConfig(
            {provider: {context_size: currentContextSize}},
            currentContextSize ? `Saved context size: ${opt.label}` : 'Context size set to Auto'
        );
    }));

    focusWindowRow.connect('notify::value', () => ctx.onUserChange(() => {
        const windowSize = Math.max(0, Math.min(500, Math.round(focusWindowRow.get_value())));
        ctx.patchConfig(
            {provider: {focus_window: windowSize}},
            windowSize === 0 ? 'Focus window set to full history' : `Saved focus window: ${windowSize}`
        );
    }));

    maxToolRoundsRow.connect('notify::value', () => ctx.onUserChange(() => {
        const rounds = Math.max(1, Math.min(100, Math.round(maxToolRoundsRow.get_value())));
        ctx.patchConfig(
            {provider: {max_tool_rounds: rounds}},
            `Saved max tool loops: ${rounds}`
        );
    }));

    forceToolsRow.connect('notify::active', () => ctx.onUserChange(() => {
        updateToolsSensitivity();
        ctx.patchConfig(
            {provider: {supports_tools_override: forceToolsRow.get_active() ? true : null}},
            'Updated tool override'
        );
    }));

    const applyConfig = (cfg, nextSecretsConfigured) => {
        secretsConfigured = nextSecretsConfigured;
        const provider = cfg.provider ?? {};
        const llm = cfg.llm ?? {};

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
        mcp.setConfig(cfg.mcpServers ?? cfg.mcp_servers ?? {});
    };

    const syncVisibility = () => {
        syncProviderVisibility();
        mcp.sync();
        updateToolsSensitivity();
        refreshContextSizeRow();
        fetchModels(comboText(providerRow), baseUrlRow.get_text().trim(), {
            autoSelectIfMissing: false,
            showResultToast: false,
        }).catch(() => {});
    };

    const onPatchLlmMetadata = (patch, llmData) => {
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
                ctx.httpJson('PATCH', '/api/v1/config', {provider: {context_size: currentContextSize}}).catch(() => {});
            }
        }
    };

    const syncInitialVisibility = () => {
        syncProviderVisibility();
        mcp.sync();
        updateToolsSensitivity();
        refreshContextSizeRow();
    };

    return {page, applyConfig, syncVisibility, syncInitialVisibility, onPatchLlmMetadata};
}
