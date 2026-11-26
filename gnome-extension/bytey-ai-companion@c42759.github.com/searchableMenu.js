// SPDX-License-Identifier: GPL-3.0-or-later

import Clutter from 'gi://Clutter';
import St from 'gi://St';

export function populateSearchableMenu(menuBox, placeholder, items, renderItemFn, footerFn = null) {
    menuBox.destroy_all_children();
    const searchEntry = new St.Entry({
        style_class: 'bytey-menu-search',
        hint_text: placeholder,
        can_focus: true,
        reactive: true,
        x_expand: true,
    });
    menuBox.add_child(searchEntry);

    const listBox = new St.BoxLayout({
        orientation: Clutter.Orientation.VERTICAL,
        x_expand: true,
    });
    const scrollView = new St.ScrollView({
        style_class: 'bytey-menu-scroll',
        hscrollbar_policy: St.PolicyType.NEVER,
        vscrollbar_policy: St.PolicyType.AUTOMATIC,
        overlay_scrollbars: false,
        x_expand: true,
    });
    scrollView.set_child(listBox);
    menuBox.add_child(scrollView);

    const rebuildList = query => {
        listBox.destroy_all_children();
        const q = String(query || '').trim().toLowerCase();
        const filtered = q
            ? items.filter(it => it.searchText.includes(q))
            : items;
        if (filtered.length === 0) {
            listBox.add_child(new St.Label({
                style_class: 'bytey-workspace-menu-item',
                text: 'No matches',
            }));
            return;
        }
        for (const item of filtered)
            listBox.add_child(renderItemFn(item));
    };

    searchEntry.clutter_text.connect('text-changed', () => {
        rebuildList(searchEntry.get_text());
    });
    searchEntry.clutter_text.connect('key-press-event', (_actor, event) => {
        if (event.get_key_symbol() === Clutter.KEY_Escape) {
            menuBox.visible = false;
            return Clutter.EVENT_STOP;
        }
        return Clutter.EVENT_PROPAGATE;
    });

    rebuildList('');
    if (footerFn)
        footerFn(menuBox);

    return searchEntry;
}
