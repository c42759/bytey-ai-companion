// SPDX-License-Identifier: GPL-3.0-or-later

import Adw from 'gi://Adw';
import GLib from 'gi://GLib';
import Gio from 'gi://Gio';
import Gtk from 'gi://Gtk';

export function createModalDialog({title, contentWidth = 520, contentHeight = 480, confirmLabel = 'Save', confirmCss = ['suggested-action'], onConfirm = null}) {
    const dialog = new Adw.Dialog({
        title,
        content_width: contentWidth,
        content_height: contentHeight,
    });

    const toolbar = new Adw.ToolbarView();
    const header = new Adw.HeaderBar();
    const cancelBtn = new Gtk.Button({label: 'Cancel'});
    cancelBtn.connect('clicked', () => dialog.close());
    header.pack_start(cancelBtn);

    let confirmBtn = null;
    if (confirmLabel) {
        confirmBtn = new Gtk.Button({
            label: confirmLabel,
            css_classes: confirmCss,
        });
        if (onConfirm)
            confirmBtn.connect('clicked', () => onConfirm(dialog));
        header.pack_end(confirmBtn);
    }

    toolbar.add_top_bar(header);
    const page = new Adw.PreferencesPage();
    toolbar.set_content(page);
    dialog.set_child(toolbar);

    return {dialog, page, header, confirmBtn};
}

export function confirmDestructiveAction(window, {heading, body, confirmLabel = 'Delete', onConfirm}) {
    const dlg = new Adw.AlertDialog({heading, body});
    dlg.add_response('cancel', 'Cancel');
    dlg.add_response('confirm', confirmLabel);
    dlg.set_response_appearance('confirm', Adw.ResponseAppearance.DESTRUCTIVE);
    dlg.set_default_response('cancel');
    dlg.set_close_response('cancel');
    dlg.connect('response', (_d, response) => {
        if (response === 'confirm')
            onConfirm();
    });
    dlg.present(window);
}

export function openFileDialog(window, {title, mode = 'open', filterName = null, mimeTypes = [], patterns = [], initialName = null, onSelect, onError = null}) {
    const dlg = new Gtk.FileDialog({title});
    if (filterName && (mimeTypes.length > 0 || patterns.length > 0)) {
        const filter = new Gtk.FileFilter();
        filter.set_name(filterName);
        for (const m of mimeTypes)
            filter.add_mime_type(m);
        for (const p of patterns)
            filter.add_pattern(p);
        const filters = new Gio.ListStore({item_type: Gtk.FileFilter.$gtype});
        filters.append(filter);
        dlg.set_filters(filters);
    }
    if (initialName)
        dlg.set_initial_name(initialName);

    const handleResult = (fn, res) => {
        try {
            const file = fn(res);
            if (file)
                onSelect(file);
        } catch (e) {
            if (!(e instanceof GLib.Error && e.matches(Gtk.DialogError, Gtk.DialogError.DISMISSED)))
                onError?.(e);
        }
    };

    if (mode === 'folder')
        dlg.select_folder(window, null, (_d, res) => handleResult(r => dlg.select_folder_finish(r), res));
    else if (mode === 'save')
        dlg.save(window, null, (_d, res) => handleResult(r => dlg.save_finish(r), res));
    else
        dlg.open(window, null, (_d, res) => handleResult(r => dlg.open_finish(r), res));
}
