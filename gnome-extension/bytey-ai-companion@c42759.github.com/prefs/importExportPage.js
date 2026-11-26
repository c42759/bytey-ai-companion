// SPDX-License-Identifier: GPL-3.0-or-later

import Adw from 'gi://Adw';
import GLib from 'gi://GLib';
import Gio from 'gi://Gio';
import Gtk from 'gi://Gtk';

import {confirmDestructiveAction, openFileDialog} from './dialogs.js';

function buildExportPayload(rawCfg) {
    const exportKeys = ['general', 'provider', 'voice_in', 'voice_out', 'visuals', 'mcp_servers'];
    const out = {};
    for (const key of exportKeys) {
        if (rawCfg && Object.prototype.hasOwnProperty.call(rawCfg, key))
            out[key] = rawCfg[key];
    }
    return out;
}

export function buildImportExportPage(ctx) {
    const page = new Adw.PreferencesPage({
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

    page.add(backupGroup);

    const confirmReplaceImport = parsedConfig => {
        confirmDestructiveAction(ctx.window, {
            heading: 'Replace All Configuration?',
            body: 'Importing this JSON file will replace all existing Bytey configurations. This cannot be undone.',
            confirmLabel: 'Replace All',
            onConfirm: async () => {
                const resp = await ctx.httpJson('PUT', '/api/v1/config', parsedConfig);
                if (ctx.cancellable.is_cancelled())
                    return;
                if (resp.ok && resp.data && typeof resp.data === 'object') {
                    ctx.setOfflineState(false);
                    ctx.applyConfig(resp.data);
                    ctx.showToast('Configuration imported and replaced');
                } else {
                    const errMsg = resp.data?.detail || resp.data?.error || 'Failed to import configuration';
                    ctx.showToast(errMsg);
                }
            },
        });
    };

    exportBtn.connect('clicked', async () => {
        if (ctx.cancellable.is_cancelled())
            return;
        const resp = await ctx.httpJson('GET', '/api/v1/config');
        if (ctx.cancellable.is_cancelled())
            return;
        if (!resp.ok || !resp.data || typeof resp.data !== 'object') {
            ctx.setOfflineState(true);
            ctx.showToast('Daemon offline — cannot export configuration');
            return;
        }
        const payload = JSON.stringify(buildExportPayload(resp.data), null, 2) + '\n';
        openFileDialog(ctx.window, {
            title: 'Export Bytey Configuration',
            mode: 'save',
            filterName: 'JSON files',
            mimeTypes: ['application/json'],
            patterns: ['*.json'],
            initialName: 'bytey-config.json',
            onSelect: file => {
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
                            ctx.showToast(`Exported to ${file.get_basename()}`);
                        } catch (e) {
                            ctx.showToast(`Export failed: ${e.message}`);
                        }
                    }
                );
            },
            onError: e => ctx.showToast(`Export failed: ${e.message}`),
        });
    });

    importBtn.connect('clicked', () => {
        if (ctx.cancellable.is_cancelled())
            return;
        openFileDialog(ctx.window, {
            title: 'Import Bytey Configuration',
            mode: 'open',
            filterName: 'JSON files',
            mimeTypes: ['application/json'],
            patterns: ['*.json'],
            onSelect: file => {
                file.load_contents_async(null, (source, res) => {
                    try {
                        const [ok, contents] = source.load_contents_finish(res);
                        if (!ok) {
                            ctx.showToast('Failed to read selected file');
                            return;
                        }
                        const text = new TextDecoder().decode(contents);
                        const parsed = JSON.parse(text);
                        if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
                            ctx.showToast('Invalid JSON: expected a configuration object');
                            return;
                        }
                        confirmReplaceImport(parsed);
                    } catch (e) {
                        ctx.showToast(`Import failed: ${e.message}`);
                    }
                });
            },
            onError: e => ctx.showToast(`Import failed: ${e.message}`),
        });
    });

    return {page};
}
