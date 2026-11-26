// SPDX-License-Identifier: GPL-3.0-or-later

import Adw from 'gi://Adw';
import Gtk from 'gi://Gtk';

import {openFileDialog} from './dialogs.js';
import {makeSearchableComboRow} from './widgets.js';

export function buildWorkspacePage(ctx) {
    const page = new Adw.PreferencesPage({
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
    page.add(workspaceFolderGroup);

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
    page.add(workspacePolicyGroup);

    let workspaceRecentPaths = [];
    let workspaceUpdating = false;

    const syncControls = (activePath, recentPaths) => {
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
        const resp = await ctx.httpJson('PUT', '/api/v1/workspace', {path: cleaned});
        if (ctx.cancellable.is_cancelled())
            return;
        if (!resp.ok) {
            if (resp.status === 0)
                ctx.setOfflineState(true);
            ctx.showToast(`Workspace update failed: ${resp.error || 'Daemon unreachable'}`);
            return;
        }
        ctx.setOfflineState(false);
        const data = resp.data && typeof resp.data === 'object' ? resp.data : {};
        const nextActive = String(data.active_path ?? cleaned).trim();
        const nextRecent = Array.isArray(data.recent_paths)
            ? data.recent_paths
            : (nextActive
                ? [nextActive, ...workspaceRecentPaths.filter(p => p !== nextActive)].slice(0, 10)
                : workspaceRecentPaths);
        syncControls(nextActive, nextRecent);
        ctx.showToast(nextActive ? `Workspace set to ${nextActive}` : 'Workspace cleared');
    };

    workspaceChooseBtn.connect('clicked', () => {
        if (ctx.cancellable.is_cancelled())
            return;
        openFileDialog(ctx.window, {
            title: 'Select Work Folder',
            mode: 'folder',
            onSelect: folder => {
                const path = folder?.get_path();
                if (path)
                    setWorkspacePath(path).catch(() => {});
            },
            onError: e => ctx.showToast(`Folder selection failed: ${e.message}`),
        });
    });

    workspaceClearBtn.connect('clicked', () => {
        setWorkspacePath('').catch(() => {});
    });

    workspaceRecentRow.connect('notify::selected', () => {
        if (ctx.isLoading() || workspaceUpdating)
            return;
        const idx = workspaceRecentRow.get_selected();
        if (idx === Gtk.INVALID_LIST_POSITION)
            return;
        if (idx === 0)
            setWorkspacePath('').catch(() => {});
        else if (idx >= 1 && idx <= workspaceRecentPaths.length)
            setWorkspacePath(workspaceRecentPaths[idx - 1]).catch(() => {});
    });

    workspaceAutoSkillsRow.connect('notify::active', () => ctx.onUserChange(() => {
        ctx.patchConfig({workspace: {auto_load_skills: workspaceAutoSkillsRow.get_active()}}, 'Saved');
    }));

    workspaceAutoRulesRow.connect('notify::active', () => ctx.onUserChange(() => {
        ctx.patchConfig({workspace: {auto_load_rules: workspaceAutoRulesRow.get_active()}}, 'Saved');
    }));

    workspaceWriteApprovalRow.connect('notify::active', () => ctx.onUserChange(() => {
        ctx.patchConfig({workspace: {require_write_approval: workspaceWriteApprovalRow.get_active()}}, 'Saved');
    }));

    workspaceCmdApprovalRow.connect('notify::active', () => ctx.onUserChange(() => {
        ctx.patchConfig({workspace: {require_command_approval: workspaceCmdApprovalRow.get_active()}}, 'Saved');
    }));

    const applyConfig = ws => {
        syncControls(ws.active_path ?? '', ws.recent_paths ?? []);
        workspaceAutoSkillsRow.set_active(Boolean(ws.auto_load_skills ?? true));
        workspaceAutoRulesRow.set_active(Boolean(ws.auto_load_rules ?? true));
        workspaceWriteApprovalRow.set_active(Boolean(ws.require_write_approval ?? true));
        workspaceCmdApprovalRow.set_active(Boolean(ws.require_command_approval ?? true));
    };

    return {page, applyConfig};
}
