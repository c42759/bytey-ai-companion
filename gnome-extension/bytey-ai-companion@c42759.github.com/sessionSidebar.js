// SPDX-License-Identifier: GPL-3.0-or-later

import Clutter from 'gi://Clutter';
import GLib from 'gi://GLib';
import Pango from 'gi://Pango';
import St from 'gi://St';

export function formatRelativeTime(rawTs) {
    if (!rawTs)
        return '';
    let ms = NaN;
    if (typeof rawTs === 'number') {
        ms = rawTs > 1e12 ? rawTs : rawTs * 1000;
    } else if (typeof rawTs === 'string') {
        const parsed = Date.parse(rawTs);
        if (Number.isFinite(parsed)) {
            ms = parsed;
        } else {
            const num = Number(rawTs);
            if (Number.isFinite(num))
                ms = num > 1e12 ? num : num * 1000;
        }
    }
    if (!Number.isFinite(ms))
        return '';

    const diffSec = Math.max(0, Math.floor((Date.now() - ms) / 1000));
    if (diffSec < 60)
        return 'just now';
    const diffMin = Math.floor(diffSec / 60);
    if (diffMin < 60)
        return `${diffMin}m ago`;
    const diffHr = Math.floor(diffMin / 60);
    if (diffHr < 24)
        return `${diffHr}h ago`;
    const diffDay = Math.floor(diffHr / 24);
    return `${diffDay}d ago`;
}

export function formatSessionSubtitle(session) {
    const rel = formatRelativeTime(session.updated_at ?? session.created_at);
    const count = Number(session.message_count);
    if (Number.isFinite(count) && count >= 0) {
        const countText = `${count} msg${count === 1 ? '' : 's'}`;
        return rel ? `${rel} · ${countText}` : countText;
    }
    const preview = typeof session.preview === 'string' ? session.preview.trim() : '';
    if (preview)
        return rel ? `${rel} · ${preview}` : preview;
    return rel || 'Chat';
}

export class SessionSidebar {
    constructor(sessionsList, callbacks = {}) {
        this._sessionsList = sessionsList;
        this._callbacks = callbacks;
        this._renameFocusIdleId = 0;
    }

    render({
        sessions,
        activeSessionId,
        editingSessionId,
        pendingDeleteSessionId,
        iconSize,
        onStateChange,
    }) {
        if (!this._sessionsList)
            return;

        this._sessionsList.destroy_all_children();

        if (sessions.length === 0) {
            this._sessionsList.add_child(new St.Label({
                style_class: 'bytey-sidebar-empty',
                text: 'No saved chats',
                x_expand: true,
                x_align: Clutter.ActorAlign.CENTER,
            }));
            return;
        }

        for (const session of sessions) {
            if (!session || !session.id)
                continue;
            const sid = String(session.id);
            const isActive = sid === activeSessionId;
            const isEditing = sid === editingSessionId;
            const isPendingDelete = sid === pendingDeleteSessionId;

            const row = new St.BoxLayout({
                style_class: isActive
                    ? 'bytey-session-row bytey-session-row-active'
                    : 'bytey-session-row',
                orientation: Clutter.Orientation.HORIZONTAL,
                x_expand: true,
                reactive: true,
            });

            const textCol = new St.BoxLayout({
                orientation: Clutter.Orientation.VERTICAL,
                x_expand: true,
                y_align: Clutter.ActorAlign.CENTER,
            });

            const subLabel = new St.Label({
                style_class: 'bytey-session-subtitle',
                text: formatSessionSubtitle(session),
                x_align: Clutter.ActorAlign.START,
                x_expand: true,
            });
            subLabel.clutter_text.ellipsize = Pango.EllipsizeMode.END;
            subLabel.clutter_text.single_line_mode = true;

            if (isEditing) {
                const currentTitle = String(session.title || 'New Chat');
                const titleEntry = new St.Entry({
                    style_class: 'bytey-session-title-entry',
                    text: currentTitle,
                    can_focus: true,
                    x_expand: true,
                });
                titleEntry.clutter_text.connect('activate', () => {
                    if (!this._sessionsList)
                        return;
                    const newTitle = titleEntry.get_text().trim();
                    onStateChange({editingSessionId: null});
                    if (newTitle && newTitle !== currentTitle.trim())
                        this._callbacks?.onRenameSession?.(sid, newTitle);
                });
                titleEntry.connect('key-press-event', (_actor, event) => {
                    if (event.get_key_symbol() === Clutter.KEY_Escape) {
                        onStateChange({editingSessionId: null});
                        return Clutter.EVENT_STOP;
                    }
                    return Clutter.EVENT_PROPAGATE;
                });

                textCol.add_child(titleEntry);
                textCol.add_child(subLabel);
                row.add_child(textCol);

                if (this._renameFocusIdleId) {
                    GLib.Source.remove(this._renameFocusIdleId);
                    this._renameFocusIdleId = 0;
                }
                this._renameFocusIdleId = GLib.idle_add(GLib.PRIORITY_DEFAULT_IDLE, () => {
                    this._renameFocusIdleId = 0;
                    if (this._sessionsList && this._callbacks?.isEditing?.(sid) && titleEntry.clutter_text) {
                        titleEntry.clutter_text.grab_key_focus();
                        titleEntry.clutter_text.set_selection(0, -1);
                    }
                    return GLib.SOURCE_REMOVE;
                });
            } else {
                const titleLabel = new St.Label({
                    style_class: 'bytey-session-title',
                    text: String(session.title || 'New Chat'),
                    x_align: Clutter.ActorAlign.START,
                    x_expand: true,
                });
                titleLabel.clutter_text.ellipsize = Pango.EllipsizeMode.END;
                titleLabel.clutter_text.single_line_mode = true;

                textCol.add_child(titleLabel);
                textCol.add_child(subLabel);

                const selectBtn = new St.Button({
                    style_class: 'bytey-session-select-btn',
                    can_focus: true,
                    x_expand: true,
                    child: textCol,
                });
                selectBtn.connect('clicked', () => {
                    this._callbacks?.onSelectSession?.(sid);
                });
                row.add_child(selectBtn);
            }

            if (isPendingDelete) {
                const confirmBtn = new St.Button({
                    style_class: 'bytey-session-confirm-delete-btn',
                    can_focus: true,
                    y_align: Clutter.ActorAlign.CENTER,
                    accessible_name: 'Confirm delete chat',
                    child: new St.Icon({
                        icon_name: 'object-select-symbolic',
                        icon_size: iconSize,
                    }),
                });
                confirmBtn.connect('clicked', () => {
                    onStateChange({pendingDeleteSessionId: null});
                    this._callbacks?.onDeleteSession?.(sid);
                });

                const cancelBtn = new St.Button({
                    style_class: 'bytey-session-cancel-delete-btn',
                    can_focus: true,
                    y_align: Clutter.ActorAlign.CENTER,
                    accessible_name: 'Cancel delete chat',
                    child: new St.Icon({
                        icon_name: 'window-close-symbolic',
                        icon_size: iconSize,
                    }),
                });
                cancelBtn.connect('clicked', () => {
                    onStateChange({pendingDeleteSessionId: null});
                });

                row.add_child(confirmBtn);
                row.add_child(cancelBtn);
            } else {
                const editBtn = new St.Button({
                    style_class: 'bytey-session-edit-btn',
                    can_focus: true,
                    y_align: Clutter.ActorAlign.CENTER,
                    accessible_name: 'Rename chat',
                    child: new St.Icon({
                        icon_name: 'document-edit-symbolic',
                        icon_size: iconSize,
                    }),
                });
                editBtn.connect('clicked', () => {
                    onStateChange({editingSessionId: sid, pendingDeleteSessionId: null});
                });

                const delBtn = new St.Button({
                    style_class: 'bytey-session-delete-btn',
                    can_focus: true,
                    y_align: Clutter.ActorAlign.CENTER,
                    accessible_name: 'Delete chat',
                    child: new St.Icon({
                        icon_name: 'user-trash-symbolic',
                        icon_size: iconSize,
                    }),
                });
                delBtn.connect('clicked', () => {
                    onStateChange({pendingDeleteSessionId: sid, editingSessionId: null});
                });

                row.add_child(editBtn);
                row.add_child(delBtn);
            }

            this._sessionsList.add_child(row);
        }
    }

    destroy() {
        if (this._renameFocusIdleId) {
            GLib.Source.remove(this._renameFocusIdleId);
            this._renameFocusIdleId = 0;
        }
        this._sessionsList = null;
        this._callbacks = null;
    }
}
