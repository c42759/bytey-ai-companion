// SPDX-License-Identifier: GPL-3.0-or-later

import Adw from 'gi://Adw';
import Gtk from 'gi://Gtk';

import {
    AI_PROVIDERS,
    MCP_TRANSPORTS,
    PROVIDER_DEFAULT_URLS,
    PROVIDER_SECRET_MAP,
    buildContextPresets,
    clampContextValue,
    cloneJson,
    formatContextShort,
    formatNumberCommas,
    normalizePositiveInt,
} from './constants.js';
import {createModalDialog} from './dialogs.js';
import {
    comboText,
    makeComboRow,
    makeSearchableComboRow,
    makeSecretRow,
    makeTextViewBox,
    readTextBuffer,
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
    let mcpServers = {};
    let mcpRows = [];
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
    page.add(mcpGroup);

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
    page.add(mcpJsonGroup);

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

    const showMcpInfoDialog = async name => {
        if (ctx.cancellable.is_cancelled())
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

        const {dialog, page: dlgPage} = createModalDialog({
            title: mcpTitle,
            contentWidth: 540,
            contentHeight: 520,
            confirmLabel: null,
        });

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
        dlgPage.add(infoGroup);

        const toolsGroup = new Adw.PreferencesGroup({
            title: 'Tools',
            description: 'Discovering available tools...',
        });
        const loadingRow = new Adw.ActionRow({
            title: 'Loading tools...',
            subtitle: isBuiltin ? 'Fetching built-in Bytey MCP Host tools' : `Connecting to ${mcpTitle}`,
        });
        toolsGroup.add(loadingRow);
        dlgPage.add(toolsGroup);

        dialog.present(ctx.window);

        const resp = isBuiltin
            ? await ctx.httpJson('GET', '/api/v1/mcp/host')
            : await ctx.httpJson('POST', '/api/v1/mcp/test', {name: mcpTitle, config: cfg});
        if (ctx.cancellable.is_cancelled())
            return;

        toolsGroup.remove(loadingRow);
        if (!resp.ok) {
            if (resp.status === 0)
                ctx.setOfflineState(true);
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

        ctx.setOfflineState(false);
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

    const testMcpServer = async name => {
        const cfg = mcpServers[name];
        if (!cfg || ctx.cancellable.is_cancelled())
            return;
        ctx.showToast(`Testing MCP server '${name}'...`);
        const resp = await ctx.httpJson('POST', '/api/v1/mcp/test', {name, config: cfg});
        if (ctx.cancellable.is_cancelled())
            return;
        if (!resp.ok) {
            if (resp.status === 0)
                ctx.setOfflineState(true);
            const errMsg = resp.data?.error
                ? String(resp.data.error)
                : `HTTP ${resp.status || 'offline'}`;
            ctx.showToast(`Test failed for ${name}: ${errMsg}`);
            const alert = Adw.AlertDialog.new(`MCP Test Failed: ${name}`, errMsg);
            alert.add_response('close', 'Close');
            alert.present(ctx.window);
            return;
        }
        ctx.setOfflineState(false);
        const payload = resp.data && typeof resp.data === 'object' ? resp.data : {};
        if (payload.ok === false) {
            const err = String(payload.error ?? 'Connection failed');
            ctx.showToast(`${name}: ${err}`);
            const alert = Adw.AlertDialog.new(`MCP Test Failed: ${name}`, err);
            alert.add_response('close', 'Close');
            alert.present(ctx.window);
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
        ctx.showToast(`${name}: discovered ${lines.length} tool(s)`);
        const alert = Adw.AlertDialog.new(`MCP Connected: ${name}`, summary);
        alert.add_response('close', 'Close');
        alert.present(ctx.window);
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
        hostInfoBtn.connect('clicked', () => ctx.onUserChange(() => {
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
            enabledSwitch.connect('notify::active', () => ctx.onUserChange(() => {
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
            testMcpBtn.connect('clicked', () => ctx.onUserChange(() => {
                testMcpServer(name).catch(() => {});
            }));

            const infoMcpBtn = new Gtk.Button({
                icon_name: 'dialog-information-symbolic',
                valign: Gtk.Align.CENTER,
                tooltip_text: 'Inspect MCP details & tools',
                css_classes: ['flat'],
            });
            infoMcpBtn.connect('clicked', () => ctx.onUserChange(() => {
                showMcpInfoDialog(name).catch(() => {});
            }));

            const editMcpBtn = new Gtk.Button({
                icon_name: 'document-edit-symbolic',
                valign: Gtk.Align.CENTER,
                tooltip_text: 'Edit',
                css_classes: ['flat'],
            });
            editMcpBtn.connect('clicked', () => ctx.onUserChange(() => {
                openMcpDialog(name);
            }));

            const delMcpBtn = new Gtk.Button({
                icon_name: 'user-trash-symbolic',
                valign: Gtk.Align.CENTER,
                tooltip_text: 'Delete',
                css_classes: ['flat', 'destructive-action'],
            });
            delMcpBtn.connect('clicked', () => ctx.onUserChange(() => {
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
        if (ctx.cancellable.is_cancelled())
            return;
        const raw = JSON.stringify({mcpServers}, null, 2);
        syncMcpJsonText(raw);
        rebuildMcpList();

        const resp = await ctx.httpJson('PUT', '/api/v1/mcp/json', {raw_json: raw});
        if (ctx.cancellable.is_cancelled())
            return;
        if (!resp.ok) {
            if (resp.status === 0)
                ctx.setOfflineState(true);
            const errDetail = resp.data?.error ? `: ${resp.data.error}` : '';
            ctx.showToast(`MCP update failed${errDetail}`);
            return;
        }
        ctx.setOfflineState(false);
        if (resp.data?.mcpServers && typeof resp.data.mcpServers === 'object') {
            mcpServers = normalizeMcpMap(resp.data.mcpServers);
            rebuildMcpList();
        }
        syncMcpJsonText(resp.data?.raw_json ?? null);
        ctx.showToast(toastMsg);
    };

    const applyRawMcpJson = async () => {
        if (ctx.cancellable.is_cancelled())
            return;
        const raw = readTextBuffer(mcpJsonView);
        let parsed;
        try {
            parsed = JSON.parse(raw);
        } catch (err) {
            ctx.showToast(`Invalid JSON: ${err.message}`);
            return;
        }
        if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
            ctx.showToast('Root JSON value must be an object');
            return;
        }
        const servers = Object.prototype.hasOwnProperty.call(parsed, 'mcpServers')
            ? parsed.mcpServers
            : parsed;
        if (!servers || typeof servers !== 'object' || Array.isArray(servers)) {
            ctx.showToast('"mcpServers" must be a JSON object');
            return;
        }

        const resp = await ctx.httpJson('PUT', '/api/v1/mcp/json', {raw_json: raw});
        if (ctx.cancellable.is_cancelled())
            return;
        if (!resp.ok) {
            if (resp.status === 0)
                ctx.setOfflineState(true);
            const errDetail = resp.data?.error ? `: ${resp.data.error}` : '';
            ctx.showToast(`MCP apply failed${errDetail}`);
            return;
        }
        ctx.setOfflineState(false);
        mcpServers = normalizeMcpMap(resp.data?.mcpServers ?? servers);
        syncMcpJsonText(resp.data?.raw_json ?? null);
        rebuildMcpList();
        ctx.showToast(`Applied mcpServers JSON (${Object.keys(mcpServers).length} server(s))`);
    };

    const openMcpDialog = (existingName = null) => {
        if (ctx.cancellable.is_cancelled())
            return;
        const cfg = existingName ? cloneJson(mcpServers[existingName] ?? {}) : {};
        const {dialog, page: dlgPage, confirmBtn: saveBtn} = createModalDialog({
            title: existingName ? `Edit MCP Server: ${existingName}` : 'Add MCP Server',
            contentWidth: 520,
            contentHeight: 600,
            confirmLabel: 'Save',
        });

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
                const normalizeMcpUrl = raw => {
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
                const targetNorm = normalizeMcpUrl(url);
                for (const [otherName, otherCfg] of Object.entries(mcpServers)) {
                    if (existingName && otherName === existingName)
                        continue;
                    const otherUrl = normalizeMcpUrl(otherCfg?.url ?? '');
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

        dialog.present(ctx.window);
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

    addMcpBtn.connect('clicked', () => ctx.onUserChange(() => {
        openMcpDialog(null);
    }));

    applyMcpJsonBtn.connect('clicked', () => ctx.onUserChange(() => {
        applyRawMcpJson().catch(() => {});
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
        mcpServers = normalizeMcpMap(cfg.mcpServers ?? cfg.mcp_servers ?? {});
    };

    const syncVisibility = () => {
        syncProviderVisibility();
        syncMcpJsonText();
        rebuildMcpList();
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
        syncMcpJsonText();
        rebuildMcpList();
        updateToolsSensitivity();
        refreshContextSizeRow();
    };

    return {page, applyConfig, syncVisibility, syncInitialVisibility, onPatchLlmMetadata};
}
