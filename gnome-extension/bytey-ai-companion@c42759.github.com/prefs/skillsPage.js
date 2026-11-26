// SPDX-License-Identifier: GPL-3.0-or-later

import Adw from 'gi://Adw';
import GLib from 'gi://GLib';
import Gio from 'gi://Gio';
import Gtk from 'gi://Gtk';

import {confirmDestructiveAction, createModalDialog, openFileDialog} from './dialogs.js';

export function buildSkillsPage(ctx) {
    const page = new Adw.PreferencesPage({
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
    page.add(skillsGeneralGroup);

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
    page.add(skillsListGroup);

    let skillsList = [];
    let skillRows = [];
    let configuredSkillsDir = '';

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
            const editable = Boolean(skill?.editable ?? source === 'user');
            const row = new Adw.ActionRow({
                title: `${name} ${sourceTag}`,
                subtitle: String(skill?.error || skill?.description || ''),
            });
            if (!valid) {
                row.add_suffix(new Gtk.Image({
                    icon_name: 'dialog-warning-symbolic',
                    valign: Gtk.Align.CENTER,
                    tooltip_text: 'Invalid skill',
                    css_classes: ['warning'],
                }));
            }

            const enabledSwitch = new Gtk.Switch({
                active: Boolean(skill?.enabled ?? true),
                valign: Gtk.Align.CENTER,
                tooltip_text: 'Enable or disable skill',
            });
            enabledSwitch.connect('notify::active', () => ctx.onUserChange(() => {
                const active = enabledSwitch.get_active();
                ctx.httpJson('PATCH', `/api/v1/skills/${encodeURIComponent(name)}`, {enabled: active})
                    .then(resp => {
                        if (ctx.cancellable.is_cancelled())
                            return;
                        if (!resp.ok) {
                            ctx.showToast(`Failed to update ${name}`);
                            return;
                        }
                        skill.enabled = active;
                        ctx.showToast(`${active ? 'Enabled' : 'Disabled'} skill '${name}'`);
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

    const loadSkills = async () => {
        if (ctx.cancellable.is_cancelled())
            return;
        const resp = await ctx.httpJson('GET', '/api/v1/skills');
        if (ctx.cancellable.is_cancelled() || !resp.ok || !resp.data || typeof resp.data !== 'object')
            return;
        skillsList = Array.isArray(resp.data.skills) ? resp.data.skills : [];
        rebuildSkillsList();
    };

    const rescanSkills = async (showResultToast = false) => {
        if (ctx.cancellable.is_cancelled())
            return;
        const resp = await ctx.httpJson('POST', '/api/v1/skills/rescan', {});
        if (ctx.cancellable.is_cancelled())
            return;
        if (!resp.ok) {
            ctx.showToast('Failed to rescan skills');
            return;
        }
        if (resp.data && Array.isArray(resp.data.skills))
            skillsList = resp.data.skills;
        rebuildSkillsList();
        if (showResultToast)
            ctx.showToast(`Rescanned skills (${skillsList.length} found)`);
    };

    const openSkillEditor = skill => {
        if (ctx.cancellable.is_cancelled())
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
        dialog.present(ctx.window);

        if (!rawContent) {
            ctx.httpJson('GET', `/api/v1/skills/${encodeURIComponent(name)}`)
                .then(resp => {
                    if (!ctx.cancellable.is_cancelled() && resp.ok && resp.data?.raw_content)
                        textView.get_buffer().set_text(String(resp.data.raw_content), -1);
                })
                .catch(() => {});
        }

        if (editable && saveBtn) {
            saveBtn.connect('clicked', async () => {
                const buf = textView.get_buffer();
                const content = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), true);
                saveBtn.set_sensitive(false);
                const resp = await ctx.httpJson(
                    'PUT',
                    `/api/v1/skills/${encodeURIComponent(name)}`,
                    {content}
                );
                if (ctx.cancellable.is_cancelled())
                    return;
                saveBtn.set_sensitive(true);
                if (!resp.ok) {
                    const err = resp.data?.detail || resp.data?.error || 'Save failed';
                    ctx.showToast(String(err));
                    return;
                }
                dialog.close();
                ctx.showToast(`Saved skill '${name}'`);
                loadSkills().catch(() => {});
            });
        }
    };

    const confirmDeleteSkill = name => {
        confirmDestructiveAction(ctx.window, {
            heading: `Delete Skill '${name}'?`,
            body: 'This permanently removes the user skill directory and its files.',
            confirmLabel: 'Delete',
            onConfirm: async () => {
                const resp = await ctx.httpJson('DELETE', `/api/v1/skills/${encodeURIComponent(name)}`);
                if (ctx.cancellable.is_cancelled())
                    return;
                if (!resp.ok) {
                    const err = resp.data?.detail || resp.data?.error || 'Delete failed';
                    ctx.showToast(String(err));
                    return;
                }
                ctx.showToast(`Deleted skill '${name}'`);
                loadSkills().catch(() => {});
            },
        });
    };

    skillsEnabledRow.connect('notify::active', () => ctx.onUserChange(() => {
        ctx.patchConfig({skills: {enabled: skillsEnabledRow.get_active()}}, 'Saved');
    }));

    skillsAllowScriptsRow.connect('notify::active', () => ctx.onUserChange(() => {
        ctx.patchConfig(
            {skills: {allow_script_execution: skillsAllowScriptsRow.get_active()}},
            'Saved'
        );
    }));

    skillsBrowseBtn.connect('clicked', () => {
        if (ctx.cancellable.is_cancelled())
            return;
        openFileDialog(ctx.window, {
            title: 'Select Skills Storage Folder',
            mode: 'folder',
            onSelect: folder => {
                const path = folder?.get_path();
                if (!path)
                    return;
                configuredSkillsDir = path;
                skillsDirRow.set_subtitle(path);
                ctx.patchConfig({skills: {skills_dir: path}}, 'Skills folder updated')
                    .then(() => rescanSkills(false))
                    .catch(() => {});
            },
            onError: e => ctx.showToast(`Folder selection failed: ${e.message}`),
        });
    });

    skillsResetBtn.connect('clicked', () => {
        configuredSkillsDir = '';
        skillsDirRow.set_subtitle('Default (~/.config/bytey-ai-companion/skills)');
        ctx.patchConfig({skills: {skills_dir: ''}}, 'Skills folder reset to default')
            .then(() => rescanSkills(false))
            .catch(() => {});
    });

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
            ctx.showToast(`Could not open folder: ${e.message}`);
        }
    });

    skillsExtraDirsRow.connect('apply', () => ctx.onUserChange(() => {
        const dirs = skillsExtraDirsRow.get_text()
            .split(',')
            .map(s => s.trim())
            .filter(Boolean);
        ctx.patchConfig({skills: {extra_dirs: dirs}}, 'Saved extra skill directories')
            .then(() => rescanSkills(false))
            .catch(() => {});
    }));

    skillsRescanBtn.connect('clicked', () => {
        rescanSkills(true).catch(() => {});
    });

    addSkillBtn.connect('clicked', () => {
        if (ctx.cancellable.is_cancelled())
            return;
        const {dialog, page: dlgPage, confirmBtn: createBtn} = createModalDialog({
            title: 'Add Skill',
            contentWidth: 520,
            contentHeight: 540,
            confirmLabel: 'Create',
        });

        const metaGroup = new Adw.PreferencesGroup({title: 'Skill Metadata'});
        const nameRow = new Adw.EntryRow({title: 'Name (slug, e.g. my-skill)'});
        const descRow = new Adw.EntryRow({title: 'Description'});
        const slashRow = new Adw.EntryRow({title: 'Slash Command (optional, e.g. /myskill)'});
        metaGroup.add(nameRow);
        metaGroup.add(descRow);
        metaGroup.add(slashRow);
        dlgPage.add(metaGroup);

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
        dlgPage.add(bodyGroup);

        createBtn.connect('clicked', async () => {
            const name = nameRow.get_text().trim();
            const description = descRow.get_text().trim();
            const slashCommand = slashRow.get_text().trim();
            const buf = bodyView.get_buffer();
            const body = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), true);
            if (!name || !description) {
                ctx.showToast('Name and description are required');
                return;
            }
            const payload = {name, description, body};
            if (slashCommand)
                payload.slash_command = slashCommand;
            createBtn.set_sensitive(false);
            const resp = await ctx.httpJson('POST', '/api/v1/skills', payload);
            if (ctx.cancellable.is_cancelled())
                return;
            createBtn.set_sensitive(true);
            if (!resp.ok) {
                const err = resp.data?.detail || resp.data?.error || 'Create failed';
                ctx.showToast(String(err));
                return;
            }
            dialog.close();
            ctx.showToast(`Created skill '${name}'`);
            loadSkills().catch(() => {});
        });

        dialog.present(ctx.window);
    });

    importSkillBtn.connect('clicked', () => {
        if (ctx.cancellable.is_cancelled())
            return;
        const doImportPath = async path => {
            const resp = await ctx.httpJson('POST', '/api/v1/skills/import', {path});
            if (ctx.cancellable.is_cancelled())
                return;
            if (!resp.ok) {
                const err = resp.data?.detail || resp.data?.error || 'Import failed';
                ctx.showToast(String(err));
                return;
            }
            ctx.showToast('Skill imported');
            loadSkills().catch(() => {});
        };

        const pickFolder = () => openFileDialog(ctx.window, {
            title: 'Select Skill Folder',
            mode: 'folder',
            onSelect: folder => {
                const path = folder?.get_path();
                if (path)
                    doImportPath(path).catch(() => {});
            },
            onError: e => ctx.showToast(`Import failed: ${e.message}`),
        });

        const pickZip = () => openFileDialog(ctx.window, {
            title: 'Select Skill .zip Archive',
            mode: 'open',
            filterName: 'ZIP archives',
            mimeTypes: ['application/zip'],
            patterns: ['*.zip'],
            onSelect: file => {
                const path = file?.get_path();
                if (path)
                    doImportPath(path).catch(() => {});
            },
            onError: e => ctx.showToast(`Import failed: ${e.message}`),
        });

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
        dlg.present(ctx.window);
    });

    rebuildSkillsList();

    const applyConfig = skillsCfg => {
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
    };

    return {page, applyConfig, loadSkills};
}
