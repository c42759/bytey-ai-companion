// SPDX-License-Identifier: GPL-3.0-or-later

import St from 'gi://St';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as PopupMenu from 'resource:///org/gnome/shell/ui/popupMenu.js';

/**
 * Native GNOME right-click `PopupMenu` anchored to the Dynamic Island pill.
 */
export class IslandContextMenu {
    /**
     * @param {St.Widget} sourceActor
     * @param {object} callbacks
     * @param {() => void} [callbacks.onOpenPreferences]
     * @param {(muted: boolean) => void} [callbacks.onToggleMicMute]
     * @param {(muted: boolean) => void} [callbacks.onToggleTtsMute]
     * @param {(floating: boolean) => void} [callbacks.onToggleFloating]
     * @param {() => void} [callbacks.onNewChat]
     * @param {() => void} [callbacks.onExit]
     */
    constructor(sourceActor, callbacks = {}) {
        this._sourceActor = sourceActor;
        this._callbacks = callbacks;
        this._syncing = false;
        this._destroyed = false;
        this._signalIds = [];

        this._menuManager = new PopupMenu.PopupMenuManager(sourceActor);
        this._menu = new PopupMenu.PopupMenu(sourceActor, 0.5, St.Side.TOP);
        this._menu.actor.add_style_class_name('bytey-context-menu');
        this._menu.actor.hide();
        this._menuManager.addMenu(this._menu);
        Main.uiGroup.add_child(this._menu.actor);

        this._buildItems();
    }

    /**
     * @returns {boolean}
     */
    get isOpen() {
        return Boolean(this._menu?.isOpen);
    }

    _connect(obj, signal, handler) {
        const id = obj.connect(signal, handler);
        this._signalIds.push([obj, id]);
        return id;
    }

    _buildItems() {
        const prefsItem = new PopupMenu.PopupMenuItem('Preferences');
        this._connect(prefsItem, 'activate', () => {
            this._callbacks?.onOpenPreferences?.();
        });
        this._menu.addMenuItem(prefsItem);

        this._menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());

        this._micMuteItem = new PopupMenu.PopupSwitchMenuItem('Mute Microphone', false);
        this._connect(this._micMuteItem, 'toggled', (_item, state) => {
            if (!this._syncing)
                this._callbacks?.onToggleMicMute?.(Boolean(state));
        });
        this._menu.addMenuItem(this._micMuteItem);

        this._ttsMuteItem = new PopupMenu.PopupSwitchMenuItem('Mute Voice (TTS)', false);
        this._connect(this._ttsMuteItem, 'toggled', (_item, state) => {
            if (!this._syncing)
                this._callbacks?.onToggleTtsMute?.(Boolean(state));
        });
        this._menu.addMenuItem(this._ttsMuteItem);

        this._floatingItem = new PopupMenu.PopupSwitchMenuItem('Floating Position', false);
        this._connect(this._floatingItem, 'toggled', (_item, state) => {
            if (!this._syncing)
                this._callbacks?.onToggleFloating?.(Boolean(state));
        });
        this._menu.addMenuItem(this._floatingItem);

        this._menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());

        const newChatItem = new PopupMenu.PopupMenuItem('New Chat Session');
        this._connect(newChatItem, 'activate', () => {
            this._callbacks?.onNewChat?.();
        });
        this._menu.addMenuItem(newChatItem);

        this._menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());

        const exitItem = new PopupMenu.PopupMenuItem('Exit');
        exitItem.label.add_style_class_name('bytey-context-menu-danger');
        this._connect(exitItem, 'activate', () => {
            this._callbacks?.onExit?.();
        });
        this._menu.addMenuItem(exitItem);
    }

    /**
     * Synchronize switch states with daemon config without emitting toggle events.
     *
     * @param {object} config
     */
    syncFromConfig(config) {
        if (this._destroyed || !config)
            return;

        const micMuted = Boolean(config.voice_in?.mic_muted ?? config.voice_input?.mic_muted ?? false);
        const ttsMuted = Boolean(config.voice_out?.tts_muted ?? config.voice_output?.tts_muted ?? false);
        const isFloating = (config.visuals?.position_mode ?? 'fixed') === 'floating';

        this._syncing = true;
        try {
            this._micMuteItem.setToggleState(micMuted);
            this._ttsMuteItem.setToggleState(ttsMuted);
            this._floatingItem.setToggleState(isFloating);
        } finally {
            this._syncing = false;
        }
    }

    /**
     * Toggle menu visibility, orienting arrow upward or downward based on island placement.
     *
     * @param {boolean} [isLowerHalf=false]
     */
    toggle(isLowerHalf = false) {
        if (this._destroyed || !this._menu)
            return;

        if (!this._menu.isOpen) {
            const side = isLowerHalf ? St.Side.BOTTOM : St.Side.TOP;
            if (typeof this._menu._boxPointer?.updateArrowSide === 'function')
                this._menu._boxPointer.updateArrowSide(side);
        }

        this._menu.toggle();
    }

    /**
     * Close the context menu if open.
     */
    close() {
        if (this._destroyed || !this._menu?.isOpen)
            return;
        this._menu.close();
    }

    /**
     * Remove menu from manager and destroy actors.
     */
    destroy() {
        if (this._destroyed)
            return;
        this._destroyed = true;

        if (this._menu)
            this._menu.close(false);

        for (const [obj, id] of this._signalIds) {
            try {
                obj.disconnect(id);
            } catch (_e) {
                // Ignore if item was already disposed.
            }
        }
        this._signalIds = [];

        if (this._menu) {
            this._menuManager?.removeMenu(this._menu);
            this._menu.actor?.get_parent()?.remove_child(this._menu.actor);
            this._menu.destroy();
            this._menu = null;
        }
        this._menuManager = null;
        this._sourceActor = null;
        this._callbacks = null;
        this._micMuteItem = null;
        this._ttsMuteItem = null;
        this._floatingItem = null;
    }
}
