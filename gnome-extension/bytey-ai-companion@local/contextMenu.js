import Gio from 'gi://Gio';
import St from 'gi://St';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as PopupMenu from 'resource:///org/gnome/shell/ui/popupMenu.js';

const SETTINGS_SCRIPT_PATH = '/home/c42759/Git/pet/settings-ui/run_settings.py';

/**
 * Native GNOME right-click `PopupMenu` anchored to the Dynamic Island pill.
 */
export class IslandContextMenu {
    /**
     * @param {St.Widget} sourceActor
     * @param {object} callbacks
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

    _buildItems() {
        const configItem = new PopupMenu.PopupMenuItem('Configuration');
        configItem.connect('activate', () => {
            this._launchSettings();
        });
        this._menu.addMenuItem(configItem);

        this._menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());

        this._micMuteItem = new PopupMenu.PopupSwitchMenuItem('Mute Microphone', false);
        this._micMuteItem.connect('toggled', (_item, state) => {
            if (!this._syncing)
                this._callbacks.onToggleMicMute?.(Boolean(state));
        });
        this._menu.addMenuItem(this._micMuteItem);

        this._ttsMuteItem = new PopupMenu.PopupSwitchMenuItem('Mute Voice (TTS)', false);
        this._ttsMuteItem.connect('toggled', (_item, state) => {
            if (!this._syncing)
                this._callbacks.onToggleTtsMute?.(Boolean(state));
        });
        this._menu.addMenuItem(this._ttsMuteItem);

        this._floatingItem = new PopupMenu.PopupSwitchMenuItem('Floating Position', false);
        this._floatingItem.connect('toggled', (_item, state) => {
            if (!this._syncing)
                this._callbacks.onToggleFloating?.(Boolean(state));
        });
        this._menu.addMenuItem(this._floatingItem);

        this._menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());

        const newChatItem = new PopupMenu.PopupMenuItem('New Chat Session');
        newChatItem.connect('activate', () => {
            this._callbacks.onNewChat?.();
        });
        this._menu.addMenuItem(newChatItem);

        this._menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());

        const exitItem = new PopupMenu.PopupMenuItem('Exit');
        exitItem.label.add_style_class_name('bytey-context-menu-danger');
        exitItem.connect('activate', () => {
            this._callbacks.onExit?.();
        });
        this._menu.addMenuItem(exitItem);
    }

    _launchSettings() {
        try {
            const proc = Gio.Subprocess.new(
                ['python3', SETTINGS_SCRIPT_PATH],
                Gio.SubprocessFlags.NONE
            );
            proc.wait_async(null, (source, res) => {
                try {
                    source.wait_finish(res);
                } catch (_e) {
                    // Settings window closed or terminated.
                }
            });
        } catch (err) {
            console.error(`Bytey AI Companion: failed to launch Settings UI: ${err.message}`);
        }
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

        if (this._menu) {
            this._menu.close(false);
            this._menuManager?.removeMenu(this._menu);
            this._menu.destroy();
            this._menu = null;
        }
        this._menuManager = null;
    }
}
