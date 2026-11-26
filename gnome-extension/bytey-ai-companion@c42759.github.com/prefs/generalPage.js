// SPDX-License-Identifier: GPL-3.0-or-later

import Adw from 'gi://Adw';
import Gio from 'gi://Gio';
import Gtk from 'gi://Gtk';

import {normalizeUrl} from './constants.js';

export function buildGeneralPage(ctx) {
    const page = new Adw.PreferencesPage({
        title: 'General',
        icon_name: 'preferences-system-symbolic',
    });

    const offlineBanner = new Adw.Banner({
        title: 'Bytey daemon offline — start core daemon to edit live settings',
        button_label: 'Retry',
        revealed: false,
    });
    page.set_banner(offlineBanner);

    const brandGroup = new Adw.PreferencesGroup();
    const versionLabel = ctx.metadata?.['version-name'] ?? '0.1.0';
    const brandRow = new Adw.ActionRow({
        title: 'Bytey AI Companion',
        subtitle: `Dynamic Island desktop AI assistant · v${versionLabel}`,
    });
    const iconFile = ctx.dir.get_child('icons').get_child('bytey-ai-companion.svg');
    if (iconFile.query_exists(null)) {
        const brandIcon = new Gtk.Image({
            gicon: new Gio.FileIcon({file: iconFile}),
            pixel_size: 48,
            valign: Gtk.Align.CENTER,
        });
        brandRow.add_prefix(brandIcon);
    }
    brandGroup.add(brandRow);
    page.add(brandGroup);

    const connectionGroup = new Adw.PreferencesGroup({title: 'Connection'});
    const daemonUrlRow = new Adw.EntryRow({
        title: 'Daemon URL',
        show_apply_button: true,
    });
    daemonUrlRow.set_text(ctx.resolveBaseUrl());
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
    page.add(connectionGroup);

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

    const promptTitleLabel = new Gtk.Label({
        label: 'Companion System Prompt',
        xalign: 0,
        margin_top: 12,
        margin_bottom: 6,
        css_classes: ['heading'],
    });
    identityGroup.add(promptTitleLabel);

    const systemPromptView = new Gtk.TextView({
        wrap_mode: Gtk.WrapMode.WORD_CHAR,
        top_margin: 8,
        bottom_margin: 8,
        left_margin: 10,
        right_margin: 10,
    });
    const systemPromptScroll = new Gtk.ScrolledWindow({min_content_height: 150});
    systemPromptScroll.set_child(systemPromptView);
    const systemPromptFrame = new Gtk.Frame();
    systemPromptFrame.set_child(systemPromptScroll);
    identityGroup.add(systemPromptFrame);

    const promptBtnBox = new Gtk.Box({
        orientation: Gtk.Orientation.HORIZONTAL,
        spacing: 8,
        halign: Gtk.Align.END,
        margin_top: 8,
    });
    const resetSystemPromptBtn = new Gtk.Button({label: 'Reset Default'});
    const saveSystemPromptBtn = new Gtk.Button({
        label: 'Save Prompt',
        css_classes: ['suggested-action'],
    });
    promptBtnBox.append(resetSystemPromptBtn);
    promptBtnBox.append(saveSystemPromptBtn);
    identityGroup.add(promptBtnBox);

    page.add(identityGroup);

    let defaultSystemPrompt = '';

    const setOfflineState = offline => {
        offlineBanner.set_revealed(offline);
        statusRow.set_subtitle(offline ? 'Offline (unreachable)' : `Connected (${ctx.resolveBaseUrl()})`);
    };

    const setCheckingStatus = () => {
        statusRow.set_subtitle('Checking...');
    };

    offlineBanner.connect('button-clicked', () => ctx.loadConfig());
    testBtn.connect('clicked', () => ctx.checkHealth());

    daemonUrlRow.connect('apply', () => ctx.onUserChange(() => {
        const validUrl = normalizeUrl(daemonUrlRow.get_text());
        if (!validUrl) {
            ctx.showToast('Invalid Daemon URL — expected http:// or https://');
            ctx.withLoading(() => daemonUrlRow.set_text(ctx.resolveBaseUrl()));
            return;
        }
        const prevUrl = ctx.settings.get_string('daemon-url');
        ctx.withLoading(() => daemonUrlRow.set_text(validUrl));
        ctx.settings.set_string('daemon-url', validUrl);
        ctx.showToast('Saved Daemon URL');
        if (prevUrl === validUrl)
            ctx.loadConfig();
    }));

    const settingsChangedId = ctx.settings.connect('changed::daemon-url', () => {
        if (ctx.cancellable.is_cancelled())
            return;
        ctx.withLoading(() => daemonUrlRow.set_text(ctx.resolveBaseUrl()));
        ctx.loadConfig();
    });

    petNameRow.connect('apply', () => ctx.onUserChange(() => {
        const petName = petNameRow.get_text().trim();
        if (petName)
            ctx.patchConfig({general: {pet_name: petName}}, 'Saved companion name');
    }));

    wakePhraseRow.connect('apply', () => ctx.onUserChange(() => {
        const wakePhrase = wakePhraseRow.get_text().trim();
        if (wakePhrase)
            ctx.patchConfig({general: {wake_phrase: wakePhrase}}, 'Saved wake phrase');
    }));

    resetSystemPromptBtn.connect('clicked', () => ctx.onUserChange(() => {
        if (!defaultSystemPrompt) {
            ctx.showToast('Default prompt unavailable — check daemon connection');
            return;
        }
        systemPromptView.get_buffer().set_text(defaultSystemPrompt, -1);
        ctx.showToast('System prompt reset (click Save Prompt to apply)');
    }));

    saveSystemPromptBtn.connect('clicked', () => ctx.onUserChange(() => {
        const buf = systemPromptView.get_buffer();
        const [start, end] = buf.get_bounds();
        const prompt = buf.get_text(start, end, true).trim();
        const finalPrompt = prompt || defaultSystemPrompt;
        if (!finalPrompt) {
            ctx.showToast('System prompt cannot be empty');
            return;
        }
        ctx.patchConfig({general: {system_prompt: finalPrompt}}, 'Saved system prompt');
    }));

    const applyConfig = cfg => {
        const general = cfg.general ?? {};
        if (typeof cfg.default_system_prompt === 'string' && cfg.default_system_prompt.trim())
            defaultSystemPrompt = cfg.default_system_prompt;
        petNameRow.set_text(String(general.pet_name ?? cfg.pet_name ?? 'Bytey'));
        wakePhraseRow.set_text(String(general.wake_phrase ?? cfg.wake_phrase ?? 'Hey Bytey'));
        const promptText = String(general.system_prompt ?? cfg.system_prompt ?? defaultSystemPrompt ?? '');
        systemPromptView.get_buffer().set_text(promptText, -1);
    };

    const cleanup = () => {
        ctx.settings.disconnect(settingsChangedId);
    };

    return {page, applyConfig, setOfflineState, setCheckingStatus, cleanup};
}
