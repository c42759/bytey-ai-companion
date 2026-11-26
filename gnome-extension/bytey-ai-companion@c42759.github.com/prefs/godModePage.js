// SPDX-License-Identifier: GPL-3.0-or-later

import Adw from 'gi://Adw';
import Gtk from 'gi://Gtk';

import {makeSearchableComboRow} from './widgets.js';

export function buildGodModePage(ctx) {
    const page = new Adw.PreferencesPage({
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
    page.add(godModeGroup);

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
    page.add(agentsGroup);

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
                if (ctx.isLoading())
                    return;
                ctx.httpJson('PATCH', `/api/v1/agents/${encodeURIComponent(agentId)}`, {
                    enabled: sw.get_active(),
                }).then(r => {
                    if (r.ok && r.data?.agents)
                        rebuildAgentsList(r.data.agents);
                    else if (!r.ok)
                        ctx.showToast(r.error || 'Failed to update agent');
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
                ctx.httpJson('PATCH', `/api/v1/agents/${encodeURIComponent(agentId)}`, {
                    model: modelRow.get_text().trim(),
                }).then(r => {
                    if (r.ok)
                        ctx.showToast(`${agent.display_name || agentId} model saved`);
                    else
                        ctx.showToast(r.error || 'Failed to save model');
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
                ctx.httpJson('PATCH', `/api/v1/agents/${encodeURIComponent(agentId)}`, {
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
                ctx.httpJson('PATCH', `/api/v1/agents/${encodeURIComponent(agentId)}`, {
                    allow_autonomous_flags: autoFlagsRow.get_active(),
                }).catch(() => {});
            });
            row.add_row(autoFlagsRow);

            agentsGroup.add(row);
            agentRows.push(row);
        }
    };

    const loadAgents = async () => {
        const resp = await ctx.httpJson('GET', '/api/v1/agents');
        if (ctx.cancellable.is_cancelled())
            return;
        if (resp.ok && resp.data)
            rebuildAgentsList(resp.data.agents || []);
    };

    detectAgentsBtn.connect('clicked', () => {
        ctx.httpJson('POST', '/api/v1/agents/detect', {}).then(r => {
            if (r.ok && r.data) {
                rebuildAgentsList(r.data.agents || []);
                ctx.showToast('CLI agents scanned');
            } else {
                ctx.showToast(r.error || 'Failed to detect CLI agents');
            }
        }).catch(() => {});
    });

    godModeEnabledRow.connect('notify::active', () => ctx.onUserChange(() => {
        ctx.patchConfig({agents: {god_mode_enabled: godModeEnabledRow.get_active()}}, 'Saved');
    }));
    godModeApprovalRow.connect('notify::active', () => ctx.onUserChange(() => {
        ctx.patchConfig({agents: {require_run_approval: godModeApprovalRow.get_active()}}, 'Saved');
    }));
    godModeTimeoutRow.connect('notify::value', () => ctx.onUserChange(() => {
        ctx.patchConfig({agents: {timeout_seconds: Math.round(godModeTimeoutRow.get_value())}}, 'Saved');
    }));

    const applyConfig = agentsCfg => {
        godModeEnabledRow.set_active(Boolean(agentsCfg.god_mode_enabled ?? false));
        godModeApprovalRow.set_active(Boolean(agentsCfg.require_run_approval ?? true));
        godModeTimeoutRow.set_value(Number(agentsCfg.timeout_seconds ?? 300));
    };

    return {page, applyConfig, loadAgents};
}
