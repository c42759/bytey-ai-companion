// SPDX-License-Identifier: GPL-3.0-or-later

import Adw from 'gi://Adw';
import Gtk from 'gi://Gtk';

import {SECRET_MASK_PLACEHOLDER} from './constants.js';

export function makeSearchableComboRow(props) {
    return new Adw.ComboRow({
        enable_search: true,
        expression: Gtk.PropertyExpression.new(Gtk.StringObject, null, 'string'),
        ...props,
    });
}

export function makeComboRow(title, items) {
    return makeSearchableComboRow({
        title,
        model: Gtk.StringList.new(items),
    });
}

export function comboText(row) {
    const item = row.get_selected_item();
    return item ? item.get_string() : '';
}

export function setComboText(row, value) {
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

export function makeSpinRow(title, min, max, step) {
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

export function makeFloatSpinRow(title, subtitle, min, max, step, digits) {
    return new Adw.SpinRow({
        title,
        subtitle,
        digits,
        adjustment: new Gtk.Adjustment({
            lower: min,
            upper: max,
            step_increment: step,
            page_increment: step * 10,
            value: min,
        }),
    });
}

export function makeSecretRow(title) {
    const row = new Adw.PasswordEntryRow({
        title,
        show_apply_button: true,
    });
    const badge = new Gtk.Label({
        label: 'Not set',
        valign: Gtk.Align.CENTER,
        css_classes: ['dim-label', 'caption'],
    });
    row.add_suffix(badge);
    return {row, badge};
}

export function setSecretRowState(row, badge, configured) {
    if (configured) {
        badge.set_label('Configured');
        badge.remove_css_class('dim-label');
        badge.add_css_class('success');
        row.set_text(SECRET_MASK_PLACEHOLDER);
    } else {
        badge.set_label('Not set');
        badge.remove_css_class('success');
        badge.add_css_class('dim-label');
        row.set_text('');
    }
}

export function readTextBuffer(view) {
    const buf = view.get_buffer();
    const [start, end] = buf.get_bounds();
    return buf.get_text(start, end, true).trim();
}

export function makeTextViewBox(labelText, view, minHeight = 96) {
    const box = new Gtk.Box({
        orientation: Gtk.Orientation.VERTICAL,
        spacing: 6,
        margin_top: 6,
        margin_bottom: 6,
    });
    const lbl = new Gtk.Label({
        label: labelText,
        xalign: 0,
        css_classes: ['caption', 'dim-label'],
    });
    const scroll = new Gtk.ScrolledWindow({min_content_height: minHeight});
    scroll.set_child(view);
    const frame = new Gtk.Frame();
    frame.set_child(scroll);
    box.append(lbl);
    box.append(frame);
    return box;
}

export function expandPreferencesPage(widget) {
    if (widget instanceof Adw.Clamp) {
        widget.set_maximum_size(100000);
        widget.set_tightening_threshold(100000);
        return;
    }
    let child = widget.get_first_child();
    while (child) {
        expandPreferencesPage(child);
        child = child.get_next_sibling();
    }
}
