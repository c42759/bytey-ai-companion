// SPDX-License-Identifier: GPL-3.0-or-later

import Clutter from 'gi://Clutter';
import GLib from 'gi://GLib';
import Pango from 'gi://Pango';
import St from 'gi://St';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';

const DRAWER_GAP_PX = 8;
const DRAWER_WIDTH_FALLBACK = 380;
const DRAWER_HEIGHT_FALLBACK = 420;
const DRAWER_MIN_WIDTH = 260;
const DRAWER_MAX_WIDTH = 900;
const DRAWER_MIN_HEIGHT = 220;
const DRAWER_MAX_HEIGHT = 900;
const SIDEBAR_WIDTH = 210;
const SIDEBAR_ANIM_DURATION_MS = 220;

const DENSITY_ORDER = ['compact', 'comfortable', 'spacious'];
const DENSITY_LABELS = {
    compact: 'Compact',
    comfortable: 'Comfort',
    spacious: 'Spacious',
};
const DENSITY_ICON_SIZES = {
    compact: {close: 12, mic: 13, replay: 11},
    comfortable: {close: 14, mic: 15, replay: 13},
    spacious: {close: 16, mic: 17, replay: 15},
};
const DENSITY_ENTRY_METRICS = {
    compact: {minHeight: 28, lineHeight: 16},
    comfortable: {minHeight: 34, lineHeight: 18},
    spacious: {minHeight: 40, lineHeight: 20},
};
const ENTRY_MAX_LINES = 5;

const CONNECTION_STATES = ['connected', 'error', 'disconnected'];
const CONNECTION_STATUS_LABELS = {
    connected: 'Connected',
    error: 'Connection error',
    disconnected: 'Disconnected',
};

/**
 * Anchored expandable conversation transcript and tool-confirmation drawer.
 */
export class TranscriptDrawer {
    /**
     * @param {import('./positionManager.js').PositionManager} positionManager
     * @param {object} callbacks
     * @param {(text: string) => void} [callbacks.onSendText]
     * @param {() => void} [callbacks.onClearChat]
     * @param {(messageId: string, text: string) => void} [callbacks.onReplayTts]
     * @param {() => void} [callbacks.onManualMic]
     * @param {(callId: string, approved: boolean) => void} [callbacks.onConfirmTool]
     * @param {(width: number, height: number) => void} [callbacks.onResizeCommit]
     * @param {(density: string) => void} [callbacks.onDensityChange]
     * @param {(isOpen: boolean) => void} [callbacks.onOpenChanged]
     * @param {(sessionId: string) => void} [callbacks.onSelectSession]
     * @param {(sessionId: string) => void} [callbacks.onDeleteSession]
     * @param {(sessionId: string, newTitle: string) => void} [callbacks.onRenameSession]
     * @param {() => void} [callbacks.onCreateSession]
     * @param {(isOpen: boolean) => void} [callbacks.onToggleSidebar]
     */
    constructor(positionManager, callbacks = {}) {
        this._positionManager = positionManager;
        this._callbacks = callbacks;

        this._isOpen = false;
        this._sidebarOpen = false;
        this._destroyed = false;
        this._connectionState = 'disconnected';
        this._online = false;
        this._listening = false;
        this._petName = 'Bytey';
        this._monitorIndex = 0;

        this._drawerWidth = DRAWER_WIDTH_FALLBACK;
        this._drawerHeight = DRAWER_HEIGHT_FALLBACK;
        this._density = 'comfortable';
        this._lastIslandBounds = {x: 500, y: 12, width: 112, height: 38};

        this._resizing = false;
        this._resizeGrab = null;
        this._resizeStartCoords = [0, 0];
        this._resizeStartSize = [DRAWER_WIDTH_FALLBACK, DRAWER_HEIGHT_FALLBACK];

        this._messages = [];
        this._sessions = [];
        this._activeSessionId = null;
        this._pendingDeleteSessionId = null;
        this._editingSessionId = null;
        this._messageWidgets = new Map();
        this._toolWidgets = new Map();
        this._emptyStateActor = null;
        this._scrollIdleId = 0;
        this._focusIdleId = 0;
        this._renameFocusIdleId = 0;
        this._signalIds = [];

        this._container = new St.BoxLayout({
            style_class: 'bytey-drawer bytey-density-comfortable',
            orientation: Clutter.Orientation.VERTICAL,
            width: this._drawerWidth,
            height: this._drawerHeight,
            reactive: true,
            visible: false,
            x_expand: false,
            y_expand: false,
        });

        this._buildHeader();
        this._buildBody();

        this._connect(this._container, 'key-press-event', (_actor, event) => {
            if (event.get_key_symbol() === Clutter.KEY_Escape) {
                this.close();
                return Clutter.EVENT_STOP;
            }
            return Clutter.EVENT_PROPAGATE;
        });

        Main.layoutManager.addTopChrome(this._container);
        this._statusTooltip = new St.Label({
            style_class: 'bytey-status-tooltip',
            text: CONNECTION_STATUS_LABELS.disconnected,
            reactive: false,
            visible: false,
        });
        Main.layoutManager.addTopChrome(this._statusTooltip);
        this._renderEmptyState();
    }

    /**
     * @returns {boolean}
     */
    get isOpen() {
        return this._isOpen;
    }

    /**
     * @returns {St.BoxLayout}
     */
    get actor() {
        return this._container;
    }

    _connect(obj, signal, handler) {
        const id = obj.connect(signal, handler);
        this._signalIds.push([obj, id]);
        return id;
    }

    _buildHeader() {
        const header = new St.BoxLayout({
            style_class: 'bytey-drawer-header',
            x_expand: true,
            y_align: Clutter.ActorAlign.CENTER,
        });

        this._sidebarToggleIcon = new St.Icon({
            icon_name: 'sidebar-show-symbolic',
            icon_size: DENSITY_ICON_SIZES[this._density].close,
        });
        this._sidebarToggleBtn = new St.Button({
            style_class: 'bytey-sidebar-toggle-btn',
            can_focus: true,
            y_align: Clutter.ActorAlign.CENTER,
            accessible_name: 'Toggle chat history',
            child: this._sidebarToggleIcon,
        });
        this._connect(this._sidebarToggleBtn, 'clicked', () => {
            this.toggleSidebar();
        });
        header.add_child(this._sidebarToggleBtn);

        this._providerLabel = new St.Label({
            style_class: 'bytey-provider-pill-label',
            text: 'ollama',
            x_align: Clutter.ActorAlign.START,
            y_align: Clutter.ActorAlign.CENTER,
        });
        this._providerLabel.clutter_text.ellipsize = Pango.EllipsizeMode.END;

        this._providerPill = new St.Bin({
            style_class: 'bytey-provider-pill',
            y_align: Clutter.ActorAlign.CENTER,
            child: this._providerLabel,
        });
        header.add_child(this._providerPill);

        this._statusDot = new St.Widget({
            style_class: 'bytey-status-dot bytey-status-disconnected',
            x_align: Clutter.ActorAlign.CENTER,
            y_align: Clutter.ActorAlign.CENTER,
        });

        this._statusIndicator = new St.Bin({
            style_class: 'bytey-status-indicator',
            reactive: true,
            track_hover: true,
            x_align: Clutter.ActorAlign.CENTER,
            y_align: Clutter.ActorAlign.CENTER,
            accessible_name: CONNECTION_STATUS_LABELS.disconnected,
            child: this._statusDot,
        });
        this._connect(this._statusIndicator, 'notify::hover', () => {
            if (this._statusIndicator.hover)
                this._showStatusTooltip();
            else
                this._hideStatusTooltip();
        });
        header.add_child(this._statusIndicator);

        header.add_child(new St.Widget({x_expand: true}));

        this._densityBtn = new St.Button({
            style_class: 'bytey-density-btn',
            label: DENSITY_LABELS[this._density] ?? 'Comfort',
            can_focus: true,
            y_align: Clutter.ActorAlign.CENTER,
        });
        this._connect(this._densityBtn, 'clicked', () => {
            this._cycleDensity();
        });
        header.add_child(this._densityBtn);

        const clearBtn = new St.Button({
            style_class: 'bytey-clear-btn',
            label: 'New Chat',
            can_focus: true,
            y_align: Clutter.ActorAlign.CENTER,
        });
        this._connect(clearBtn, 'clicked', () => {
            this._callbacks.onClearChat?.();
        });
        header.add_child(clearBtn);

        this._closeIcon = new St.Icon({
            icon_name: 'window-close-symbolic',
            icon_size: DENSITY_ICON_SIZES[this._density].close,
        });
        const closeBtn = new St.Button({
            style_class: 'bytey-close-btn',
            can_focus: true,
            y_align: Clutter.ActorAlign.CENTER,
            child: this._closeIcon,
        });
        this._connect(closeBtn, 'clicked', () => this.close());
        header.add_child(closeBtn);

        this._container.add_child(header);
    }

    _buildBody() {
        this._bodyRow = new St.BoxLayout({
            style_class: 'bytey-drawer-body',
            orientation: Clutter.Orientation.HORIZONTAL,
            x_expand: true,
            y_expand: true,
        });

        this._sidebarBox = new St.BoxLayout({
            style_class: 'bytey-chat-sidebar',
            orientation: Clutter.Orientation.VERTICAL,
            width: SIDEBAR_WIDTH,
            visible: false,
            clip_to_allocation: true,
            x_expand: false,
            y_expand: true,
        });

        this._sidebarNewIcon = new St.Icon({
            icon_name: 'list-add-symbolic',
            icon_size: DENSITY_ICON_SIZES[this._density].close,
        });
        const newChatContent = new St.BoxLayout({
            style_class: 'bytey-sidebar-new-content',
            x_expand: true,
            y_align: Clutter.ActorAlign.CENTER,
        });
        newChatContent.add_child(this._sidebarNewIcon);
        newChatContent.add_child(new St.Label({
            style_class: 'bytey-sidebar-new-label',
            text: 'New Chat',
            y_align: Clutter.ActorAlign.CENTER,
        }));

        const newChatBtn = new St.Button({
            style_class: 'bytey-sidebar-new-btn',
            can_focus: true,
            x_expand: true,
            child: newChatContent,
        });
        this._connect(newChatBtn, 'clicked', () => {
            if (this._callbacks.onCreateSession)
                this._callbacks.onCreateSession();
            else
                this._callbacks.onClearChat?.();
        });
        this._sidebarBox.add_child(newChatBtn);

        this._sessionsList = new St.BoxLayout({
            style_class: 'bytey-sessions-list',
            orientation: Clutter.Orientation.VERTICAL,
            x_expand: true,
        });
        this._sessionsScroll = new St.ScrollView({
            style_class: 'bytey-sessions-scroll',
            hscrollbar_policy: St.PolicyType.NEVER,
            vscrollbar_policy: St.PolicyType.AUTOMATIC,
            enable_mouse_scrolling: true,
            overlay_scrollbars: false,
            x_expand: true,
            y_expand: true,
        });
        this._sessionsScroll.set_child(this._sessionsList);
        this._sidebarBox.add_child(this._sessionsScroll);

        this._chatColumn = new St.BoxLayout({
            style_class: 'bytey-chat-column',
            orientation: Clutter.Orientation.VERTICAL,
            x_expand: true,
            y_expand: true,
        });

        this._buildMessageArea(this._chatColumn);
        this._buildFooter(this._chatColumn);

        this._bodyRow.add_child(this._sidebarBox);
        this._bodyRow.add_child(this._chatColumn);
        this._container.add_child(this._bodyRow);
    }

    _buildMessageArea(parent) {
        this._messagesBox = new St.BoxLayout({
            style_class: 'bytey-messages',
            orientation: Clutter.Orientation.VERTICAL,
            x_expand: true,
        });

        this._scrollView = new St.ScrollView({
            style_class: 'bytey-scroll',
            hscrollbar_policy: St.PolicyType.NEVER,
            vscrollbar_policy: St.PolicyType.AUTOMATIC,
            enable_mouse_scrolling: true,
            overlay_scrollbars: false,
            x_expand: true,
            y_expand: true,
        });
        this._scrollView.set_child(this._messagesBox);

        parent.add_child(this._scrollView);
    }

    _buildFooter(parent) {
        const footer = new St.BoxLayout({
            style_class: 'bytey-drawer-footer',
            x_expand: true,
        });

        this._entry = new St.Entry({
            style_class: 'bytey-entry',
            hint_text: 'Ask Bytey...',
            can_focus: true,
            reactive: true,
            x_expand: true,
        });
        this._entry.clutter_text.set_single_line_mode(false);
        this._entry.clutter_text.set_line_wrap(true);
        this._entry.clutter_text.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR);

        this._entryScroll = new St.ScrollView({
            style_class: 'bytey-entry-scroll',
            hscrollbar_policy: St.PolicyType.NEVER,
            vscrollbar_policy: St.PolicyType.NEVER,
            x_expand: true,
            y_align: Clutter.ActorAlign.END,
        });
        const entryBox = new St.BoxLayout({
            vertical: true,
            x_expand: true,
        });
        entryBox.add_child(this._entry);
        this._entryScroll.set_child(entryBox);

        this._connect(this._entryScroll, 'button-press-event', () => {
            this._entry.clutter_text.grab_key_focus();
            return Clutter.EVENT_PROPAGATE;
        });

        this._connect(this._entry.clutter_text, 'key-focus-in', () => {
            this._entryScroll?.add_style_pseudo_class('focus');
        });
        this._connect(this._entry.clutter_text, 'key-focus-out', () => {
            this._entryScroll?.remove_style_pseudo_class('focus');
        });

        this._connect(this._entry.clutter_text, 'text-changed', () => {
            this._syncEntryHeight();
        });

        this._connect(this._entry.clutter_text, 'key-press-event', (_actor, event) => {
            const sym = event.get_key_symbol();
            if (sym === Clutter.KEY_Escape) {
                this.close();
                return Clutter.EVENT_STOP;
            }
            if (sym === Clutter.KEY_Return || sym === Clutter.KEY_KP_Enter) {
                if (event.get_state() & Clutter.ModifierType.SHIFT_MASK) {
                    const ct = this._entry.clutter_text;
                    ct.delete_selection();
                    ct.insert_unichar(0x0A);
                    return Clutter.EVENT_STOP;
                }
                this._submitEntry();
                return Clutter.EVENT_STOP;
            }
            return Clutter.EVENT_PROPAGATE;
        });

        footer.add_child(this._entryScroll);

        this._sendIcon = new St.Icon({
            icon_name: 'mail-send-symbolic',
            icon_size: DENSITY_ICON_SIZES[this._density].mic,
        });
        const sendBtn = new St.Button({
            style_class: 'bytey-send-btn',
            can_focus: true,
            y_align: Clutter.ActorAlign.END,
            child: this._sendIcon,
        });
        this._connect(sendBtn, 'clicked', () => this._submitEntry());
        footer.add_child(sendBtn);

        this._micIcon = new St.Icon({
            icon_name: 'audio-input-microphone-symbolic',
            icon_size: DENSITY_ICON_SIZES[this._density].mic,
        });
        this._micBtn = new St.Button({
            style_class: 'bytey-mic-btn',
            can_focus: true,
            y_align: Clutter.ActorAlign.END,
            child: this._micIcon,
        });
        this._connect(this._micBtn, 'clicked', () => {
            this._callbacks.onManualMic?.();
        });
        footer.add_child(this._micBtn);

        const gripLabel = new St.Label({
            style_class: 'bytey-resize-grip-label',
            text: '◢',
            x_align: Clutter.ActorAlign.CENTER,
            y_align: Clutter.ActorAlign.END,
        });
        this._resizeHandle = new St.Bin({
            style_class: 'bytey-resize-grip',
            reactive: true,
            track_hover: true,
            can_focus: false,
            y_align: Clutter.ActorAlign.END,
            child: gripLabel,
        });
        this._connect(this._resizeHandle, 'button-press-event', (_a, ev) => this._onResizePress(ev));
        this._connect(this._resizeHandle, 'motion-event', (_a, ev) => this._onResizeMotion(ev));
        this._connect(this._resizeHandle, 'button-release-event', (_a, ev) => this._onResizeRelease(ev));
        footer.add_child(this._resizeHandle);

        parent.add_child(footer);
        this._syncEntryHeight();
    }

    _syncEntryHeight() {
        if (this._destroyed || !this._entry || !this._entryScroll)
            return;
        const metrics = DENSITY_ENTRY_METRICS[this._density] ?? DENSITY_ENTRY_METRICS.comfortable;
        const minH = metrics.minHeight;
        const maxH = minH + (ENTRY_MAX_LINES - 1) * metrics.lineHeight;
        const scrollW = this._entryScroll.width;
        const availW = scrollW > 20
            ? scrollW - 4
            : Math.max(120, (this._drawerWidth || DRAWER_WIDTH_FALLBACK) - 130);
        const [, natH] = this._entry.get_preferred_height(availW);
        const targetH = Math.min(maxH, Math.max(minH, Math.ceil(natH)));
        this._entryScroll.height = targetH;
        this._entryScroll.vscrollbar_policy = natH > maxH
            ? St.PolicyType.AUTOMATIC
            : St.PolicyType.NEVER;
    }

    _submitEntry() {
        const text = (this._entry.text ?? '').trim();
        if (!text)
            return;
        this._entry.text = '';
        this._syncEntryHeight();
        this._callbacks.onSendText?.(text);
    }

    /**
     * Update daemon connection status indicator.
     *
     * @param {'connected'|'error'|'disconnected'|boolean} status
     */
    setConnectionStatus(status) {
        if (this._destroyed)
            return;

        let nextState = 'disconnected';
        if (typeof status === 'boolean') {
            nextState = status ? 'connected' : 'disconnected';
        } else if (typeof status === 'string') {
            const norm = status.toLowerCase().trim();
            if (norm === 'connected' || norm === 'online')
                nextState = 'connected';
            else if (norm === 'error' || norm === 'connection_error')
                nextState = 'error';
            else
                nextState = 'disconnected';
        }

        this._connectionState = nextState;
        this._online = nextState === 'connected';

        if (this._statusDot) {
            for (const state of CONNECTION_STATES)
                this._statusDot.remove_style_class_name(`bytey-status-${state}`);
            this._statusDot.add_style_class_name(`bytey-status-${nextState}`);
        }

        const label = CONNECTION_STATUS_LABELS[nextState];
        if (this._statusIndicator)
            this._statusIndicator.accessible_name = label;
        if (this._statusTooltip?.visible)
            this._showStatusTooltip();
    }

    _showStatusTooltip() {
        if (this._destroyed || !this._isOpen || !this._statusTooltip || !this._statusIndicator)
            return;
        const label = CONNECTION_STATUS_LABELS[this._connectionState] ?? CONNECTION_STATUS_LABELS.disconnected;
        this._statusTooltip.text = label;
        this._statusTooltip.visible = true;
        this._statusTooltip.get_parent()?.set_child_above_sibling(this._statusTooltip, null);

        const [ix, iy] = this._statusIndicator.get_transformed_position();
        const [iw, ih] = this._statusIndicator.get_transformed_size();
        const [, tipW] = this._statusTooltip.get_preferred_width(-1);
        const [, tipH] = this._statusTooltip.get_preferred_height(-1);
        const w = Math.max(1, Math.ceil(tipW || 72));
        const h = Math.max(1, Math.ceil(tipH || 24));

        const rawX = Math.round(ix + iw / 2 - w / 2);
        const aboveY = Math.round(iy - h - 6);
        const area = this._positionManager.getWorkArea(this._monitorIndex);
        const targetY = aboveY < area.y ? Math.round(iy + ih + 6) : aboveY;
        const clamped = this._positionManager.clampToMonitor(rawX, targetY, w, h, this._monitorIndex);
        this._statusTooltip.set_position(clamped.x, clamped.y);
    }

    _hideStatusTooltip() {
        if (this._statusTooltip)
            this._statusTooltip.visible = false;
    }

    /**
     * Reflect active listening state on the footer Mic button.
     *
     * @param {boolean} listening
     */
    setListening(listening) {
        if (this._destroyed)
            return;
        this._listening = Boolean(listening);
        if (this._listening)
            this._micBtn.add_style_class_name('bytey-mic-btn-active');
        else
            this._micBtn.remove_style_class_name('bytey-mic-btn-active');
    }

    /**
     * Update active provider/model pill and companion display name from daemon config.
     *
     * @param {object} config
     */
    setConfig(config) {
        if (this._destroyed || !config)
            return;

        const petName = config.general?.pet_name ?? config.pet_name;
        if (typeof petName === 'string' && petName.trim())
            this._petName = petName.trim();
        if (this._entry)
            this._entry.hint_text = `Ask ${this._petName}...`;

        const provider = config.provider?.active_provider ??
            config.llm?.provider ??
            config.llm_provider ??
            'ollama';
        const model = config.provider?.model ??
            config.llm?.model ??
            config.model ??
            '';
        const monIdx = config.visuals?.monitor_index;
        if (Number.isInteger(monIdx))
            this._monitorIndex = monIdx;

        this._providerLabel.text = model ? `${provider} · ${model}` : String(provider);

        const rawWidth = Number(config.visuals?.chat_width ?? config.chat_width);
        const rawHeight = Number(config.visuals?.chat_height ?? config.chat_height);
        let sizeChanged = false;
        if (Number.isFinite(rawWidth) && rawWidth >= DRAWER_MIN_WIDTH && !this._resizing) {
            const clampedW = Math.max(DRAWER_MIN_WIDTH, Math.min(DRAWER_MAX_WIDTH, Math.round(rawWidth)));
            if (clampedW !== this._drawerWidth) {
                this._drawerWidth = clampedW;
                sizeChanged = true;
            }
        }
        if (Number.isFinite(rawHeight) && rawHeight >= DRAWER_MIN_HEIGHT && !this._resizing) {
            const clampedH = Math.max(DRAWER_MIN_HEIGHT, Math.min(DRAWER_MAX_HEIGHT, Math.round(rawHeight)));
            if (clampedH !== this._drawerHeight) {
                this._drawerHeight = clampedH;
                sizeChanged = true;
            }
        }
        if (sizeChanged && this._container)
            this._applyDrawerSize();

        const rawDensity = String(config.visuals?.chat_density ?? config.chat_density ?? '').toLowerCase().trim();
        if (DENSITY_ORDER.includes(rawDensity))
            this._applyDensity(rawDensity, false);
    }

    _cycleDensity() {
        const idx = DENSITY_ORDER.indexOf(this._density);
        const nextDensity = DENSITY_ORDER[(idx + 1) % DENSITY_ORDER.length];
        this._applyDensity(nextDensity, true);
    }

    _applyDensity(density, notifyCallback = false) {
        if (this._destroyed || !this._container || !DENSITY_ORDER.includes(density))
            return;
        this._density = density;
        for (const mode of DENSITY_ORDER)
            this._container.remove_style_class_name(`bytey-density-${mode}`);
        this._container.add_style_class_name(`bytey-density-${density}`);

        if (this._densityBtn)
            this._densityBtn.label = DENSITY_LABELS[density] ?? 'Comfort';

        const sizes = DENSITY_ICON_SIZES[density] ?? DENSITY_ICON_SIZES.comfortable;
        if (this._closeIcon)
            this._closeIcon.icon_size = sizes.close;
        if (this._sidebarToggleIcon)
            this._sidebarToggleIcon.icon_size = sizes.close;
        if (this._sidebarNewIcon)
            this._sidebarNewIcon.icon_size = sizes.close;
        if (this._sendIcon)
            this._sendIcon.icon_size = sizes.mic;
        if (this._micIcon)
            this._micIcon.icon_size = sizes.mic;
        this._syncEntryHeight();
        for (const rec of this._messageWidgets.values()) {
            if (rec.replayIcon)
                rec.replayIcon.icon_size = sizes.replay;
        }
        this._renderSessions();

        if (notifyCallback)
            this._callbacks.onDensityChange?.(density);
    }

    _onResizePress(event) {
        if (event.get_button() !== Clutter.BUTTON_PRIMARY || this._destroyed || !this._container)
            return Clutter.EVENT_PROPAGATE;

        this._hideStatusTooltip();
        this._resizing = true;
        this._resizeStartCoords = event.get_coords();
        this._resizeStartSize = [this._drawerWidth, this._drawerHeight];
        this._releaseResizeGrab();
        try {
            this._resizeGrab = global.stage.grab(this._resizeHandle);
        } catch (_e) {
            this._resizeGrab = null;
        }
        this._container.add_style_class_name('bytey-drawer-resizing');
        return Clutter.EVENT_STOP;
    }

    _onResizeMotion(event) {
        if (!this._resizing || this._destroyed || !this._container)
            return Clutter.EVENT_PROPAGATE;

        const [curX, curY] = event.get_coords();
        const dx = curX - this._resizeStartCoords[0];
        const dy = curY - this._resizeStartCoords[1];

        const b = this._lastIslandBounds;
        const inLowerHalf = this._positionManager.isInLowerHalf(b.y, b.height, this._monitorIndex);
        const heightDelta = inLowerHalf ? -dy : dy;

        const nextW = Math.max(
            DRAWER_MIN_WIDTH,
            Math.min(DRAWER_MAX_WIDTH, Math.round(this._resizeStartSize[0] + dx * 1.5))
        );
        const nextH = Math.max(
            DRAWER_MIN_HEIGHT,
            Math.min(DRAWER_MAX_HEIGHT, Math.round(this._resizeStartSize[1] + heightDelta))
        );

        if (nextW !== this._drawerWidth || nextH !== this._drawerHeight) {
            this._drawerWidth = nextW;
            this._drawerHeight = nextH;
            this._applyDrawerSize();
        }
        return Clutter.EVENT_STOP;
    }

    _onResizeRelease(event) {
        if (!this._resizing || event.get_button() !== Clutter.BUTTON_PRIMARY)
            return Clutter.EVENT_PROPAGATE;

        this._resizing = false;
        this._releaseResizeGrab();
        this._container?.remove_style_class_name('bytey-drawer-resizing');
        this._callbacks.onResizeCommit?.(this._drawerWidth, this._drawerHeight);
        return Clutter.EVENT_STOP;
    }

    _releaseResizeGrab() {
        if (this._resizeGrab) {
            try {
                this._resizeGrab.dismiss();
            } catch (_e) {
                // Ignore if grab was already dismissed.
            }
            this._resizeGrab = null;
        }
    }

    /**
     * Replace transcript view with a full list of messages from SQLite history.
     *
     * @param {Array<object>} messages
     */
    setMessages(messages) {
        if (this._destroyed)
            return;

        this._messages = [];
        this._messageWidgets.clear();
        this._toolWidgets.clear();
        this._emptyStateActor = null;
        this._messagesBox.destroy_all_children();

        const list = Array.isArray(messages) ? messages : [];
        if (list.length === 0) {
            this._renderEmptyState();
            return;
        }

        for (const raw of list) {
            const msg = this._normalizeMessage(raw);
            this._messages.push(msg);
            this._appendMessageWidget(msg);
        }

        this._scheduleScrollToBottom();
    }

    /**
     * Load a full list of messages into the transcript view.
     *
     * @param {Array<object>} messages
     */
    loadMessages(messages) {
        this.setMessages(messages);
    }

    /**
     * Toggle or explicitly set the chat history sidebar visibility.
     *
     * @param {boolean} [forceState]
     */
    toggleSidebar(forceState) {
        if (this._destroyed)
            return;

        const nextOpen = typeof forceState === 'boolean' ? forceState : !this._sidebarOpen;
        this._sidebarOpen = nextOpen;

        if (this._sidebarToggleBtn) {
            if (this._sidebarOpen)
                this._sidebarToggleBtn.add_style_class_name('bytey-sidebar-toggle-active');
            else
                this._sidebarToggleBtn.remove_style_class_name('bytey-sidebar-toggle-active');
        }

        this._sidebarBox?.remove_all_transitions();
        this._container?.remove_all_transitions();

        if (!this._isOpen || !this._container) {
            if (this._sidebarBox) {
                this._sidebarBox.visible = nextOpen;
                this._sidebarBox.width = SIDEBAR_WIDTH;
                this._sidebarBox.opacity = 255;
            }
            this._applyDrawerSize();
            this._syncEntryHeight();
            this._callbacks.onToggleSidebar?.(this._sidebarOpen);
            return;
        }

        const {width: targetW, height: targetH} = this._getEffectiveSize();
        const b = this._lastIslandBounds;
        const clamped = this._computeClampedPosition(
            targetW,
            targetH,
            b.x,
            b.y,
            b.width,
            b.height,
            this._monitorIndex
        );

        if (this._sidebarBox) {
            if (nextOpen) {
                if (!this._sidebarBox.visible) {
                    this._sidebarBox.width = 0;
                    this._sidebarBox.opacity = 0;
                    this._sidebarBox.visible = true;
                }
                this._sidebarBox.ease({
                    width: SIDEBAR_WIDTH,
                    opacity: 255,
                    duration: SIDEBAR_ANIM_DURATION_MS,
                    mode: Clutter.AnimationMode.EASE_OUT_QUAD,
                });
            } else if (this._sidebarBox.visible) {
                this._sidebarBox.ease({
                    width: 0,
                    opacity: 0,
                    duration: SIDEBAR_ANIM_DURATION_MS,
                    mode: Clutter.AnimationMode.EASE_OUT_QUAD,
                    onComplete: () => {
                        if (this._destroyed || this._sidebarOpen !== false || !this._sidebarBox)
                            return;
                        this._sidebarBox.visible = false;
                        this._sidebarBox.width = SIDEBAR_WIDTH;
                        this._sidebarBox.opacity = 255;
                    },
                });
            }
        }

        this._container.ease({
            width: targetW,
            x: clamped.x,
            duration: SIDEBAR_ANIM_DURATION_MS,
            mode: Clutter.AnimationMode.EASE_OUT_QUAD,
            onComplete: () => {
                if (this._destroyed || this._sidebarOpen !== nextOpen)
                    return;
                this._syncEntryHeight();
            },
        });

        this._syncEntryHeight();
        this._callbacks.onToggleSidebar?.(this._sidebarOpen);
    }

    /**
     * Populate the sidebar sessions list and highlight the active session.
     *
     * @param {Array<object>} sessions
     * @param {string|null} [activeSessionId]
     */
    setSessions(sessions, activeSessionId = this._activeSessionId) {
        if (this._destroyed)
            return;
        this._sessions = Array.isArray(sessions) ? sessions : [];
        if (activeSessionId !== undefined)
            this._activeSessionId = activeSessionId;
        this._renderSessions();
    }

    /**
     * Focus the text entry on the next idle tick when the drawer is open.
     */
    focusInput() {
        if (this._destroyed || !this._isOpen || !this._entry)
            return;
        if (this._focusIdleId) {
            GLib.Source.remove(this._focusIdleId);
            this._focusIdleId = 0;
        }
        this._focusIdleId = GLib.idle_add(GLib.PRIORITY_DEFAULT_IDLE, () => {
            this._focusIdleId = 0;
            if (!this._destroyed && this._isOpen && this._entry)
                this._entry.clutter_text.grab_key_focus();
            return GLib.SOURCE_REMOVE;
        });
    }

    _getEffectiveSize() {
        const area = this._positionManager.getWorkArea(this._monitorIndex);
        const maxAvailW = Math.max(DRAWER_MIN_WIDTH, (area?.width ?? 1920) - 16);
        const maxAvailH = Math.max(DRAWER_MIN_HEIGHT, (area?.height ?? 1080) - 16);

        let targetW = this._drawerWidth;
        if (this._sidebarOpen) {
            // Split inside wide drawers (>= 520px), otherwise expand by sidebar width.
            targetW = this._drawerWidth >= 520
                ? this._drawerWidth
                : this._drawerWidth + SIDEBAR_WIDTH;
        }

        return {
            width: Math.min(maxAvailW, Math.max(DRAWER_MIN_WIDTH, targetW)),
            height: Math.min(maxAvailH, Math.max(DRAWER_MIN_HEIGHT, this._drawerHeight)),
        };
    }

    _applyDrawerSize() {
        if (this._destroyed || !this._container)
            return;
        const {width, height} = this._getEffectiveSize();
        this._container.set_size(width, height);
        if (this._isOpen) {
            const b = this._lastIslandBounds;
            this.updatePosition(b.x, b.y, b.width, b.height, this._monitorIndex);
        }
    }

    _renderSessions() {
        if (this._destroyed || !this._sessionsList)
            return;

        if (this._renameFocusIdleId) {
            GLib.Source.remove(this._renameFocusIdleId);
            this._renameFocusIdleId = 0;
        }

        this._sessionsList.destroy_all_children();

        if (this._sessions.length === 0) {
            const empty = new St.Label({
                style_class: 'bytey-sidebar-empty',
                text: 'No saved chats',
                x_expand: true,
                x_align: Clutter.ActorAlign.CENTER,
            });
            this._sessionsList.add_child(empty);
            return;
        }

        const iconSizes = DENSITY_ICON_SIZES[this._density] ?? DENSITY_ICON_SIZES.comfortable;

        for (const session of this._sessions) {
            if (!session || !session.id)
                continue;
            const sid = String(session.id);
            const isActive = sid === this._activeSessionId;
            const isEditing = sid === this._editingSessionId;
            const isPendingDelete = sid === this._pendingDeleteSessionId;

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
                text: this._formatSessionSubtitle(session),
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
                    if (this._destroyed)
                        return;
                    const newTitle = titleEntry.get_text().trim();
                    this._editingSessionId = null;
                    if (newTitle && newTitle !== currentTitle.trim())
                        this._callbacks.onRenameSession?.(sid, newTitle);
                    this._renderSessions();
                });
                titleEntry.connect('key-press-event', (_actor, event) => {
                    if (event.get_key_symbol() === Clutter.KEY_Escape) {
                        this._editingSessionId = null;
                        this._renderSessions();
                        return Clutter.EVENT_STOP;
                    }
                    return Clutter.EVENT_PROPAGATE;
                });

                textCol.add_child(titleEntry);
                textCol.add_child(subLabel);
                row.add_child(textCol);

                this._renameFocusIdleId = GLib.idle_add(GLib.PRIORITY_DEFAULT_IDLE, () => {
                    this._renameFocusIdleId = 0;
                    if (!this._destroyed && this._editingSessionId === sid && titleEntry.clutter_text) {
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
                    this._callbacks.onSelectSession?.(sid);
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
                        icon_size: iconSizes.replay,
                    }),
                });
                confirmBtn.connect('clicked', () => {
                    this._pendingDeleteSessionId = null;
                    this._callbacks.onDeleteSession?.(sid);
                    this._renderSessions();
                });

                const cancelBtn = new St.Button({
                    style_class: 'bytey-session-cancel-delete-btn',
                    can_focus: true,
                    y_align: Clutter.ActorAlign.CENTER,
                    accessible_name: 'Cancel delete chat',
                    child: new St.Icon({
                        icon_name: 'window-close-symbolic',
                        icon_size: iconSizes.replay,
                    }),
                });
                cancelBtn.connect('clicked', () => {
                    this._pendingDeleteSessionId = null;
                    this._renderSessions();
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
                        icon_size: iconSizes.replay,
                    }),
                });
                editBtn.connect('clicked', () => {
                    this._editingSessionId = sid;
                    this._pendingDeleteSessionId = null;
                    this._renderSessions();
                });

                const delBtn = new St.Button({
                    style_class: 'bytey-session-delete-btn',
                    can_focus: true,
                    y_align: Clutter.ActorAlign.CENTER,
                    accessible_name: 'Delete chat',
                    child: new St.Icon({
                        icon_name: 'user-trash-symbolic',
                        icon_size: iconSizes.replay,
                    }),
                });
                delBtn.connect('clicked', () => {
                    this._pendingDeleteSessionId = sid;
                    this._editingSessionId = null;
                    this._renderSessions();
                });

                row.add_child(editBtn);
                row.add_child(delBtn);
            }

            this._sessionsList.add_child(row);
        }
    }

    _formatSessionSubtitle(session) {
        const rel = this._formatRelativeTime(session.updated_at ?? session.created_at);
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

    _formatRelativeTime(rawTs) {
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

    /**
     * Clear all displayed messages immediately.
     */
    clearMessages() {
        this.setMessages([]);
    }

    /**
     * Apply an incoming WebSocket `transcript_delta` event.
     *
     * @param {object} event
     */
    upsertMessageDelta(event) {
        if (this._destroyed || !event)
            return;

        const messageId = event.message_id ?? event.id ?? `msg-${Date.now()}`;
        const role = (event.role ?? 'assistant').toLowerCase();
        const source = event.source ?? event.input_mode ?? 'text';
        const emotion = event.emotion ?? null;
        const isFinal = Boolean(event.is_final);
        const incomingDelta = typeof event.delta === 'string' ? event.delta : null;
        const incomingText = typeof event.text === 'string'
            ? event.text
            : (typeof event.content === 'string' ? event.content : '');

        let record = this._messageWidgets.get(messageId);
        if (!record) {
            this._clearEmptyState();

            const initialText = incomingDelta !== null ? incomingDelta : incomingText;
            const msg = this._normalizeMessage({
                id: messageId,
                role,
                input_mode: source,
                content: initialText,
                emotion: emotion ?? 'neutral',
                tool_calls: event.tool_calls ?? [],
            });
            this._messages.push(msg);
            record = this._appendMessageWidget(msg);
        } else {
            const existing = record.msg.content ?? '';
            let nextContent = existing;

            if (incomingDelta !== null) {
                nextContent = existing + incomingDelta;
            } else if (isFinal) {
                if (incomingText.length > 0) {
                    const looksLikeShortTail = existing.length > 16 &&
                        incomingText.length < existing.length / 2 &&
                        !incomingText.startsWith(existing.slice(0, Math.min(8, existing.length)));
                    nextContent = looksLikeShortTail ? (existing + incomingText) : incomingText;
                }
            } else if (incomingText.length > 0) {
                if (existing.length >= 2 && incomingText.startsWith(existing))
                    nextContent = incomingText;
                else
                    nextContent = existing + incomingText;
            }

            record.msg.content = nextContent;
            record.bodyLabel.text = nextContent;
            record.bodyLabel.visible = nextContent.length > 0;
            if (emotion)
                this._updateEmotionBadge(record, emotion);
            if (record.replayBtn)
                record.replayBtn.visible = nextContent.trim().length > 0;
        }

        this._scheduleScrollToBottom();
    }

    /**
     * Update or attach a tool-call pill from a `tool_call_status` WebSocket event.
     *
     * @param {object} event
     */
    updateToolCall(event) {
        if (this._destroyed || !event)
            return;

        const tool = this._normalizeTool(event);
        if (!tool.id)
            return;

        const existingRecord = this._toolWidgets.get(tool.id);
        if (existingRecord) {
            Object.assign(existingRecord.tool, tool);
            this._refreshToolWidget(existingRecord);
            this._scheduleScrollToBottom();
            return;
        }

        // Attach to specified message_id or trailing assistant message (creating one if needed).
        let msgRecord = tool.messageId ? this._messageWidgets.get(tool.messageId) : null;
        if (!msgRecord && !tool.messageId) {
            const lastMsg = this._messages[this._messages.length - 1];
            if (lastMsg && lastMsg.role === 'assistant')
                msgRecord = this._messageWidgets.get(lastMsg.id);
        }
        if (!msgRecord) {
            const targetId = tool.messageId ?? `tool-msg-${tool.id}`;
            this.upsertMessageDelta({
                message_id: targetId,
                role: 'assistant',
                source: 'text',
                text: '',
                is_final: false,
            });
            msgRecord = this._messageWidgets.get(targetId);
        }

        if (msgRecord) {
            msgRecord.msg.tool_calls.push(tool);
            this._appendToolWidget(msgRecord.toolBox, tool);
            this._scheduleScrollToBottom();
        }
    }

    /**
     * Position drawer anchored directly below or above the island actor.
     *
     * @param {number} islandX
     * @param {number} islandY
     * @param {number} islandW
     * @param {number} islandH
     * @param {number} [monitorIndex]
     */
    _computeClampedPosition(drawerW, drawerH, islandX, islandY, islandW, islandH, monitorIndex) {
        const inLowerHalf = this._positionManager.isInLowerHalf(islandY, islandH, monitorIndex);
        const targetX = Math.round(islandX + islandW / 2 - drawerW / 2);
        const targetY = inLowerHalf
            ? Math.round(islandY - drawerH - DRAWER_GAP_PX)
            : Math.round(islandY + islandH + DRAWER_GAP_PX);

        return this._positionManager.clampToMonitor(
            targetX,
            targetY,
            drawerW,
            drawerH,
            monitorIndex
        );
    }

    updatePosition(islandX, islandY, islandW, islandH, monitorIndex = this._monitorIndex) {
        if (this._destroyed || !this._container)
            return;

        this._monitorIndex = monitorIndex;
        this._lastIslandBounds = {x: islandX, y: islandY, width: islandW, height: islandH};
        const {width: drawerW, height: drawerH} = this._getEffectiveSize();
        this._container.set_size(drawerW, drawerH);

        const clamped = this._computeClampedPosition(
            drawerW,
            drawerH,
            islandX,
            islandY,
            islandW,
            islandH,
            monitorIndex
        );
        this._container.set_position(clamped.x, clamped.y);
    }

    /**
     * Open the transcript drawer anchored to the given island geometry.
     *
     * @param {number} islandX
     * @param {number} islandY
     * @param {number} islandW
     * @param {number} islandH
     * @param {number} [monitorIndex]
     */
    open(islandX, islandY, islandW, islandH, monitorIndex = this._monitorIndex) {
        if (this._destroyed || !this._container)
            return;

        this._isOpen = true;
        this._container.visible = true;
        this.updatePosition(islandX, islandY, islandW, islandH, monitorIndex);

        this._scheduleScrollToBottom();
        if (this._focusIdleId) {
            GLib.Source.remove(this._focusIdleId);
            this._focusIdleId = 0;
        }
        this._focusIdleId = GLib.idle_add(GLib.PRIORITY_DEFAULT_IDLE, () => {
            this._focusIdleId = 0;
            if (!this._destroyed && this._isOpen && this._entry)
                this._entry.clutter_text.grab_key_focus();
            return GLib.SOURCE_REMOVE;
        });
        this._callbacks.onOpenChanged?.(true);
    }

    /**
     * Hide the transcript drawer.
     */
    close() {
        if (this._destroyed || !this._isOpen)
            return;
        this._isOpen = false;
        this._pendingDeleteSessionId = null;
        this._editingSessionId = null;
        this._hideStatusTooltip();
        if (this._focusIdleId) {
            GLib.Source.remove(this._focusIdleId);
            this._focusIdleId = 0;
        }
        if (this._renameFocusIdleId) {
            GLib.Source.remove(this._renameFocusIdleId);
            this._renameFocusIdleId = 0;
        }
        if (this._container)
            this._container.visible = false;
        this._callbacks.onOpenChanged?.(false);
    }

    /**
     * Toggle drawer open/closed.
     *
     * @param {number} islandX
     * @param {number} islandY
     * @param {number} islandW
     * @param {number} islandH
     * @param {number} [monitorIndex]
     */
    toggle(islandX, islandY, islandW, islandH, monitorIndex = this._monitorIndex) {
        if (this._isOpen)
            this.close();
        else
            this.open(islandX, islandY, islandW, islandH, monitorIndex);
    }

    /**
     * Clean up signals, idle timers, and chrome actor.
     */
    destroy() {
        if (this._destroyed)
            return;
        this._destroyed = true;
        this._isOpen = false;
        this._resizing = false;
        this._releaseResizeGrab();

        if (this._scrollIdleId) {
            GLib.Source.remove(this._scrollIdleId);
            this._scrollIdleId = 0;
        }
        if (this._focusIdleId) {
            GLib.Source.remove(this._focusIdleId);
            this._focusIdleId = 0;
        }
        if (this._renameFocusIdleId) {
            GLib.Source.remove(this._renameFocusIdleId);
            this._renameFocusIdleId = 0;
        }
        this._pendingDeleteSessionId = null;
        this._editingSessionId = null;
        this._sidebarBox?.remove_all_transitions();
        this._container?.remove_all_transitions();

        for (const [obj, id] of this._signalIds) {
            try {
                obj.disconnect(id);
            } catch (_e) {
                // Ignore if actor was already disposed.
            }
        }
        this._signalIds = [];
        this._messageWidgets.clear();
        this._toolWidgets.clear();
        this._messages = [];
        this._emptyStateActor = null;

        if (this._statusTooltip) {
            Main.layoutManager.removeChrome(this._statusTooltip);
            this._statusTooltip.destroy();
            this._statusTooltip = null;
        }

        if (this._container) {
            Main.layoutManager.removeChrome(this._container);
            this._container.destroy();
            this._container = null;
        }
        this._messagesBox = null;
        this._scrollView = null;
        this._bodyRow = null;
        this._sidebarBox = null;
        this._sidebarToggleBtn = null;
        this._sidebarToggleIcon = null;
        this._sidebarNewIcon = null;
        this._sessionsScroll = null;
        this._sessionsList = null;
        this._chatColumn = null;
        this._sessions = [];
        this._activeSessionId = null;
        this._entry = null;
        this._entryScroll = null;
        this._sendIcon = null;
        this._providerLabel = null;
        this._densityBtn = null;
        this._closeIcon = null;
        this._micBtn = null;
        this._micIcon = null;
        this._resizeHandle = null;
        this._positionManager = null;
        this._callbacks = null;

        this._statusIndicator = null;
        this._statusDot = null;
        this._providerPill = null;
    }

    _clearEmptyState() {
        if (this._emptyStateActor) {
            try {
                if (this._emptyStateActor.get_parent() === this._messagesBox)
                    this._messagesBox.remove_child(this._emptyStateActor);
                this._emptyStateActor.destroy();
            } catch (_e) {
                // Ignore if already disposed.
            }
            this._emptyStateActor = null;
        }
    }

    _renderEmptyState() {
        if (this._destroyed || !this._messagesBox || this._emptyStateActor)
            return;
        const emptyLabel = new St.Label({
            style_class: 'bytey-empty-state',
            text: 'No messages yet. Speak your wake phrase or type below.',
            x_expand: true,
            x_align: Clutter.ActorAlign.CENTER,
        });
        emptyLabel.clutter_text.line_wrap = true;
        emptyLabel.clutter_text.line_wrap_mode = Pango.WrapMode.WORD_CHAR;
        emptyLabel.clutter_text.ellipsize = Pango.EllipsizeMode.NONE;
        this._emptyStateActor = emptyLabel;
        this._messagesBox.add_child(emptyLabel);
    }

    _normalizeMessage(raw) {
        const id = String(raw.id ?? raw.message_id ?? `msg-${Date.now()}-${Math.random()}`);
        const role = String(raw.role ?? 'assistant').toLowerCase() === 'user' ? 'user' : 'assistant';
        const inputMode = String(raw.input_mode ?? raw.source ?? 'text').toLowerCase();
        const content = String(raw.content ?? raw.text ?? '');
        const emotion = String(raw.emotion ?? 'neutral').toLowerCase();
        const rawTools = Array.isArray(raw.tool_calls) ? raw.tool_calls : [];
        const toolCalls = rawTools.map(t => this._normalizeTool({...t, message_id: id}));
        return {id, role, input_mode: inputMode, content, emotion, tool_calls: toolCalls};
    }

    _normalizeTool(raw) {
        let argsText = '';
        if (typeof raw.arguments_json === 'string') {
            argsText = raw.arguments_json.trim();
        } else if (raw.arguments && typeof raw.arguments === 'object') {
            try {
                argsText = JSON.stringify(raw.arguments, null, 2);
            } catch (_e) {
                argsText = '';
            }
        }
        return {
            id: raw.call_id ?? raw.id ? String(raw.call_id ?? raw.id) : '',
            messageId: raw.message_id ? String(raw.message_id) : null,
            server: String(raw.server ?? raw.server_name ?? 'mcp'),
            tool: String(raw.tool ?? raw.tool_name ?? 'tool'),
            status: String(raw.status ?? 'running').toLowerCase(),
            argumentsText: argsText === '{}' ? '' : argsText,
            summary: String(raw.summary ?? raw.result_summary ?? ''),
        };
    }

    _appendMessageWidget(msg) {
        this._clearEmptyState();

        const isUser = msg.role === 'user';
        const bubble = new St.BoxLayout({
            style_class: isUser ? 'bytey-msg-user' : 'bytey-msg-assistant',
            orientation: Clutter.Orientation.VERTICAL,
            x_expand: true,
        });

        const metaRow = new St.BoxLayout({
            style_class: 'bytey-msg-meta',
            x_expand: true,
        });

        const roleLabel = new St.Label({
            style_class: 'bytey-msg-role',
            text: isUser ? 'You' : this._petName,
            y_align: Clutter.ActorAlign.CENTER,
        });
        metaRow.add_child(roleLabel);

        if (isUser) {
            const isVoice = msg.input_mode === 'voice';
            const srcBadge = new St.Label({
                style_class: `bytey-source-badge ${isVoice ? 'bytey-source-voice' : 'bytey-source-text'}`,
                text: isVoice ? 'Voice' : 'Text',
                y_align: Clutter.ActorAlign.CENTER,
            });
            metaRow.add_child(srcBadge);
        }

        const emotionLabel = new St.Label({
            style_class: 'bytey-emotion-tag',
            text: msg.emotion,
            visible: !isUser && msg.emotion && msg.emotion !== 'neutral',
            y_align: Clutter.ActorAlign.CENTER,
        });
        metaRow.add_child(emotionLabel);

        const spacer = new St.Widget({x_expand: true});
        metaRow.add_child(spacer);

        let replayBtn = null;
        let replayIcon = null;
        if (!isUser) {
            const iconSizes = DENSITY_ICON_SIZES[this._density] ?? DENSITY_ICON_SIZES.comfortable;
            replayIcon = new St.Icon({
                icon_name: 'audio-volume-high-symbolic',
                icon_size: iconSizes.replay,
            });
            replayBtn = new St.Button({
                style_class: 'bytey-replay-btn',
                can_focus: true,
                visible: msg.content.trim().length > 0,
                y_align: Clutter.ActorAlign.CENTER,
                child: replayIcon,
            });
            replayBtn.connect('clicked', () => {
                const text = (msg.content ?? '').trim();
                if (text)
                    this._callbacks.onReplayTts?.(msg.id, text);
            });
            metaRow.add_child(replayBtn);
        }

        bubble.add_child(metaRow);

        const bodyLabel = new St.Label({
            style_class: 'bytey-msg-text',
            text: msg.content,
            visible: msg.content.length > 0,
            x_expand: true,
        });
        bodyLabel.clutter_text.line_wrap = true;
        bodyLabel.clutter_text.line_wrap_mode = Pango.WrapMode.WORD_CHAR;
        bodyLabel.clutter_text.ellipsize = Pango.EllipsizeMode.NONE;
        bubble.add_child(bodyLabel);

        const toolBox = new St.BoxLayout({
            style_class: 'bytey-tool-list',
            orientation: Clutter.Orientation.VERTICAL,
            x_expand: true,
            visible: msg.tool_calls.length > 0,
        });
        bubble.add_child(toolBox);

        for (const tool of msg.tool_calls)
            this._appendToolWidget(toolBox, tool);

        this._messagesBox.add_child(bubble);

        const record = {msg, box: bubble, bodyLabel, emotionLabel, toolBox, replayBtn, replayIcon};
        this._messageWidgets.set(msg.id, record);
        return record;
    }

    _updateEmotionBadge(record, emotion) {
        const clean = String(emotion ?? 'neutral').toLowerCase();
        record.msg.emotion = clean;
        record.emotionLabel.text = clean;
        record.emotionLabel.visible = record.msg.role !== 'user' && clean !== 'neutral';
    }

    _appendToolWidget(toolBox, tool) {
        toolBox.visible = true;

        const badge = new St.BoxLayout({
            style_class: `bytey-tool-badge bytey-tool-${tool.status}`,
            orientation: Clutter.Orientation.VERTICAL,
            x_expand: true,
        });

        const headerBtn = new St.Button({
            style_class: 'bytey-tool-header-btn',
            x_expand: true,
            can_focus: true,
        });

        const headerRow = new St.BoxLayout({
            style_class: 'bytey-tool-header',
            x_expand: true,
        });
        const titleLabel = new St.Label({
            style_class: 'bytey-tool-title',
            text: `${tool.server} · ${tool.tool}`,
            x_expand: true,
            x_align: Clutter.ActorAlign.START,
            y_align: Clutter.ActorAlign.CENTER,
        });
        const statusLabel = new St.Label({
            style_class: 'bytey-tool-status-chip',
            text: this._formatToolStatus(tool.status),
            y_align: Clutter.ActorAlign.CENTER,
        });
        headerRow.add_child(titleLabel);
        headerRow.add_child(statusLabel);
        headerBtn.set_child(headerRow);
        badge.add_child(headerBtn);

        const detailsLabel = new St.Label({
            style_class: 'bytey-tool-details',
            text: this._formatToolDetails(tool),
            visible: tool.status === 'awaiting_confirm',
            x_expand: true,
        });
        detailsLabel.clutter_text.line_wrap = true;
        detailsLabel.clutter_text.line_wrap_mode = Pango.WrapMode.WORD_CHAR;
        detailsLabel.clutter_text.ellipsize = Pango.EllipsizeMode.NONE;
        badge.add_child(detailsLabel);

        const confirmRow = new St.BoxLayout({
            style_class: 'bytey-confirm-row',
            visible: tool.status === 'awaiting_confirm',
            x_expand: true,
        });
        const allowBtn = new St.Button({
            style_class: 'bytey-btn-allow',
            label: 'Allow',
            can_focus: true,
        });
        const denyBtn = new St.Button({
            style_class: 'bytey-btn-deny',
            label: 'Deny',
            can_focus: true,
        });
        confirmRow.add_child(allowBtn);
        confirmRow.add_child(denyBtn);
        badge.add_child(confirmRow);

        const record = {
            tool,
            badge,
            titleLabel,
            statusLabel,
            detailsLabel,
            confirmRow,
            allowBtn,
            denyBtn,
            expanded: tool.status === 'awaiting_confirm',
        };

        headerBtn.connect('clicked', () => {
            record.expanded = !record.expanded;
            detailsLabel.visible = record.expanded && detailsLabel.text.length > 0;
        });

        allowBtn.connect('clicked', () => {
            allowBtn.reactive = false;
            denyBtn.reactive = false;
            record.tool.status = 'running';
            this._refreshToolWidget(record);
            this._callbacks.onConfirmTool?.(record.tool.id, true);
        });

        denyBtn.connect('clicked', () => {
            allowBtn.reactive = false;
            denyBtn.reactive = false;
            record.tool.status = 'error';
            record.tool.summary = 'Denied by user';
            this._refreshToolWidget(record);
            this._callbacks.onConfirmTool?.(record.tool.id, false);
        });

        this._toolWidgets.set(tool.id, record);
        this._refreshToolWidget(record);
        toolBox.add_child(badge);
    }

    _refreshToolWidget(record) {
        const {tool, badge, statusLabel, detailsLabel, confirmRow, allowBtn, denyBtn} = record;
        for (const cls of ['running', 'ok', 'error', 'awaiting_confirm'])
            badge.remove_style_class_name(`bytey-tool-${cls}`);
        badge.add_style_class_name(`bytey-tool-${tool.status}`);

        statusLabel.text = this._formatToolStatus(tool.status);
        const detailText = this._formatToolDetails(tool);
        detailsLabel.text = detailText;

        const awaiting = tool.status === 'awaiting_confirm';
        if (awaiting) {
            record.expanded = true;
            allowBtn.reactive = true;
            denyBtn.reactive = true;
        }
        detailsLabel.visible = record.expanded && detailText.length > 0;
        confirmRow.visible = awaiting;
    }

    _formatToolStatus(status) {
        switch (status) {
        case 'awaiting_confirm':
            return 'Confirm?';
        case 'ok':
            return 'OK';
        case 'error':
            return 'Error';
        case 'running':
        default:
            return 'Running';
        }
    }

    _formatToolDetails(tool) {
        const parts = [];
        if (tool.argumentsText)
            parts.push(`Args: ${tool.argumentsText}`);
        if (tool.summary)
            parts.push(tool.summary);
        return parts.join('\n');
    }

    _scheduleScrollToBottom() {
        if (this._destroyed || this._scrollIdleId || !this._isOpen)
            return;

        this._scrollIdleId = GLib.idle_add(GLib.PRIORITY_DEFAULT_IDLE, () => {
            this._scrollIdleId = 0;
            if (this._destroyed || !this._scrollView)
                return GLib.SOURCE_REMOVE;

            const adj = this._scrollView.vadjustment;
            if (adj) {
                const target = Math.max(0, adj.upper - adj.page_size);
                adj.value = target;
            }
            return GLib.SOURCE_REMOVE;
        });
    }
}
