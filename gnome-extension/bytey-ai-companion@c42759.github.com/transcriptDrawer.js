// SPDX-License-Identifier: GPL-3.0-or-later

import Cairo from 'cairo';
import Clutter from 'gi://Clutter';
import Gio from 'gi://Gio';
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
const SIDEBAR_WIDTH_EM = 15;
const THEME_BASE_PX_FALLBACK = 14;
const SIDEBAR_ANIM_DURATION_MS = 220;

const DENSITY_ORDER = ['compact', 'comfortable', 'spacious'];
const DENSITY_SCALE_FACTORS = {
    compact: 0.9,
    comfortable: 1.0,
    spacious: 1.08,
};
const ENTRY_MAX_LINES = 6;

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
     * @param {(messageId: string) => void} [callbacks.onDeleteMessage]
     * @param {() => void} [callbacks.onTogglePauseTts]
     * @param {() => void} [callbacks.onManualMic]
     * @param {(callId: string, approved: boolean) => void} [callbacks.onConfirmTool]
     * @param {(width: number, height: number) => void} [callbacks.onResizeCommit]
     * @param {(enabled: boolean) => void} [callbacks.onAutoAcceptChange]
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
        this._connectionState = 'disconnected';
        this._online = false;
        this._listening = false;
        this._petName = 'Bytey';
        this._monitorIndex = 0;

        this._drawerWidth = DRAWER_WIDTH_FALLBACK;
        this._drawerHeight = DRAWER_HEIGHT_FALLBACK;
        this._density = 'comfortable';
        this._autoAccept = false;
        this._godMode = false;
        this._lastIslandBounds = {x: 500, y: 12, width: 112, height: 38};

        this._resizing = false;
        this._resizeGrab = null;
        this._resizeStartCoords = [0, 0];
        this._resizeStartSize = [DRAWER_WIDTH_FALLBACK, DRAWER_HEIGHT_FALLBACK];

        this._messages = [];
        this._sessions = [];
        this._skills = [];
        this._skillsEnabled = true;
        this._skillMatches = [];
        this._skillSuggestIdx = 0;
        this._pickCancellable = null;
        this._activeSessionId = null;
        this._pendingDeleteSessionId = null;
        this._pendingDeleteMessageId = null;
        this._speakingMessageId = null;
        this._speakingStatus = 'idle';
        this._editingSessionId = null;
        this._messageWidgets = new Map();
        this._toolWidgets = new Map();
        this._copyTimers = new Set();
        this._emptyStateActor = null;
        this._scrollIdleId = 0;
        this._focusIdleId = 0;
        this._renameFocusIdleId = 0;
        this._entryScrollIdleId = 0;
        this._menuFocusIdleId = 0;
        this._portalSubId = 0;
        this._signalIds = [];
        this._contextWindowTokens = null;
        this._latestTokenUsage = null;
        this._modelContextMap = new Map();
        this._contextRingBtn = null;
        this._contextRingArea = null;

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
        this._connect(this._container, 'style-changed', () => this._onThemeStyleChanged());
        this._syncAllIconSizes();
        this._syncEntryHeight();
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

    _copyWithFeedback(text, copyIcon, doneIconName = 'emblem-ok-symbolic') {
        St.Clipboard.get_default().set_text(St.ClipboardType.CLIPBOARD, text);
        copyIcon.icon_name = doneIconName;
        const tid = GLib.timeout_add(GLib.PRIORITY_DEFAULT, 1200, () => {
            this._copyTimers.delete(tid);
            if (this._container && copyIcon)
                copyIcon.icon_name = 'edit-copy-symbolic';
            return GLib.SOURCE_REMOVE;
        });
        this._copyTimers.add(tid);
    }

    _getThemeBasePx() {
        if (this._container?.realized) {
            const node = this._container.get_theme_node();
            const fontDesc = node?.get_font();
            if (fontDesc) {
                const rawSize = fontDesc.get_size();
                if (rawSize > 0) {
                    const basePx = fontDesc.get_size_is_absolute()
                        ? rawSize / Pango.SCALE
                        : (rawSize / Pango.SCALE) * (96 / 72);
                    return Math.max(8, basePx);
                }
            }
        }
        const scale = DENSITY_SCALE_FACTORS[this._density] ?? 1.0;
        return THEME_BASE_PX_FALLBACK * scale;
    }

    _getIconSizes() {
        const basePx = this._getThemeBasePx();
        return {
            close: Math.max(10, Math.round(basePx * 1.0)),
            mic: Math.max(11, Math.round(basePx * 1.08)),
            replay: Math.max(9, Math.round(basePx * 0.92)),
        };
    }

    _getEntryMetrics() {
        const basePx = this._getThemeBasePx();
        return {
            minHeight: Math.max(22, Math.round(basePx * 2.4)),
            lineHeight: Math.max(12, Math.round(basePx * 1.3)),
        };
    }

    _getSidebarWidth() {
        const basePx = this._getThemeBasePx();
        return Math.round(Math.max(180, Math.min(280, basePx * SIDEBAR_WIDTH_EM)));
    }

    _syncAllIconSizes() {
        if (!this._container)
            return;
        const sizes = this._getIconSizes();
        if (this._closeIcon)
            this._closeIcon.icon_size = sizes.close;
        if (this._godModeIcon)
            this._godModeIcon.icon_size = sizes.close;
        if (this._autoAcceptIcon)
            this._autoAcceptIcon.icon_size = sizes.close;
        if (this._clearIcon)
            this._clearIcon.icon_size = sizes.close;
        if (this._sidebarToggleIcon)
            this._sidebarToggleIcon.icon_size = sizes.close;
        if (this._sidebarNewIcon)
            this._sidebarNewIcon.icon_size = sizes.close;
        if (this._sendIcon)
            this._sendIcon.icon_size = sizes.mic;
        if (this._micIcon)
            this._micIcon.icon_size = sizes.mic;
        for (const rec of this._messageWidgets.values()) {
            if (rec.replayIcon)
                rec.replayIcon.icon_size = sizes.replay;
            if (rec.copyIcon)
                rec.copyIcon.icon_size = sizes.replay;
            if (rec.deleteIcon)
                rec.deleteIcon.icon_size = sizes.replay;
            if (rec.confirmDeleteIcon)
                rec.confirmDeleteIcon.icon_size = sizes.replay;
            if (rec.cancelDeleteIcon)
                rec.cancelDeleteIcon.icon_size = sizes.replay;
        }
    }

    _onThemeStyleChanged() {
        if (!this._container)
            return;
        this._syncAllIconSizes();
        this._syncEntryHeight();
        if (this._sidebarBox && this._sidebarOpen)
            this._sidebarBox.width = this._getSidebarWidth();
    }

    _buildHeader() {
        const header = new St.BoxLayout({
            style_class: 'bytey-drawer-header',
            x_expand: true,
            y_align: Clutter.ActorAlign.CENTER,
        });

        this._sidebarToggleIcon = new St.Icon({
            icon_name: 'sidebar-show-symbolic',
            icon_size: this._getIconSizes().close,
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

        const extDir = Gio.File.new_for_uri(import.meta.url).get_parent();
        const iconSize = this._getIconSizes().close;

        this._providerIcon = new St.Icon({
            icon_name: 'application-x-executable-symbolic',
            icon_size: iconSize - 2,
        });
        this._providerLabel = new St.Label({
            style_class: 'bytey-provider-pill-label',
            text: 'ollama',
            x_align: Clutter.ActorAlign.START,
            y_align: Clutter.ActorAlign.CENTER,
        });
        this._providerLabel.clutter_text.ellipsize = Pango.EllipsizeMode.END;
        const providerChevron = new St.Icon({
            icon_name: 'pan-down-symbolic',
            icon_size: iconSize - 3,
        });
        const providerRow = new St.BoxLayout({
            style_class: 'bytey-pill-row',
            y_align: Clutter.ActorAlign.CENTER,
        });
        providerRow.add_child(this._providerIcon);
        providerRow.add_child(this._providerLabel);
        providerRow.add_child(providerChevron);

        this._providerPill = new St.Button({
            style_class: 'bytey-provider-pill',
            can_focus: true,
            y_align: Clutter.ActorAlign.CENTER,
            accessible_name: 'Select model',
            child: providerRow,
        });
        this._connect(this._providerPill, 'clicked', () => {
            this._toggleModelMenu();
        });
        header.add_child(this._providerPill);

        const wsIcon = new St.Icon({
            icon_name: 'folder-symbolic',
            icon_size: iconSize - 2,
        });
        this._workspaceLabel = new St.Label({
            style_class: 'bytey-workspace-pill-label',
            text: 'Workspace',
            x_align: Clutter.ActorAlign.START,
            y_align: Clutter.ActorAlign.CENTER,
        });
        this._workspaceLabel.clutter_text.ellipsize = Pango.EllipsizeMode.END;
        const wsChevron = new St.Icon({
            icon_name: 'pan-down-symbolic',
            icon_size: iconSize - 3,
        });
        const wsRow = new St.BoxLayout({
            style_class: 'bytey-pill-row',
            y_align: Clutter.ActorAlign.CENTER,
        });
        wsRow.add_child(wsIcon);
        wsRow.add_child(this._workspaceLabel);
        wsRow.add_child(wsChevron);

        this._workspacePill = new St.Button({
            style_class: 'bytey-workspace-pill',
            can_focus: true,
            y_align: Clutter.ActorAlign.CENTER,
            accessible_name: 'Select workspace',
            child: wsRow,
        });
        this._connect(this._workspacePill, 'clicked', () => {
            this._hideModelMenu();
            this._toggleWorkspaceMenu();
        });
        header.add_child(this._workspacePill);

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

        this._godModeIcon = new St.Icon({
            gicon: Gio.FileIcon.new(
                extDir.resolve_relative_path('icons/bytey-god-mode-symbolic.svg')
            ),
            icon_size: iconSize,
        });
        this._godModeBtn = new St.Button({
            style_class: 'bytey-god-mode-btn',
            toggle_mode: true,
            can_focus: true,
            y_align: Clutter.ActorAlign.CENTER,
            accessible_name: 'God Mode',
            child: this._godModeIcon,
        });
        this._connect(this._godModeBtn, 'clicked', () => {
            this._toggleGodMode();
        });
        this._bindHoverTooltip(this._godModeBtn, 'God Mode');
        header.add_child(this._godModeBtn);

        this._autoAcceptIcon = new St.Icon({
            gicon: Gio.FileIcon.new(
                extDir.resolve_relative_path('icons/bytey-double-check-symbolic.svg')
            ),
            icon_size: iconSize,
        });
        this._autoAcceptBtn = new St.Button({
            style_class: 'bytey-auto-accept-btn',
            toggle_mode: true,
            can_focus: true,
            y_align: Clutter.ActorAlign.CENTER,
            accessible_name: 'Auto-Accept',
            child: this._autoAcceptIcon,
        });
        this._connect(this._autoAcceptBtn, 'clicked', () => {
            this._toggleAutoAccept();
        });
        this._bindHoverTooltip(this._autoAcceptBtn, 'Auto-Accept');
        header.add_child(this._autoAcceptBtn);

        this._clearIcon = new St.Icon({
            icon_name: 'list-add-symbolic',
            icon_size: iconSize,
        });
        const clearBtn = new St.Button({
            style_class: 'bytey-clear-btn',
            can_focus: true,
            y_align: Clutter.ActorAlign.CENTER,
            accessible_name: 'New Chat',
            child: this._clearIcon,
        });
        this._connect(clearBtn, 'clicked', () => {
            this._callbacks.onClearChat?.();
        });
        this._bindHoverTooltip(clearBtn, 'New Chat');
        header.add_child(clearBtn);

        this._closeIcon = new St.Icon({
            icon_name: 'window-close-symbolic',
            icon_size: this._getIconSizes().close,
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

        this._modelMenuBox = new St.BoxLayout({
            style_class: 'bytey-workspace-menu',
            orientation: Clutter.Orientation.VERTICAL,
            visible: false,
            x_expand: true,
        });
        this._container.add_child(this._modelMenuBox);

        this._workspaceMenuBox = new St.BoxLayout({
            style_class: 'bytey-workspace-menu',
            orientation: Clutter.Orientation.VERTICAL,
            visible: false,
            x_expand: true,
        });
        this._container.add_child(this._workspaceMenuBox);
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
            width: this._getSidebarWidth(),
            visible: false,
            clip_to_allocation: true,
            x_expand: false,
            y_expand: true,
        });

        this._sidebarNewIcon = new St.Icon({
            icon_name: 'list-add-symbolic',
            icon_size: this._getIconSizes().close,
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
        this._buildSkillSuggest(this._chatColumn);
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
        this._pendingAttachments = [];
        this._attachStrip = new St.BoxLayout({
            style_class: 'bytey-attach-strip',
            x_expand: true,
            visible: false,
        });
        parent.add_child(this._attachStrip);

        const footer = new St.BoxLayout({
            style_class: 'bytey-drawer-footer',
            x_expand: true,
        });

        this._contextRingArea = new St.DrawingArea({
            width: 32,
            height: 32,
            x_expand: false,
            y_expand: false,
        });
        this._connect(this._contextRingArea, 'repaint', area => {
            this._drawContextRing(area);
        });
        this._contextRingBtn = new St.Bin({
            style_class: 'bytey-context-ring',
            reactive: true,
            track_hover: true,
            can_focus: false,
            y_align: Clutter.ActorAlign.END,
            accessible_name: 'Context usage',
            child: this._contextRingArea,
        });
        this._connect(this._contextRingBtn, 'notify::hover', () => {
            if (this._contextRingBtn?.hover)
                this._showTooltip(this._contextRingBtn, this._getContextTooltipText());
            else
                this._hideStatusTooltip();
        });
        footer.add_child(this._contextRingBtn);

        this._entry = new St.Entry({
            style_class: 'bytey-entry',
            hint_text: 'Ask Bytey...',
            can_focus: true,
            reactive: true,
            x_expand: true,
        });
        this._entry.clutter_text.set_single_line_mode(false);
        this._entry.clutter_text.set_activatable(false);
        this._entry.clutter_text.set_line_wrap(true);
        this._entry.clutter_text.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR);

        this._entryScroll = new St.ScrollView({
            style_class: 'bytey-entry-scroll',
            hscrollbar_policy: St.PolicyType.NEVER,
            vscrollbar_policy: St.PolicyType.NEVER,
            overlay_scrollbars: false,
            x_expand: true,
            y_align: Clutter.ActorAlign.END,
        });
        const entryBox = new St.BoxLayout({
            orientation: Clutter.Orientation.VERTICAL,
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
            this._updateSkillSuggestions();
        });

        this._connect(this._entry.clutter_text, 'cursor-changed', () => {
            this._scrollEntryToCursor();
        });

        this._connect(this._entry.clutter_text, 'key-press-event', (_actor, event) => {
            const sym = event.get_key_symbol();
            const suggestOpen = Boolean(this._skillSuggestBox?.visible && this._skillMatches.length > 0);
            if (suggestOpen) {
                if (sym === Clutter.KEY_Down) {
                    this._skillSuggestIdx = (this._skillSuggestIdx + 1) % this._skillMatches.length;
                    this._updateSkillSuggestHighlight();
                    return Clutter.EVENT_STOP;
                }
                if (sym === Clutter.KEY_Up) {
                    this._skillSuggestIdx = (this._skillSuggestIdx - 1 + this._skillMatches.length) % this._skillMatches.length;
                    this._updateSkillSuggestHighlight();
                    return Clutter.EVENT_STOP;
                }
                const isEnter = sym === Clutter.KEY_Return || sym === Clutter.KEY_KP_Enter;
                const hasShift = Boolean(event.get_state() & Clutter.ModifierType.SHIFT_MASK);
                if (sym === Clutter.KEY_Tab || (isEnter && !hasShift)) {
                    const selected = this._skillMatches[this._skillSuggestIdx];
                    if (selected)
                        this._applySkillSuggestion(String(selected.slash_command || selected.name || ''));
                    return Clutter.EVENT_STOP;
                }
                if (sym === Clutter.KEY_Escape) {
                    this._hideSkillSuggestions();
                    return Clutter.EVENT_STOP;
                }
            }
            if (sym === Clutter.KEY_Escape) {
                this.close();
                return Clutter.EVENT_STOP;
            }
            if (sym === Clutter.KEY_Return || sym === Clutter.KEY_KP_Enter) {
                if (event.get_state() & Clutter.ModifierType.SHIFT_MASK) {
                    const ct = this._entry.clutter_text;
                    ct.delete_selection();
                    ct.insert_text('\n', ct.get_cursor_position());
                    return Clutter.EVENT_STOP;
                }
                this._submitEntry();
                return Clutter.EVENT_STOP;
            }
            return Clutter.EVENT_PROPAGATE;
        });

        footer.add_child(this._entryScroll);

        const iconSizes = this._getIconSizes();
        this._attachIcon = new St.Icon({
            icon_name: 'mail-attachment-symbolic',
            icon_size: iconSizes.mic,
        });
        this._attachBtn = new St.Button({
            style_class: 'bytey-attach-btn',
            can_focus: true,
            y_align: Clutter.ActorAlign.END,
            accessible_name: 'Attach files',
            child: this._attachIcon,
        });
        this._connect(this._attachBtn, 'clicked', () => {
            this._pickAttachments();
        });
        footer.add_child(this._attachBtn);

        this._micIcon = new St.Icon({
            icon_name: 'audio-input-microphone-symbolic',
            icon_size: iconSizes.mic,
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

        this._sendIcon = new St.Icon({
            icon_name: 'mail-send-symbolic',
            icon_size: iconSizes.mic,
        });
        const sendBtn = new St.Button({
            style_class: 'bytey-send-btn',
            can_focus: true,
            y_align: Clutter.ActorAlign.END,
            child: this._sendIcon,
        });
        this._connect(sendBtn, 'clicked', () => this._submitEntry());
        footer.add_child(sendBtn);

        const gripLabel = new St.Icon({
            style_class: 'bytey-resize-grip-label',
            icon_name: 'view-fullscreen-symbolic',
            icon_size: 12,
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
        if (!this._container || !this._entry || !this._entryScroll)
            return;
        const metrics = this._getEntryMetrics();
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
        if (natH > maxH)
            this._scrollEntryToCursor();
    }

    _scrollEntryToCursor() {
        if (!this._container || !this._entry || !this._entryScroll)
            return;
        if (this._entryScroll.vscrollbar_policy !== St.PolicyType.AUTOMATIC)
            return;
        const vadj = this._entryScroll.vadjustment;
        if (!vadj)
            return;
        if (this._entryScrollIdleId) {
            GLib.Source.remove(this._entryScrollIdleId);
            this._entryScrollIdleId = 0;
        }
        this._entryScrollIdleId = GLib.idle_add(GLib.PRIORITY_DEFAULT_IDLE, () => {
            this._entryScrollIdleId = 0;
            if (!this._container || !this._entry || !this._entryScroll)
                return GLib.SOURCE_REMOVE;
            const ct = this._entry.clutter_text;
            const [ok, _x, y, lineH] = ct.position_to_coords(ct.get_cursor_position());
            if (!ok)
                return GLib.SOURCE_REMOVE;
            const pad = 6;
            const cur = vadj.value;
            const page = vadj.page_size;
            const maxVal = Math.max(0, vadj.upper - page);
            if (y - pad < cur)
                vadj.value = Math.max(0, y - pad);
            else if (y + lineH + pad > cur + page)
                vadj.value = Math.min(maxVal, y + lineH + pad - page);
            return GLib.SOURCE_REMOVE;
        });
    }

    _buildSkillSuggest(parent) {
        this._skillSuggestBox = new St.BoxLayout({
            style_class: 'bytey-skill-suggest',
            orientation: Clutter.Orientation.VERTICAL,
            visible: false,
            x_expand: true,
        });
        this._skillSuggestList = new St.BoxLayout({
            style_class: 'bytey-skill-suggest-list',
            orientation: Clutter.Orientation.VERTICAL,
            x_expand: true,
        });
        this._skillSuggestScroll = new St.ScrollView({
            style_class: 'bytey-skill-suggest-scroll',
            hscrollbar_policy: St.PolicyType.NEVER,
            vscrollbar_policy: St.PolicyType.AUTOMATIC,
            enable_mouse_scrolling: true,
            overlay_scrollbars: false,
            x_expand: true,
        });
        this._skillSuggestScroll.set_child(this._skillSuggestList);
        this._skillSuggestBox.add_child(this._skillSuggestScroll);
        parent.add_child(this._skillSuggestBox);
    }

    /**
     * Update available slash-command skills for autocomplete.
     *
     * @param {Array<object>} skills
     * @param {boolean} [enabled=true]
     */
    setSkills(skills, enabled = true) {
        if (!this._container)
            return;
        this._skillsEnabled = enabled !== false;
        this._skills = Array.isArray(skills)
            ? skills.filter(s => s && s.enabled !== false && s.valid !== false)
            : [];
        if (this._skillSuggestBox?.visible)
            this._updateSkillSuggestions();
    }

    _updateSkillSuggestions() {
        if (!this._container || !this._entry || !this._skillSuggestBox)
            return;
        if (!this._skillsEnabled || this._skills.length === 0) {
            this._hideSkillSuggestions();
            return;
        }
        const text = this._entry.text ?? '';
        const match = text.match(/^\/([a-zA-Z0-9_-]*)$/);
        if (!match) {
            this._hideSkillSuggestions();
            return;
        }
        const query = match[1].toLowerCase();
        const matches = this._skills.filter(s => {
            const cmd = String(s.slash_command || s.name || '').toLowerCase();
            const name = String(s.name || '').toLowerCase();
            return cmd.startsWith(query) || name.includes(query);
        });
        if (matches.length === 0) {
            this._hideSkillSuggestions();
            return;
        }
        const wasVisible = this._skillSuggestBox.visible;
        this._skillMatches = matches;
        this._skillSuggestIdx = wasVisible
            ? Math.max(0, Math.min(this._skillSuggestIdx, matches.length - 1))
            : 0;
        this._renderSkillSuggestions();
        this._skillSuggestBox.show();
    }

    _renderSkillSuggestions() {
        if (!this._container || !this._skillSuggestList)
            return;
        this._skillSuggestList.destroy_all_children();
        this._skillMatches.forEach((skill, idx) => {
            const cmd = String(skill.slash_command || skill.name || '');
            const rawDesc = String(skill.description || '').trim();
            const desc = rawDesc.length > 64 ? `${rawDesc.slice(0, 61)}...` : rawDesc;

            const rowContent = new St.BoxLayout({
                orientation: Clutter.Orientation.HORIZONTAL,
                x_expand: true,
                y_align: Clutter.ActorAlign.CENTER,
            });
            rowContent.add_child(new St.Label({
                style_class: 'bytey-skill-suggest-cmd',
                text: `/${cmd}`,
                y_align: Clutter.ActorAlign.CENTER,
            }));
            if (desc) {
                rowContent.add_child(new St.Label({
                    style_class: 'bytey-skill-suggest-desc',
                    text: desc,
                    x_expand: true,
                    y_align: Clutter.ActorAlign.CENTER,
                }));
            }

            const rowBtn = new St.Button({
                style_class: idx === this._skillSuggestIdx
                    ? 'bytey-skill-suggest-row bytey-skill-suggest-row-active'
                    : 'bytey-skill-suggest-row',
                can_focus: false,
                x_expand: true,
                child: rowContent,
            });
            rowBtn.connect('clicked', () => {
                this._applySkillSuggestion(cmd);
            });
            this._skillSuggestList.add_child(rowBtn);
        });
    }

    _updateSkillSuggestHighlight() {
        if (!this._container || !this._skillSuggestList)
            return;
        const children = this._skillSuggestList.get_children();
        for (let i = 0; i < children.length; i++) {
            children[i].style_class = i === this._skillSuggestIdx
                ? 'bytey-skill-suggest-row bytey-skill-suggest-row-active'
                : 'bytey-skill-suggest-row';
        }
    }

    _applySkillSuggestion(cmd) {
        if (!this._container || !this._entry)
            return;
        const nextText = `/${cmd} `;
        this._entry.text = nextText;
        this._entry.clutter_text.set_cursor_position(nextText.length);
        this._entry.clutter_text.set_selection(nextText.length, nextText.length);
        this._hideSkillSuggestions();
        this._entry.clutter_text.grab_key_focus();
        this._syncEntryHeight();
    }

    _hideSkillSuggestions() {
        this._skillMatches = [];
        this._skillSuggestIdx = 0;
        this._pickCancellable = null;
        this._skillSuggestList?.destroy_all_children();
        this._skillSuggestBox?.hide();
    }

    _renderAttachStrip() {
        if (!this._attachStrip || !this._container)
            return;
        this._attachStrip.destroy_all_children();
        if (this._pendingAttachments.length === 0) {
            this._attachStrip.visible = false;
            return;
        }
        this._attachStrip.visible = true;
        for (let idx = 0; idx < this._pendingAttachments.length; idx++) {
            const att = this._pendingAttachments[idx];
            const chip = new St.BoxLayout({
                style_class: 'bytey-attach-chip',
                y_align: Clutter.ActorAlign.CENTER,
            });
            chip.add_child(new St.Icon({
                icon_name: 'mail-attachment-symbolic',
                icon_size: 13,
                y_align: Clutter.ActorAlign.CENTER,
            }));
            chip.add_child(new St.Label({
                text: att.name,
                y_align: Clutter.ActorAlign.CENTER,
            }));
            const rmBtn = new St.Button({
                style_class: 'bytey-attach-chip-remove',
                child: new St.Icon({
                    icon_name: 'window-close-symbolic',
                    icon_size: 12,
                }),
                can_focus: true,
                y_align: Clutter.ActorAlign.CENTER,
            });
            rmBtn.connect('clicked', () => {
                this._pendingAttachments.splice(idx, 1);
                this._renderAttachStrip();
            });
            chip.add_child(rmBtn);
            this._attachStrip.add_child(chip);
        }
    }

    _beginAttachmentPick() {
        if (!this._container)
            return;
        this._restoreDrawerAfterPick = Boolean(this._isOpen && this._container.visible);
        if (this._restoreDrawerAfterPick)
            this._container.visible = false;
    }

    _endAttachmentPick() {
        if (!this._container)
            return;
        if (this._restoreDrawerAfterPick && this._isOpen) {
            this._container.visible = true;
            this._entry?.clutter_text?.grab_key_focus();
        }
        this._restoreDrawerAfterPick = false;
    }

    _pickAttachments() {
        if (!this._container)
            return;
        this._beginAttachmentPick();
        try {
            const token = `bytey_${Date.now()}_${Math.floor(Math.random() * 10000)}`;
            const senderName = Gio.DBus.session.get_unique_name()
                .replace(/^:/, '')
                .replace(/\./g, '_');
            const reqPath = `/org/freedesktop/portal/desktop/request/${senderName}/${token}`;

            if (this._portalSubId) {
                Gio.DBus.session.signal_unsubscribe(this._portalSubId);
                this._portalSubId = 0;
            }
            if (this._pickCancellable)
                this._pickCancellable.cancel();
            this._pickCancellable = new Gio.Cancellable();

            this._portalSubId = Gio.DBus.session.signal_subscribe(
                'org.freedesktop.portal.Desktop',
                'org.freedesktop.portal.Request',
                'Response',
                reqPath,
                null,
                Gio.DBusSignalFlags.NONE,
                (_conn, _sender, _path, _iface, _sig, params) => {
                    if (this._portalSubId) {
                        Gio.DBus.session.signal_unsubscribe(this._portalSubId);
                        this._portalSubId = 0;
                    }
                    this._endAttachmentPick();
                    if (!this._container)
                        return;
                    const [response, results] = params.deep_unpack();
                    if (response !== 0 || !results?.uris)
                        return;
                    const rawUris = results.uris instanceof GLib.Variant
                        ? results.uris.deep_unpack()
                        : results.uris;
                    const uris = Array.isArray(rawUris) ? rawUris : [];
                    const paths = [];
                    for (const uri of uris) {
                        const filePath = Gio.File.new_for_uri(String(uri)).get_path();
                        if (filePath)
                            paths.push(filePath);
                    }
                    this._addAttachmentPaths(paths);
                }
            );

            Gio.DBus.session.call(
                'org.freedesktop.portal.Desktop',
                '/org/freedesktop/portal/desktop',
                'org.freedesktop.portal.FileChooser',
                'OpenFile',
                new GLib.Variant('(ssa{sv})', [
                    '',
                    'Attach files',
                    {
                        handle_token: new GLib.Variant('s', token),
                        multiple: new GLib.Variant('b', true),
                    },
                ]),
                null,
                Gio.DBusCallFlags.NONE,
                -1,
                this._pickCancellable,
                (conn, res) => {
                    try {
                        conn.call_finish(res);
                    } catch (_err) {
                        if (subId) {
                            Gio.DBus.session.signal_unsubscribe(subId);
                            subId = 0;
                        }
                        this._pickAttachmentsZenity();
                    }
                }
            );
        } catch (_e) {
            this._pickAttachmentsZenity();
        }
    }

    _addAttachmentPaths(paths) {
        if (!this._container || !Array.isArray(paths))
            return;
        for (const filePath of paths) {
            if (this._pendingAttachments.length >= 5)
                break;
            if (!filePath)
                continue;
            const name = GLib.path_get_basename(filePath);
            if (!this._pendingAttachments.some(a => a.path === filePath))
                this._pendingAttachments.push({path: filePath, name});
        }
        this._renderAttachStrip();
    }

    _pickAttachmentsZenity() {
        if (!this._container)
            return;
        try {
            if (this._pickCancellable)
                this._pickCancellable.cancel();
            this._pickCancellable = new Gio.Cancellable();
            const proc = Gio.Subprocess.new(
                ['zenity', '--file-selection', '--multiple', '--separator=\n', '--title=Attach files'],
                Gio.SubprocessFlags.STDOUT_PIPE
            );
            proc.communicate_utf8_async(null, this._pickCancellable, (p, res) => {
                try {
                    const [, stdout] = p.communicate_utf8_finish(res);
                    if (p.get_successful() && stdout) {
                        const paths = stdout.split('\n').map(s => s.trim()).filter(Boolean);
                        this._addAttachmentPaths(paths);
                    }
                } catch (_err) {
                    // User cancelled or zenity unavailable.
                } finally {
                    this._endAttachmentPick();
                }
            });
        } catch (_e) {
            this._endAttachmentPick();
        }
    }

    _submitEntry() {
        const text = (this._entry.text ?? '').trim();
        const attachments = this._pendingAttachments.map(a => ({path: a.path, name: a.name}));
        if (!text && attachments.length === 0)
            return;
        this._hideSkillSuggestions();
        this._entry.text = '';
        this._pendingAttachments = [];
        this._renderAttachStrip();
        this._syncEntryHeight();
        this._callbacks.onSendText?.(text, attachments);
    }

    /**
     * Update daemon connection status indicator.
     *
     * @param {'connected'|'error'|'disconnected'|boolean} status
     */
    setConnectionStatus(status) {
        if (!this._container)
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

    _bindHoverTooltip(actor, label) {
        this._connect(actor, 'notify::hover', () => {
            if (actor.hover)
                this._showTooltip(actor, label);
            else
                this._hideStatusTooltip();
        });
    }

    _showTooltip(anchorActor, label) {
        if (!this._container || !this._isOpen || !this._statusTooltip || !anchorActor)
            return;
        this._statusTooltip.text = label;
        this._statusTooltip.visible = true;
        this._statusTooltip.get_parent()?.set_child_above_sibling(this._statusTooltip, null);

        const [ix, iy] = anchorActor.get_transformed_position();
        const [iw, ih] = anchorActor.get_transformed_size();
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

    _showStatusTooltip() {
        const label = CONNECTION_STATUS_LABELS[this._connectionState] ?? CONNECTION_STATUS_LABELS.disconnected;
        this._showTooltip(this._statusIndicator, label);
    }

    _hideStatusTooltip() {
        if (this._statusTooltip)
            this._statusTooltip.visible = false;
    }

    _formatSpaceNumber(num) {
        return Math.max(0, Math.round(num))
            .toString()
            .replace(/\B(?=(\d{3})+(?!\d))/g, ' ');
    }

    _getContextStats() {
        const usage = this._latestTokenUsage;
        let used = null;
        if (usage && typeof usage === 'object') {
            if (typeof usage.total_tokens === 'number' && Number.isFinite(usage.total_tokens) && usage.total_tokens >= 0) {
                used = usage.total_tokens;
            } else {
                const p = typeof usage.prompt_tokens === 'number' && Number.isFinite(usage.prompt_tokens) ? usage.prompt_tokens : null;
                const c = typeof usage.completion_tokens === 'number' && Number.isFinite(usage.completion_tokens) ? usage.completion_tokens : null;
                if (p !== null || c !== null)
                    used = (p ?? 0) + (c ?? 0);
            }
        }
        const limit = (typeof this._contextWindowTokens === 'number' &&
            Number.isFinite(this._contextWindowTokens) &&
            this._contextWindowTokens > 0)
            ? this._contextWindowTokens
            : null;
        const pct = (used !== null && limit !== null)
            ? Math.max(0, Math.min(100, Math.round((used / limit) * 100)))
            : null;
        return {used, limit, pct};
    }

    _getContextTooltipText() {
        const {used, limit} = this._getContextStats();
        if (used !== null && limit !== null)
            return `${this._formatSpaceNumber(used)}/${this._formatSpaceNumber(limit)}`;
        if (used !== null)
            return `${this._formatSpaceNumber(used)} / —`;
        if (limit !== null)
            return `0/${this._formatSpaceNumber(limit)}`;
        return 'Context usage unavailable';
    }

    _refreshContextRing() {
        if (!this._container)
            return;
        if (this._contextRingBtn)
            this._contextRingBtn.accessible_name = `Context usage: ${this._getContextTooltipText()}`;
        this._contextRingArea?.queue_repaint();
        if (this._contextRingBtn?.hover)
            this._showTooltip(this._contextRingBtn, this._getContextTooltipText());
    }

    _drawContextRing(area) {
        const cr = area.get_context();
        const [w, h] = area.get_surface_size();
        const cx = w / 2;
        const cy = h / 2;
        const stroke = 3.2;
        const radius = Math.max(4, Math.min(w, h) / 2 - stroke / 2 - 1);

        cr.setLineCap(Cairo.LineCap.ROUND);
        cr.setLineWidth(stroke);

        // Background track
        cr.setSourceRGBA(1, 1, 1, 0.18);
        cr.arc(cx, cy, radius, 0, Math.PI * 2);
        cr.stroke();

        const {pct} = this._getContextStats();
        let r = 0.72;
        let g = 0.75;
        let b = 0.80;
        if (pct !== null) {
            if (pct <= 65) {
                r = 0.133; g = 0.773; b = 0.369; // #22C55E green
            } else if (pct <= 89) {
                r = 0.918; g = 0.702; b = 0.031; // #EAB308 yellow
            } else {
                r = 0.937; g = 0.267; b = 0.267; // #EF4444 red
            }
            if (pct > 0) {
                const startAngle = -Math.PI / 2;
                const endAngle = startAngle + (pct / 100) * Math.PI * 2;
                cr.setSourceRGBA(r, g, b, 1.0);
                cr.arc(cx, cy, radius, startAngle, endAngle);
                cr.stroke();
            }
        }

        const label = pct !== null ? `${pct}%` : '--';
        cr.selectFontFace('Sans', Cairo.FontSlant.NORMAL, Cairo.FontWeight.BOLD);
        cr.setFontSize(pct === 100 ? 8.2 : 9.2);
        const ext = cr.textExtents(label);
        cr.setSourceRGBA(r, g, b, 1.0);
        cr.moveTo(cx - ext.width / 2 - ext.xBearing, cy - ext.height / 2 - ext.yBearing);
        cr.showText(label);

        cr.$dispose();
    }

    /**
     * Reflect active listening state on the footer Mic button.
     *
     * @param {boolean} listening
     */
    setListening(listening) {
        if (!this._container)
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
        if (!this._container || !config)
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

        this._activeProvider = String(provider || 'ollama');
        this._activeModel = String(model || '');
        this._providerLabel.text = this._activeModel || this._activeProvider;

        const cfgCtx = Number(
            config.provider?.context_size ??
            config.llm?.context_size ??
            config.llm?.max_context_size ??
            0
        );
        if (Number.isFinite(cfgCtx) && cfgCtx > 0) {
            this._contextWindowTokens = Math.round(cfgCtx);
        } else {
            const cachedModelCtx = this._modelContextMap?.get(this._activeModel);
            this._contextWindowTokens = (typeof cachedModelCtx === 'number' && cachedModelCtx > 0)
                ? cachedModelCtx
                : null;
        }
        this._refreshContextRing();

        const rawWidth = Number(config.visuals?.chat_width ?? config.chat_width);
        const rawHeight = Number(config.visuals?.chat_height ?? config.chat_height);
        const {minW, maxW} = this._getDrawerWidthBounds();
        let sizeChanged = false;
        if (Number.isFinite(rawWidth) && rawWidth >= 100 && !this._resizing) {
            const clampedW = Math.max(minW, Math.min(maxW, Math.round(rawWidth)));
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
            this._applyDensity(rawDensity);

        const rawOpacity = Number(config.visuals?.chat_opacity ?? config.chat_opacity);
        if (Number.isFinite(rawOpacity) && this._container) {
            const pct = Math.max(10, Math.min(100, Math.round(rawOpacity)));
            this._container.set_style(`background-color: rgba(22, 24, 29, ${(pct / 100).toFixed(2)});`);
        }

        const wantSidebar = Boolean(config.visuals?.chat_sidebar_open ?? config.chat_sidebar_open ?? false);
        if (wantSidebar !== this._sidebarOpen && this._sidebarBox && this._container) {
            this._sidebarOpen = wantSidebar;
            this._sidebarBox.visible = wantSidebar;
            this._sidebarBox.width = wantSidebar ? this._getSidebarWidth() : 0;
            this._applyDrawerSize();
            if (wantSidebar)
                this._callbacks.onToggleSidebar?.(true);
        }

        const wsCfg = config.workspace ?? {};
        const requireWrite = wsCfg.require_write_approval ?? true;
        const requireCmd = wsCfg.require_command_approval ?? true;
        this._syncAutoAccept(!requireWrite && !requireCmd);

        const agentsCfg = config.agents ?? {};
        this._syncGodMode(Boolean(agentsCfg.god_mode_enabled));
    }

    _toggleGodMode() {
        const next = !this._godMode;
        this._syncGodMode(next);
        this._callbacks.onGodModeChange?.(next);
    }

    _syncGodMode(enabled) {
        this._godMode = Boolean(enabled);
        if (!this._godModeBtn || !this._container)
            return;
        this._godModeBtn.set_checked(this._godMode);
        if (this._godMode)
            this._godModeBtn.add_style_class_name('bytey-god-mode-active');
        else
            this._godModeBtn.remove_style_class_name('bytey-god-mode-active');
    }

    _toggleAutoAccept() {
        const next = !this._autoAccept;
        this._syncAutoAccept(next);
        this._callbacks.onAutoAcceptChange?.(next);
    }

    _syncAutoAccept(enabled) {
        this._autoAccept = Boolean(enabled);
        if (!this._autoAcceptBtn || !this._container)
            return;
        this._autoAcceptBtn.set_checked(this._autoAccept);
        if (this._autoAccept)
            this._autoAcceptBtn.add_style_class_name('bytey-auto-accept-active');
        else
            this._autoAcceptBtn.remove_style_class_name('bytey-auto-accept-active');
    }

    _applyDensity(density) {
        if (!this._container || !DENSITY_ORDER.includes(density))
            return;
        this._density = density;
        for (const mode of DENSITY_ORDER)
            this._container.remove_style_class_name(`bytey-density-${mode}`);
        this._container.add_style_class_name(`bytey-density-${density}`);

        this._syncAllIconSizes();
        this._syncEntryHeight();
        this._renderSessions();
    }

    _onResizePress(event) {
        if (event.get_button() !== Clutter.BUTTON_PRIMARY || !this._container)
            return Clutter.EVENT_PROPAGATE;

        this._hideStatusTooltip();
        this._resizing = true;
        this._resizeStartCoords = event.get_coords();
        this._resizeStartSize = [this._drawerWidth, this._drawerHeight];
        this._releaseResizeGrab();
        this._resizeGrab = global.stage.grab(this._resizeHandle);
        this._container.add_style_class_name('bytey-drawer-resizing');
        return Clutter.EVENT_STOP;
    }

    _onResizeMotion(event) {
        if (!this._resizing || !this._container)
            return Clutter.EVENT_PROPAGATE;

        const [curX, curY] = event.get_coords();
        const dx = curX - this._resizeStartCoords[0];
        const dy = curY - this._resizeStartCoords[1];

        const b = this._lastIslandBounds;
        const inLowerHalf = this._positionManager.isInLowerHalf(b.y, b.height, this._monitorIndex);
        const heightDelta = inLowerHalf ? -dy : dy;

        const {minW, maxW} = this._getDrawerWidthBounds();
        const nextW = Math.max(
            minW,
            Math.min(maxW, Math.round(this._resizeStartSize[0] + dx * 1.5))
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
            this._resizeGrab.dismiss();
            this._resizeGrab = null;
        }
    }

    /**
     * Replace transcript view with a full list of messages from SQLite history.
     *
     * @param {Array<object>} messages
     */
    setMessages(messages) {
        if (!this._container)
            return;

        this._messages = [];
        this._messageWidgets.clear();
        this._toolWidgets.clear();
        this._emptyStateActor = null;
        this._messagesBox.destroy_all_children();

        const list = Array.isArray(messages) ? messages : [];
        if (list.length === 0) {
            this._latestTokenUsage = null;
            this._refreshContextRing();
            this._renderEmptyState();
            return;
        }

        let latestUsage = null;
        for (const raw of list) {
            const msg = this._normalizeMessage(raw);
            this._messages.push(msg);
            this._appendMessageWidget(msg);
            if (msg.role === 'assistant' && msg.usage)
                latestUsage = msg.usage;
        }
        this._latestTokenUsage = latestUsage;
        this._refreshContextRing();

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
        if (!this._container)
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

        const sidebarWidth = this._getSidebarWidth();
        if (!this._isOpen || !this._container) {
            if (this._sidebarBox) {
                this._sidebarBox.visible = nextOpen;
                this._sidebarBox.width = sidebarWidth;
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

        const animDuration = (St.ReducedMotion &&
            St.Settings.get().reducedMotion === St.ReducedMotion.REDUCE)
            ? 0
            : SIDEBAR_ANIM_DURATION_MS;

        if (this._sidebarBox) {
            if (nextOpen) {
                if (!this._sidebarBox.visible) {
                    this._sidebarBox.width = 0;
                    this._sidebarBox.opacity = 0;
                    this._sidebarBox.visible = true;
                }
                this._sidebarBox.ease({
                    width: sidebarWidth,
                    opacity: 255,
                    duration: animDuration,
                    mode: Clutter.AnimationMode.EASE_OUT_QUAD,
                });
            } else if (this._sidebarBox.visible) {
                this._sidebarBox.ease({
                    width: 0,
                    opacity: 0,
                    duration: animDuration,
                    mode: Clutter.AnimationMode.EASE_OUT_QUAD,
                    onComplete: () => {
                        if (!this._container || this._sidebarOpen !== false || !this._sidebarBox)
                            return;
                        this._sidebarBox.visible = false;
                        this._sidebarBox.width = sidebarWidth;
                        this._sidebarBox.opacity = 255;
                    },
                });
            }
        }

        this._container.ease({
            width: targetW,
            x: clamped.x,
            duration: animDuration,
            mode: Clutter.AnimationMode.EASE_OUT_QUAD,
            onComplete: () => {
                if (!this._container || this._sidebarOpen !== nextOpen)
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
        if (!this._container)
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
        if (!this._container || !this._isOpen || !this._entry)
            return;
        if (this._focusIdleId) {
            GLib.Source.remove(this._focusIdleId);
            this._focusIdleId = 0;
        }
        this._focusIdleId = GLib.idle_add(GLib.PRIORITY_DEFAULT_IDLE, () => {
            this._focusIdleId = 0;
            if (this._container && this._isOpen && this._entry)
                this._entry.clutter_text.grab_key_focus();
            return GLib.SOURCE_REMOVE;
        });
    }

    _getDrawerWidthBounds() {
        const area = this._positionManager.getWorkArea(this._monitorIndex);
        const screenW = Math.max(320, Number(area?.width ?? 1920));
        const minW = Math.round(screenW * 0.30);
        const maxW = Math.max(minW, Math.round(screenW * 0.90));
        return {minW, maxW};
    }

    _getEffectiveSize() {
        const area = this._positionManager.getWorkArea(this._monitorIndex);
        const {minW, maxW} = this._getDrawerWidthBounds();
        const maxAvailH = Math.max(DRAWER_MIN_HEIGHT, (area?.height ?? 1080) - 16);

        let targetW = this._drawerWidth;
        if (this._sidebarOpen) {
            // Split inside wide drawers (>= 520px), otherwise expand by sidebar width.
            targetW = this._drawerWidth >= 520
                ? this._drawerWidth
                : this._drawerWidth + this._getSidebarWidth();
        }

        return {
            width: Math.min(maxW, Math.max(minW, targetW)),
            height: Math.min(maxAvailH, Math.max(DRAWER_MIN_HEIGHT, this._drawerHeight)),
        };
    }

    _applyDrawerSize() {
        if (!this._container)
            return;
        const {width, height} = this._getEffectiveSize();
        this._container.set_size(width, height);
        if (this._isOpen) {
            const b = this._lastIslandBounds;
            this.updatePosition(b.x, b.y, b.width, b.height, this._monitorIndex);
        }
    }

    _renderSessions() {
        if (!this._container || !this._sessionsList)
            return;

        if (this._copyTimers) {
            for (const tid of this._copyTimers)
                GLib.Source.remove(tid);
            this._copyTimers.clear();
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

        const iconSizes = this._getIconSizes();

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
                    if (!this._container)
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

                if (this._renameFocusIdleId) {
                    GLib.Source.remove(this._renameFocusIdleId);
                    this._renameFocusIdleId = 0;
                }
                this._renameFocusIdleId = GLib.idle_add(GLib.PRIORITY_DEFAULT_IDLE, () => {
                    this._renameFocusIdleId = 0;
                    if (this._container && this._editingSessionId === sid && titleEntry.clutter_text) {
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
        if (!this._container || !event)
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
                parts: Array.isArray(event.parts) ? event.parts : undefined,
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
                const lastPart = record.msg.parts[record.msg.parts.length - 1];
                if (lastPart && lastPart.type === 'text') {
                    lastPart.text += incomingDelta;
                    const lastSeg = record.segments[record.segments.length - 1];
                    if (lastSeg && lastSeg.type === 'text') {
                        this._renderMessageBody(lastSeg.bodyBox, lastPart.text);
                    } else {
                        const bodyBox = this._createBodyBox();
                        record.contentBox.add_child(bodyBox);
                        this._renderMessageBody(bodyBox, lastPart.text);
                        record.segments.push({type: 'text', bodyBox});
                    }
                } else {
                    record.msg.parts.push({type: 'text', text: incomingDelta});
                    const bodyBox = this._createBodyBox();
                    record.contentBox.add_child(bodyBox);
                    this._renderMessageBody(bodyBox, incomingDelta);
                    record.segments.push({type: 'text', bodyBox});
                }
            } else if (isFinal) {
                if (incomingText.length > 0) {
                    const looksLikeShortTail = existing.length > 16 &&
                        incomingText.length < existing.length / 2 &&
                        !incomingText.startsWith(existing.slice(0, Math.min(8, existing.length)));
                    nextContent = looksLikeShortTail ? (existing + incomingText) : incomingText;
                }
                if (Array.isArray(event.parts)) {
                    record.msg.parts = this._normalizeParts(
                        event.parts,
                        nextContent,
                        record.msg.tool_calls
                    );
                } else {
                    record.msg.parts = this._normalizeParts(
                        null,
                        nextContent,
                        record.msg.tool_calls
                    );
                }
                this._rebuildMessageContent(record);
            } else if (incomingText.length > 0) {
                if (existing.length >= 2 && incomingText.startsWith(existing))
                    nextContent = incomingText;
                else
                    nextContent = existing + incomingText;
                record.msg.parts = this._normalizeParts(
                    null,
                    nextContent,
                    record.msg.tool_calls
                );
                this._rebuildMessageContent(record);
            }

            record.msg.content = nextContent;
            if (emotion)
                this._updateEmotionBadge(record, emotion);
            if (role === 'assistant' && event.usage && typeof event.usage === 'object') {
                record.msg.usage = event.usage;
                this._latestTokenUsage = event.usage;
                this._refreshContextRing();
            }
            const hasText = this._getMessageText(record.msg).trim().length > 0;
            if (record.replayBtn)
                record.replayBtn.visible = hasText;
            if (record.copyBtn)
                record.copyBtn.visible = hasText;
        }

        this._scheduleScrollToBottom();
    }

    /**
     * Update workspace pill and recent folders popover from `/api/v1/workspace` or `workspace_updated`.
     *
     * @param {object} ws
     */
    setWorkspace(ws) {
        if (!this._container)
            return;
        const data = ws && typeof ws === 'object' ? ws : {};
        const activePath = String(data.active_path ?? '').trim();
        const recentPaths = Array.isArray(data.recent_paths)
            ? data.recent_paths.map(p => String(p).trim()).filter(Boolean)
            : [];
        this._workspace = {active_path: activePath, recent_paths: recentPaths};

        if (this._workspaceLabel) {
            if (activePath) {
                const parts = activePath.replace(/\/+$/, '').split('/');
                const base = parts[parts.length - 1] || activePath;
                this._workspaceLabel.text = base;
                if (this._workspacePill)
                    this._workspacePill.accessible_name = `Workspace: ${activePath}`;
            } else {
                this._workspaceLabel.text = 'Workspace';
                if (this._workspacePill)
                    this._workspacePill.accessible_name = 'No Workspace';
            }
        }
        this._rebuildWorkspaceMenu();
    }

    _hideModelMenu() {
        if (this._modelMenuBox)
            this._modelMenuBox.visible = false;
    }

    _populateSearchableMenu(menuBox, placeholder, items, renderItemFn, footerFn = null) {
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
        if (this._menuFocusIdleId) {
            GLib.Source.remove(this._menuFocusIdleId);
            this._menuFocusIdleId = 0;
        }
        this._menuFocusIdleId = GLib.idle_add(GLib.PRIORITY_DEFAULT_IDLE, () => {
            this._menuFocusIdleId = 0;
            if (this._container && menuBox.visible)
                searchEntry.clutter_text.grab_key_focus();
            return GLib.SOURCE_REMOVE;
        });
    }

    async _toggleModelMenu() {
        if (!this._modelMenuBox || !this._container)
            return;
        this._hideWorkspaceMenu();
        const nextVisible = !this._modelMenuBox.visible;
        if (!nextVisible) {
            this._modelMenuBox.visible = false;
            return;
        }
        this._modelMenuBox.destroy_all_children();
        const loadingLabel = new St.Label({
            style_class: 'bytey-workspace-menu-item',
            text: 'Loading models…',
        });
        this._modelMenuBox.add_child(loadingLabel);
        this._modelMenuBox.visible = true;

        let models = [];
        try {
            const res = await this._callbacks.onListModels?.(this._activeProvider || 'ollama');
            if (Array.isArray(res))
                models = res;
        } catch (_e) {
            models = [];
        }
        if (!this._container || !this._modelMenuBox)
            return;
        const names = [];
        for (const m of models) {
            const id = String(m?.id ?? m?.name ?? m ?? '').trim();
            if (!id)
                continue;
            names.push(id);
            const ctxLen = Number(m?.context_length ?? m?.context_window ?? 0);
            if (Number.isFinite(ctxLen) && ctxLen > 0)
                this._modelContextMap?.set(id, Math.round(ctxLen));
        }
        if (this._contextWindowTokens === null && this._modelContextMap?.has(this._activeModel)) {
            this._contextWindowTokens = this._modelContextMap.get(this._activeModel);
            this._refreshContextRing();
        }
        const uniqueSorted = [...new Set(names)].sort((a, b) =>
            a.localeCompare(b, undefined, {sensitivity: 'base'})
        );
        if (uniqueSorted.length === 0) {
            this._modelMenuBox.destroy_all_children();
            this._modelMenuBox.add_child(new St.Label({
                style_class: 'bytey-workspace-menu-item',
                text: 'No models available',
            }));
            return;
        }
        const entries = uniqueSorted.map(name => ({
            name,
            searchText: name.toLowerCase(),
        }));
        this._populateSearchableMenu(
            this._modelMenuBox,
            'Filter models…',
            entries,
            entry => {
                const isActive = entry.name === this._activeModel;
                const row = new St.BoxLayout({
                    y_align: Clutter.ActorAlign.CENTER,
                });
                if (isActive) {
                    row.add_child(new St.Icon({
                        icon_name: 'emblem-ok-symbolic',
                        icon_size: 13,
                        y_align: Clutter.ActorAlign.CENTER,
                        style: 'margin-right: 6px;',
                    }));
                }
                row.add_child(new St.Label({
                    text: entry.name,
                    y_align: Clutter.ActorAlign.CENTER,
                }));
                const btn = new St.Button({
                    style_class: isActive
                        ? 'bytey-workspace-menu-item bytey-workspace-menu-item-active'
                        : 'bytey-workspace-menu-item',
                    child: row,
                    x_expand: true,
                    x_align: Clutter.ActorAlign.START,
                    can_focus: true,
                });
                btn.connect('clicked', () => {
                    this._hideModelMenu();
                    this._callbacks.onSelectModel?.(entry.name);
                });
                return btn;
            }
        );
    }

    _toggleWorkspaceMenu() {
        if (!this._workspaceMenuBox)
            return;
        const nextVisible = !this._workspaceMenuBox.visible;
        if (nextVisible)
            this._rebuildWorkspaceMenu();
        this._workspaceMenuBox.visible = nextVisible;
    }

    _hideWorkspaceMenu() {
        if (this._workspaceMenuBox)
            this._workspaceMenuBox.visible = false;
    }

    _rebuildWorkspaceMenu() {
        if (!this._workspaceMenuBox || !this._container)
            return;
        const ws = this._workspace ?? {active_path: '', recent_paths: []};
        const recent = ws.recent_paths ?? [];
        const entries = recent.map(path => {
            const parts = path.replace(/\/+$/, '').split('/');
            const base = parts[parts.length - 1] || path;
            return {
                path,
                base,
                searchText: `${base} ${path}`.toLowerCase(),
            };
        });

        const addFooter = box => {
            if (recent.length > 0) {
                box.add_child(new St.Widget({
                    style_class: 'bytey-workspace-menu-sep',
                    x_expand: true,
                }));
            }
            const settingsBtn = new St.Button({
                style_class: 'bytey-workspace-menu-item',
                label: 'Open Workspace Settings…',
                x_expand: true,
                x_align: Clutter.ActorAlign.START,
                can_focus: true,
            });
            settingsBtn.connect('clicked', () => {
                this._hideWorkspaceMenu();
                this._callbacks.onOpenWorkspaceSettings?.();
            });
            box.add_child(settingsBtn);

            if (ws.active_path) {
                const clearBtn = new St.Button({
                    style_class: 'bytey-workspace-menu-item bytey-workspace-menu-clear',
                    label: 'Clear Workspace',
                    x_expand: true,
                    x_align: Clutter.ActorAlign.START,
                    can_focus: true,
                });
                clearBtn.connect('clicked', () => {
                    this._hideWorkspaceMenu();
                    this._callbacks.onSelectWorkspace?.('');
                });
                box.add_child(clearBtn);
            }
        };

        if (entries.length === 0) {
            this._workspaceMenuBox.destroy_all_children();
            addFooter(this._workspaceMenuBox);
            return;
        }

        this._populateSearchableMenu(
            this._workspaceMenuBox,
            'Filter workspaces…',
            entries,
            entry => {
                const isActive = entry.path === ws.active_path;
                const row = new St.BoxLayout({
                    y_align: Clutter.ActorAlign.CENTER,
                });
                row.add_child(new St.Icon({
                    icon_name: isActive ? 'emblem-ok-symbolic' : 'folder-symbolic',
                    icon_size: 13,
                    y_align: Clutter.ActorAlign.CENTER,
                    style: 'margin-right: 6px;',
                }));
                row.add_child(new St.Label({
                    text: `${entry.base}  (${entry.path})`,
                    y_align: Clutter.ActorAlign.CENTER,
                }));
                const btn = new St.Button({
                    style_class: isActive
                        ? 'bytey-workspace-menu-item bytey-workspace-menu-item-active'
                        : 'bytey-workspace-menu-item',
                    child: row,
                    x_expand: true,
                    x_align: Clutter.ActorAlign.START,
                    can_focus: true,
                });
                btn.connect('clicked', () => {
                    this._hideWorkspaceMenu();
                    this._callbacks.onSelectWorkspace?.(entry.path);
                });
                return btn;
            },
            addFooter
        );
    }

    /**
     * Return true if any tool card is currently awaiting user confirmation.
     *
     * @returns {boolean}
     */
    hasPendingConfirmation() {
        for (const record of this._toolWidgets.values()) {
            if (record?.tool?.status === 'awaiting_confirm')
                return true;
        }
        return false;
    }

    /**
     * Update or attach a tool-call pill from a `tool_call_status` WebSocket event.
     *
     * @param {object} event
     */
    updateToolCall(event) {
        if (!this._container || !event)
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
            const hasPart = msgRecord.msg.parts.some(
                p => p.type === 'tool_call' && p.call_id === tool.id
            );
            if (!hasPart)
                msgRecord.msg.parts.push({type: 'tool_call', call_id: tool.id});

            const lastSeg = msgRecord.segments[msgRecord.segments.length - 1];
            let targetGroup = lastSeg && lastSeg.type === 'tool_group' ? lastSeg.group : null;
            if (!targetGroup) {
                targetGroup = this._createToolGroup();
                msgRecord.contentBox.add_child(targetGroup.container);
                msgRecord.segments.push({type: 'tool_group', group: targetGroup});
            }
            this._appendToolWidget(targetGroup.listBox, tool, targetGroup);
            this._scheduleScrollToBottom();
        }
    }

    /**
     * Show or update an inline diff / command confirmation card from `tool_confirmation_required`.
     *
     * @param {object} event
     */
    showToolConfirmation(event) {
        if (!this._container || !event)
            return;
        this.updateToolCall({
            ...event,
            status: 'awaiting_confirm',
        });
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
        if (!this._container)
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
        if (!this._container)
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
            if (this._container && this._isOpen && this._entry)
                this._entry.clutter_text.grab_key_focus();
            return GLib.SOURCE_REMOVE;
        });
        this._callbacks.onOpenChanged?.(true);
    }

    /**
     * Hide the transcript drawer.
     */
    close() {
        if (!this._container || !this._isOpen)
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
        if (this._copyTimers) {
            for (const tid of this._copyTimers)
                GLib.Source.remove(tid);
            this._copyTimers.clear();
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
        if (!this._container)
            return;
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
        if (this._entryScrollIdleId) {
            GLib.Source.remove(this._entryScrollIdleId);
            this._entryScrollIdleId = 0;
        }
        if (this._menuFocusIdleId) {
            GLib.Source.remove(this._menuFocusIdleId);
            this._menuFocusIdleId = 0;
        }
        if (this._portalSubId) {
            Gio.DBus.session.signal_unsubscribe(this._portalSubId);
            this._portalSubId = 0;
        }
        if (this._copyTimers) {
            for (const tid of this._copyTimers)
                GLib.Source.remove(tid);
            this._copyTimers.clear();
        }
        if (this._pickCancellable) {
            this._pickCancellable.cancel();
            this._pickCancellable = null;
        }
        this._pendingDeleteSessionId = null;
        this._pendingDeleteMessageId = null;
        this._speakingMessageId = null;
        this._speakingStatus = 'idle';
        this._editingSessionId = null;
        this._sidebarBox?.remove_all_transitions();
        this._container?.remove_all_transitions();

        for (const [obj, id] of this._signalIds)
            obj.disconnect(id);
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
        this._contextRingBtn = null;
        this._contextRingArea = null;
        this._skillSuggestBox = null;
        this._skillSuggestScroll = null;
        this._skillSuggestList = null;
        this._skills = [];
        this._skillMatches = [];
        this._skillSuggestIdx = 0;
        this._pickCancellable = null;
        this._sendIcon = null;
        this._providerLabel = null;
        this._autoAcceptBtn = null;
        this._godModeBtn = null;
        this._closeIcon = null;
        this._micBtn = null;
        this._micIcon = null;
        this._resizeHandle = null;
        this._positionManager = null;
        this._callbacks = null;

        this._statusIndicator = null;
        this._statusDot = null;
        this._providerPill = null;
        this._workspacePill = null;
        this._workspaceLabel = null;
        this._workspaceMenuBox = null;
        this._workspace = null;
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
        if (!this._container || !this._messagesBox || this._emptyStateActor)
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

    _normalizeParts(rawParts, content, toolCalls) {
        const parts = [];
        if (Array.isArray(rawParts) && rawParts.length > 0) {
            for (const item of rawParts) {
                if (!item || typeof item !== 'object')
                    continue;
                if (item.type === 'text') {
                    const text = String(item.text ?? '');
                    if (text)
                        parts.push({type: 'text', text});
                } else if (item.type === 'tool_call') {
                    const callId = String(item.call_id ?? '').trim();
                    if (callId)
                        parts.push({type: 'tool_call', call_id: callId});
                } else if (item.type === 'attachment') {
                    const name = String(item.name ?? '').trim();
                    if (name) {
                        parts.push({
                            type: 'attachment',
                            name,
                            mime_type: String(item.mime_type ?? 'application/octet-stream'),
                            size: Number(item.size ?? 0),
                            stored_path: String(item.stored_path ?? ''),
                        });
                    }
                }
            }
        }
        if (parts.length > 0)
            return parts;
        if (content)
            parts.push({type: 'text', text: content});
        for (const tc of toolCalls) {
            if (tc && tc.id)
                parts.push({type: 'tool_call', call_id: tc.id});
        }
        return parts;
    }

    _getMessageText(msg) {
        if (!msg)
            return '';
        if (Array.isArray(msg.parts) && msg.parts.length > 0) {
            const textSegments = msg.parts
                .filter(p => p && p.type === 'text' && String(p.text ?? '').length > 0)
                .map(p => String(p.text));
            if (textSegments.length > 0)
                return textSegments.join('\n\n');
        }
        return String(msg.content ?? '');
    }

    _normalizeMessage(raw) {
        const id = String(raw.id ?? raw.message_id ?? `msg-${Date.now()}-${Math.random()}`);
        const role = String(raw.role ?? 'assistant').toLowerCase() === 'user' ? 'user' : 'assistant';
        const inputMode = String(raw.input_mode ?? raw.source ?? 'text').toLowerCase();
        const content = String(raw.content ?? raw.text ?? '');
        const emotion = String(raw.emotion ?? 'neutral').toLowerCase();
        const rawTools = Array.isArray(raw.tool_calls) ? raw.tool_calls : [];
        const toolCalls = rawTools.map(t => this._normalizeTool({...t, message_id: id}));
        const parts = this._normalizeParts(raw.parts, content, toolCalls);
        const createdAt = raw.created_at ? String(raw.created_at) : null;
        const usage = (raw.usage && typeof raw.usage === 'object') ? raw.usage : null;
        return {id, role, input_mode: inputMode, content, parts, emotion, usage, tool_calls: toolCalls, created_at: createdAt};
    }

    _formatTimestamp(iso) {
        if (!iso) {
            const now = new Date();
            return `${String(now.getHours()).padStart(2, '0')}:${String(now.getMinutes()).padStart(2, '0')}`;
        }
        const dt = new Date(iso);
        if (Number.isNaN(dt.getTime()))
            return '';
        return `${String(dt.getHours()).padStart(2, '0')}:${String(dt.getMinutes()).padStart(2, '0')}`;
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
        } else if (raw.args && typeof raw.args === 'object') {
            try {
                argsText = JSON.stringify(raw.args, null, 2);
            } catch (_e) {
                argsText = '';
            }
        }
        const id = raw.call_id ?? raw.id ?? raw.request_id;
        const requestId = raw.request_id ? String(raw.request_id) : (id ? String(id) : '');
        const diff = typeof raw.diff === 'string' ? raw.diff : '';
        const command = typeof raw.command === 'string'
            ? raw.command
            : (typeof raw.args?.command === 'string'
                ? raw.args.command
                : (typeof raw.arguments?.command === 'string' ? raw.arguments.command : ''));
        return {
            id: id ? String(id) : '',
            requestId,
            messageId: raw.message_id ? String(raw.message_id) : null,
            server: String(raw.server ?? raw.server_name ?? 'workspace'),
            tool: String(raw.tool ?? raw.tool_name ?? 'tool'),
            status: String(raw.status ?? 'running').toLowerCase(),
            argumentsText: argsText === '{}' ? '' : argsText,
            summary: String(raw.summary ?? raw.result_summary ?? ''),
            diff,
            command,
        };
    }

    _appendMessageWidget(msg) {
        this._clearEmptyState();

        const isUser = msg.role === 'user';
        const timeStr = this._formatTimestamp(msg.created_at);
        const extDir = Gio.File.new_for_uri(import.meta.url).get_parent();

        const row = new St.BoxLayout({
            style_class: isUser ? 'bytey-msg-row-user' : 'bytey-msg-row-assistant',
            x_expand: true,
        });

        if (!isUser) {
            const avatarIcon = new St.Icon({
                gicon: Gio.FileIcon.new(
                    extDir.resolve_relative_path('icons/avatar.png')
                ),
                icon_size: 26,
            });
            const avatarBin = new St.Bin({
                style_class: 'bytey-avatar-assistant',
                y_align: Clutter.ActorAlign.START,
                child: avatarIcon,
            });
            row.add_child(avatarBin);
        } else {
            row.add_child(new St.Widget({x_expand: true}));
        }

        const metaRow = new St.BoxLayout({
            style_class: 'bytey-msg-meta',
            x_expand: true,
        });

        if (isUser) {
            metaRow.add_child(new St.Widget({x_expand: true}));
            if (timeStr) {
                metaRow.add_child(new St.Label({
                    style_class: 'bytey-msg-time',
                    text: timeStr,
                    y_align: Clutter.ActorAlign.CENTER,
                }));
            }
        } else {
            const roleLabel = new St.Label({
                style_class: 'bytey-msg-role',
                text: this._petName,
                y_align: Clutter.ActorAlign.CENTER,
            });
            metaRow.add_child(roleLabel);
            if (timeStr) {
                metaRow.add_child(new St.Label({
                    style_class: 'bytey-msg-time',
                    text: timeStr,
                    y_align: Clutter.ActorAlign.CENTER,
                }));
            }
        }

        const emotionLabel = new St.Label({
            style_class: 'bytey-emotion-tag',
            text: msg.emotion,
            visible: !isUser && msg.emotion && msg.emotion !== 'neutral',
            y_align: Clutter.ActorAlign.CENTER,
        });
        metaRow.add_child(emotionLabel);

        if (!isUser)
            metaRow.add_child(new St.Widget({x_expand: true}));

        const iconSizes = this._getIconSizes();

        let replayBtn = null;
        let replayIcon = null;
        if (!isUser) {
            let replayIconName = 'audio-volume-high-symbolic';
            if (this._speakingMessageId === msg.id && this._speakingStatus === 'playing')
                replayIconName = 'media-playback-pause-symbolic';
            else if (this._speakingMessageId === msg.id && this._speakingStatus === 'paused')
                replayIconName = 'media-playback-start-symbolic';

            replayIcon = new St.Icon({
                icon_name: replayIconName,
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
                if (this._speakingMessageId === msg.id &&
                    (this._speakingStatus === 'playing' || this._speakingStatus === 'paused')) {
                    this._callbacks.onTogglePauseTts?.();
                } else {
                    const text = this._getMessageText(msg).trim();
                    if (text)
                        this._callbacks.onReplayTts?.(msg.id, text);
                }
            });
            metaRow.add_child(replayBtn);
        }

        const copyIcon = new St.Icon({
            icon_name: 'edit-copy-symbolic',
            icon_size: iconSizes.replay,
        });
        const copyBtn = new St.Button({
            style_class: 'bytey-copy-btn',
            can_focus: true,
            accessible_name: 'Copy markdown',
            visible: this._getMessageText(msg).trim().length > 0,
            y_align: Clutter.ActorAlign.CENTER,
            child: copyIcon,
        });
        copyBtn.connect('clicked', () => {
            this._copyWithFeedback(this._getMessageText(msg), copyIcon, 'emblem-ok-symbolic');
        });
        metaRow.add_child(copyBtn);

        const deleteIcon = new St.Icon({
            icon_name: 'user-trash-symbolic',
            icon_size: iconSizes.replay,
        });
        const deleteBtn = new St.Button({
            style_class: 'bytey-msg-delete-btn',
            can_focus: true,
            accessible_name: 'Delete message',
            y_align: Clutter.ActorAlign.CENTER,
            child: deleteIcon,
        });
        const confirmDeleteIcon = new St.Icon({
            icon_name: 'object-select-symbolic',
            icon_size: iconSizes.replay,
        });
        const confirmDeleteBtn = new St.Button({
            style_class: 'bytey-msg-confirm-delete-btn',
            can_focus: true,
            accessible_name: 'Confirm delete message',
            visible: false,
            y_align: Clutter.ActorAlign.CENTER,
            child: confirmDeleteIcon,
        });
        const cancelDeleteIcon = new St.Icon({
            icon_name: 'window-close-symbolic',
            icon_size: iconSizes.replay,
        });
        const cancelDeleteBtn = new St.Button({
            style_class: 'bytey-msg-cancel-delete-btn',
            can_focus: true,
            accessible_name: 'Cancel delete message',
            visible: false,
            y_align: Clutter.ActorAlign.CENTER,
            child: cancelDeleteIcon,
        });

        deleteBtn.connect('clicked', () => {
            this._pendingDeleteMessageId = msg.id;
            this._syncDeleteMessageConfirm();
        });
        cancelDeleteBtn.connect('clicked', () => {
            this._pendingDeleteMessageId = null;
            this._syncDeleteMessageConfirm();
        });
        confirmDeleteBtn.connect('clicked', () => {
            this._pendingDeleteMessageId = null;
            this._syncDeleteMessageConfirm();
            this._callbacks.onDeleteMessage?.(msg.id);
        });

        metaRow.add_child(deleteBtn);
        metaRow.add_child(confirmDeleteBtn);
        metaRow.add_child(cancelDeleteBtn);

        const contentBox = new St.BoxLayout({
            style_class: 'bytey-msg-content',
            orientation: Clutter.Orientation.VERTICAL,
            x_expand: true,
        });

        if (isUser) {
            const userCol = new St.BoxLayout({
                style_class: 'bytey-msg-user-col',
                orientation: Clutter.Orientation.VERTICAL,
                x_align: Clutter.ActorAlign.END,
            });
            userCol.add_child(metaRow);
            const userBubble = new St.BoxLayout({
                style_class: 'bytey-msg-user',
                orientation: Clutter.Orientation.VERTICAL,
            });
            userBubble.add_child(contentBox);
            userCol.add_child(userBubble);
            row.add_child(userCol);

            const userIcon = new St.Icon({
                icon_name: 'avatar-default-symbolic',
                icon_size: 16,
            });
            const userAvatar = new St.Bin({
                style_class: 'bytey-avatar-user',
                y_align: Clutter.ActorAlign.END,
                child: userIcon,
            });
            row.add_child(userAvatar);
        } else {
            const bodyCol = new St.BoxLayout({
                style_class: 'bytey-msg-assistant',
                orientation: Clutter.Orientation.VERTICAL,
                x_expand: true,
            });
            bodyCol.add_child(metaRow);
            bodyCol.add_child(contentBox);
            row.add_child(bodyCol);
        }

        this._messagesBox.add_child(row);

        const record = {
            msg,
            box: row,
            contentBox,
            segments: [],
            emotionLabel,
            replayBtn,
            replayIcon,
            copyBtn,
            copyIcon,
            deleteBtn,
            deleteIcon,
            confirmDeleteBtn,
            confirmDeleteIcon,
            cancelDeleteBtn,
            cancelDeleteIcon,
        };
        this._rebuildMessageContent(record);
        this._messageWidgets.set(msg.id, record);
        return record;
    }

    _createBodyBox() {
        return new St.BoxLayout({
            style_class: 'bytey-msg-body',
            orientation: Clutter.Orientation.VERTICAL,
            x_expand: true,
        });
    }

    _createToolGroup() {
        const container = new St.BoxLayout({
            style_class: 'bytey-tool-group',
            orientation: Clutter.Orientation.VERTICAL,
            x_expand: true,
        });
        const summaryLabel = new St.Label({
            style_class: 'bytey-tool-group-summary',
            text: '',
            x_expand: true,
            x_align: Clutter.ActorAlign.START,
            y_align: Clutter.ActorAlign.CENTER,
        });
        summaryLabel.clutter_text.ellipsize = Pango.EllipsizeMode.END;
        const summaryChevron = new St.Icon({
            icon_name: 'pan-down-symbolic',
            icon_size: 14,
        });
        const summaryRow = new St.BoxLayout({
            style_class: 'bytey-tool-group-row',
            x_expand: true,
            y_align: Clutter.ActorAlign.CENTER,
        });
        summaryRow.add_child(summaryLabel);
        summaryRow.add_child(summaryChevron);

        const summaryBtn = new St.Button({
            style_class: 'bytey-tool-group-bar',
            x_expand: true,
            can_focus: true,
            child: summaryRow,
        });
        const listBox = new St.BoxLayout({
            style_class: 'bytey-tool-list',
            orientation: Clutter.Orientation.VERTICAL,
            x_expand: true,
            visible: false,
        });
        container.add_child(summaryBtn);
        container.add_child(listBox);

        const group = {
            container,
            summaryBtn,
            summaryLabel,
            summaryChevron,
            listBox,
            tools: [],
            expanded: false,
            userToggled: false,
        };
        summaryBtn.connect('clicked', () => {
            group.userToggled = true;
            group.expanded = !group.expanded;
            this._refreshToolGroup(group);
        });
        return group;
    }

    _refreshToolGroup(group) {
        if (!group)
            return;
        const chips = group.tools.map(t => {
            let icon = '⋯';
            if (t.status === 'ok')
                icon = '✓';
            else if (t.status === 'error' || t.status === 'denied')
                icon = '✕';
            else if (t.status === 'awaiting_confirm')
                icon = '?';
            return `${icon} ${t.tool}`;
        });
        group.summaryLabel.text = chips.join('  ·  ');

        const hasActive = group.tools.some(
            t => t.status === 'running' || t.status === 'awaiting_confirm'
        );
        if (!group.userToggled)
            group.expanded = hasActive;

        group.listBox.visible = group.expanded;
        group.summaryChevron.icon_name = group.expanded ? 'pan-up-symbolic' : 'pan-down-symbolic';
    }

    _rebuildMessageContent(record) {
        if (!record || !record.contentBox)
            return;
        for (const tool of record.msg.tool_calls) {
            if (tool?.id)
                this._toolWidgets.delete(tool.id);
        }
        record.contentBox.destroy_all_children();
        record.segments = [];

        const toolsById = new Map();
        for (const tool of record.msg.tool_calls) {
            if (tool?.id)
                toolsById.set(tool.id, tool);
        }

        const parts = this._normalizeParts(
            record.msg.parts,
            record.msg.content,
            record.msg.tool_calls
        );
        record.msg.parts = parts;

        for (const part of parts) {
            if (part.type === 'text') {
                const bodyBox = this._createBodyBox();
                record.contentBox.add_child(bodyBox);
                this._renderMessageBody(bodyBox, part.text);
                record.segments.push({type: 'text', bodyBox});
            } else if (part.type === 'tool_call') {
                const tool = toolsById.get(part.call_id);
                if (!tool)
                    continue;
                const lastSeg = record.segments[record.segments.length - 1];
                let targetGroup = lastSeg && lastSeg.type === 'tool_group' ? lastSeg.group : null;
                if (!targetGroup) {
                    targetGroup = this._createToolGroup();
                    record.contentBox.add_child(targetGroup.container);
                    record.segments.push({type: 'tool_group', group: targetGroup});
                }
                this._appendToolWidget(targetGroup.listBox, tool, targetGroup);
            } else if (part.type === 'attachment') {
                const chip = new St.BoxLayout({
                    style_class: 'bytey-msg-attachment-chip',
                    x_align: Clutter.ActorAlign.START,
                });
                chip.add_child(new St.Icon({
                    icon_name: 'mail-attachment-symbolic',
                    icon_size: 13,
                    y_align: Clutter.ActorAlign.CENTER,
                }));
                chip.add_child(new St.Label({
                    text: part.name,
                    y_align: Clutter.ActorAlign.CENTER,
                }));
                record.contentBox.add_child(chip);
                record.segments.push({type: 'attachment', chip});
            }
        }
    }

    _syncDeleteMessageConfirm() {
        for (const rec of this._messageWidgets.values()) {
            const isPending = this._pendingDeleteMessageId !== null &&
                rec.msg.id === this._pendingDeleteMessageId;
            if (rec.deleteBtn)
                rec.deleteBtn.visible = !isPending;
            if (rec.confirmDeleteBtn)
                rec.confirmDeleteBtn.visible = isPending;
            if (rec.cancelDeleteBtn)
                rec.cancelDeleteBtn.visible = isPending;
        }
    }

    /**
     * Update TTS playback indicator on assistant message widgets.
     *
     * @param {string|null} messageId
     * @param {'playing'|'paused'|'idle'} [status]
     */
    setSpeakingMessage(messageId, status = 'idle') {
        if (!this._container)
            return;
        this._speakingMessageId = messageId ?? null;
        this._speakingStatus = status ?? 'idle';

        for (const rec of this._messageWidgets.values()) {
            if (!rec.replayIcon)
                continue;
            if (rec.msg.id === this._speakingMessageId && this._speakingStatus === 'playing')
                rec.replayIcon.icon_name = 'media-playback-pause-symbolic';
            else if (rec.msg.id === this._speakingMessageId && this._speakingStatus === 'paused')
                rec.replayIcon.icon_name = 'media-playback-start-symbolic';
            else
                rec.replayIcon.icon_name = 'audio-volume-high-symbolic';
        }
    }

    /**
     * Remove a single message widget and record from the transcript.
     *
     * @param {string} messageId
     */
    removeMessage(messageId) {
        if (!this._container || !messageId)
            return;
        const record = this._messageWidgets.get(messageId);
        if (record) {
            if (record.box && record.box.get_parent() === this._messagesBox)
                this._messagesBox.remove_child(record.box);
            record.box?.destroy();
            this._messageWidgets.delete(messageId);
        }
        this._messages = this._messages.filter(m => m.id !== messageId);
        if (this._pendingDeleteMessageId === messageId)
            this._pendingDeleteMessageId = null;
        if (this._speakingMessageId === messageId) {
            this._speakingMessageId = null;
            this._speakingStatus = 'idle';
        }
        if (this._messages.length === 0)
            this._renderEmptyState();
    }

    _parseMessageSegments(text) {
        const src = String(text ?? '');
        const segments = [];
        let pos = 0;

        while (pos < src.length) {
            const tickIdx = src.indexOf('```', pos);
            const quoteIdx = src.indexOf("'''", pos);
            let fenceIdx = -1;
            let marker = '';
            if (tickIdx !== -1 && (quoteIdx === -1 || tickIdx <= quoteIdx)) {
                fenceIdx = tickIdx;
                marker = '```';
            } else if (quoteIdx !== -1) {
                fenceIdx = quoteIdx;
                marker = "'''";
            }

            if (fenceIdx === -1) {
                const tail = src.slice(pos);
                if (tail)
                    segments.push({type: 'text', text: tail});
                break;
            }

            if (fenceIdx > pos)
                segments.push({type: 'text', text: src.slice(pos, fenceIdx)});

            const afterFence = fenceIdx + 3;
            const nlIdx = src.indexOf('\n', afterFence);
            let lang = '';
            let codeStart = afterFence;
            if (nlIdx !== -1) {
                const header = src.slice(afterFence, nlIdx).trim();
                if (/^[a-zA-Z0-9_+#.-]{0,32}$/.test(header)) {
                    lang = header;
                    codeStart = nlIdx + 1;
                }
            }

            const closeIdx = src.indexOf(marker, codeStart);
            if (closeIdx === -1) {
                segments.push({type: 'code', lang, code: src.slice(codeStart).replace(/\n$/, '')});
                break;
            }

            segments.push({type: 'code', lang, code: src.slice(codeStart, closeIdx).replace(/\n$/, '')});
            pos = closeIdx + 3;
        }

        return segments;
    }

    _formatInlineMarkup(text) {
        const src = String(text ?? '');
        if (!src)
            return '';

        const codeTokens = [];
        const withPlaceholders = src.replace(/`([^`\n]+)`/g, (_m, code) => {
            const idx = codeTokens.length;
            const escaped = GLib.markup_escape_text(code, -1);
            codeTokens.push(`<tt><span background="#1e293b" foreground="#e2e8f0"> ${escaped} </span></tt>`);
            return `\u0000CODE${idx}\u0000`;
        });

        const formatChunk = chunk => {
            if (!chunk)
                return '';
            const pattern = new RegExp([
                '\\*\\*\\*[^*\\n]+?\\*\\*\\*',
                '___[^_\\n]+?___',
                '\\*\\*[^*\\n]+?\\*\\*',
                '__[^_\\n]+?__',
                '~~[^~\\n]+?~~',
                '(?<![*\\w])\\*[^*\\n]+?\\*(?!\\*)',
                '(?<![_\\w])_[^_\\n]+?_(?!_)',
                '\\[[^\\]\\n]+\\]\\(https?:\\/\\/[^\\s)]+\\)',
                '\u0000CODE\\d+\\u0000',
            ].join('|'), 'g');
            let out = '';
            let lastIdx = 0;
            let match;
            while ((match = pattern.exec(chunk)) !== null) {
                if (match.index > lastIdx)
                    out += GLib.markup_escape_text(chunk.slice(lastIdx, match.index), -1);

                const tok = match[0];
                if (tok.startsWith('\u0000CODE') && tok.endsWith('\u0000')) {
                    const idx = Number.parseInt(tok.slice(5, -1), 10);
                    out += codeTokens[idx] ?? '';
                } else if ((tok.startsWith('***') && tok.endsWith('***')) || (tok.startsWith('___') && tok.endsWith('___'))) {
                    out += `<b><i>${formatChunk(tok.slice(3, -3))}</i></b>`;
                } else if ((tok.startsWith('**') && tok.endsWith('**')) || (tok.startsWith('__') && tok.endsWith('__'))) {
                    out += `<b>${formatChunk(tok.slice(2, -2))}</b>`;
                } else if (tok.startsWith('~~') && tok.endsWith('~~')) {
                    out += `<s>${formatChunk(tok.slice(2, -2))}</s>`;
                } else if ((tok.startsWith('*') && tok.endsWith('*')) || (tok.startsWith('_') && tok.endsWith('_'))) {
                    out += `<i>${formatChunk(tok.slice(1, -1))}</i>`;
                } else if (tok.startsWith('[')) {
                    const linkMatch = /^\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)$/.exec(tok);
                    if (linkMatch) {
                        const label = formatChunk(linkMatch[1]);
                        out += `<span foreground="#38bdf8" underline="single">${label}</span>`;
                    } else {
                        out += GLib.markup_escape_text(tok, -1);
                    }
                } else {
                    out += GLib.markup_escape_text(tok, -1);
                }
                lastIdx = pattern.lastIndex;
            }
            if (lastIdx < chunk.length)
                out += GLib.markup_escape_text(chunk.slice(lastIdx), -1);
            return out;
        };

        return formatChunk(withPlaceholders);
    }

    _parseMarkdownBlocks(text) {
        const lines = String(text ?? '').split('\n');
        const blocks = [];
        let paraLines = [];
        let quoteLines = [];

        const flushPara = () => {
            if (paraLines.length > 0) {
                blocks.push({type: 'paragraph', text: paraLines.join('\n')});
                paraLines = [];
            }
        };
        const flushQuote = () => {
            if (quoteLines.length > 0) {
                blocks.push({type: 'blockquote', text: quoteLines.join('\n')});
                quoteLines = [];
            }
        };

        for (const line of lines) {
            const quoteMatch = /^\s*>\s?(.*)$/.exec(line);
            if (quoteMatch) {
                flushPara();
                quoteLines.push(quoteMatch[1]);
                continue;
            }
            flushQuote();

            if (!line.trim()) {
                flushPara();
                continue;
            }

            if (/^\s{0,3}(?:---+|\*\*\*+|___+)\s*$/.test(line)) {
                flushPara();
                blocks.push({type: 'hr'});
                continue;
            }

            const headingMatch = /^(#{1,6})\s+(.+?)\s*#*$/.exec(line);
            if (headingMatch) {
                flushPara();
                blocks.push({
                    type: 'heading',
                    level: headingMatch[1].length,
                    text: headingMatch[2],
                });
                continue;
            }

            const listMatch = /^(\s*)([*+-]|\d+\.)\s+(.+)$/.exec(line);
            if (listMatch) {
                flushPara();
                const rawMarker = listMatch[2];
                const indent = Math.min(4, Math.floor(listMatch[1].length / 2));
                blocks.push({
                    type: 'list_item',
                    indent,
                    marker: /^\d+\.$/.test(rawMarker) ? rawMarker : '•',
                    text: listMatch[3],
                });
                continue;
            }

            paraLines.push(line);
        }

        flushQuote();
        flushPara();
        return blocks;
    }

    _createFormattedLabel(text, styleClass = 'bytey-msg-text') {
        const label = new St.Label({
            style_class: styleClass,
            x_expand: true,
        });
        label.clutter_text.line_wrap = true;
        label.clutter_text.line_wrap_mode = Pango.WrapMode.WORD_CHAR;
        label.clutter_text.ellipsize = Pango.EllipsizeMode.NONE;
        label.clutter_text.set_markup(this._formatInlineMarkup(text));
        return label;
    }

    _renderMarkdownBlocks(container, text) {
        const blocks = this._parseMarkdownBlocks(text);
        for (const block of blocks) {
            if (block.type === 'heading') {
                container.add_child(
                    this._createFormattedLabel(
                        block.text,
                        `bytey-msg-text bytey-md-heading bytey-md-h${block.level}`
                    )
                );
            } else if (block.type === 'hr') {
                container.add_child(new St.Widget({
                    style_class: 'bytey-md-hr',
                    x_expand: true,
                }));
            } else if (block.type === 'blockquote') {
                const quoteBox = new St.BoxLayout({
                    style_class: 'bytey-md-blockquote',
                    orientation: Clutter.Orientation.VERTICAL,
                    x_expand: true,
                });
                this._renderMarkdownBlocks(quoteBox, block.text);
                container.add_child(quoteBox);
            } else if (block.type === 'list_item') {
                const row = new St.BoxLayout({
                    style_class: block.indent > 0
                        ? `bytey-md-list-row bytey-md-list-indent-${block.indent}`
                        : 'bytey-md-list-row',
                    x_expand: true,
                });
                const marker = new St.Label({
                    style_class: 'bytey-md-list-marker',
                    text: block.marker,
                    y_align: Clutter.ActorAlign.START,
                });
                const itemLabel = this._createFormattedLabel(
                    block.text,
                    'bytey-msg-text bytey-md-list-text'
                );
                row.add_child(marker);
                row.add_child(itemLabel);
                container.add_child(row);
            } else {
                container.add_child(this._createFormattedLabel(block.text));
            }
        }
    }

    _renderMessageBody(bodyBox, content) {
        if (!bodyBox)
            return;
        bodyBox.destroy_all_children();
        const segments = this._parseMessageSegments(content);

        for (const seg of segments) {
            if (seg.type === 'code') {
                const codeCard = new St.BoxLayout({
                    style_class: 'bytey-code-block',
                    orientation: Clutter.Orientation.VERTICAL,
                    x_expand: true,
                });

                const header = new St.BoxLayout({
                    style_class: 'bytey-code-header',
                    x_expand: true,
                });
                const langLabel = new St.Label({
                    style_class: 'bytey-code-lang',
                    text: seg.lang || 'code',
                    x_expand: true,
                    y_align: Clutter.ActorAlign.CENTER,
                });
                const copyIcon = new St.Icon({
                    icon_name: 'edit-copy-symbolic',
                    icon_size: this._getIconSizes().replay,
                });
                const copyBtn = new St.Button({
                    style_class: 'bytey-code-copy-btn',
                    can_focus: true,
                    accessible_name: 'Copy code',
                    y_align: Clutter.ActorAlign.CENTER,
                    child: copyIcon,
                });
                copyBtn.connect('clicked', () => {
                    this._copyWithFeedback(seg.code, copyIcon, 'object-select-symbolic');
                });
                header.add_child(langLabel);
                header.add_child(copyBtn);
                codeCard.add_child(header);

                const codeLabel = new St.Label({
                    style_class: 'bytey-code-text',
                    text: seg.code,
                    x_expand: true,
                });
                codeLabel.clutter_text.line_wrap = true;
                codeLabel.clutter_text.line_wrap_mode = Pango.WrapMode.WORD_CHAR;
                codeLabel.clutter_text.ellipsize = Pango.EllipsizeMode.NONE;
                codeCard.add_child(codeLabel);

                bodyBox.add_child(codeCard);
            } else {
                const trimmed = seg.text.replace(/^\n+|\n+$/g, '');
                if (!trimmed)
                    continue;
                this._renderMarkdownBlocks(bodyBox, trimmed);
            }
        }

        bodyBox.visible = bodyBox.get_n_children() > 0;
    }

    _updateEmotionBadge(record, emotion) {
        const clean = String(emotion ?? 'neutral').toLowerCase();
        record.msg.emotion = clean;
        record.emotionLabel.text = clean;
        record.emotionLabel.visible = record.msg.role !== 'user' && clean !== 'neutral';
    }

    _appendToolWidget(toolBox, tool, group = null) {
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

        const confirmCard = new St.BoxLayout({
            style_class: 'bytey-confirm-card',
            orientation: Clutter.Orientation.VERTICAL,
            visible: false,
            x_expand: true,
        });
        const confirmScroll = new St.ScrollView({
            style_class: 'bytey-confirm-scroll',
            hscrollbar_policy: St.PolicyType.AUTOMATIC,
            vscrollbar_policy: St.PolicyType.AUTOMATIC,
            overlay_scrollbars: true,
            x_expand: true,
        });
        const confirmPreviewBox = new St.BoxLayout({
            orientation: Clutter.Orientation.VERTICAL,
            x_expand: true,
        });
        const confirmPreviewLabel = new St.Label({
            style_class: 'bytey-confirm-diff',
            text: '',
            x_expand: true,
        });
        confirmPreviewLabel.clutter_text.line_wrap = false;
        confirmPreviewLabel.clutter_text.ellipsize = Pango.EllipsizeMode.NONE;
        confirmPreviewBox.add_child(confirmPreviewLabel);
        confirmScroll.set_child(confirmPreviewBox);
        confirmCard.add_child(confirmScroll);
        badge.add_child(confirmCard);

        const confirmRow = new St.BoxLayout({
            style_class: 'bytey-confirm-row',
            visible: tool.status === 'awaiting_confirm',
            x_expand: true,
        });
        const allowBtn = new St.Button({
            style_class: 'bytey-btn-allow bytey-confirm-approve-btn',
            label: 'Approve',
            can_focus: true,
        });
        const denyBtn = new St.Button({
            style_class: 'bytey-btn-deny bytey-confirm-reject-btn',
            label: 'Reject',
            can_focus: true,
        });
        confirmRow.add_child(allowBtn);
        confirmRow.add_child(denyBtn);
        badge.add_child(confirmRow);

        const record = {
            tool,
            group,
            badge,
            titleLabel,
            statusLabel,
            detailsLabel,
            confirmCard,
            confirmPreviewLabel,
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
            this._callbacks.onConfirmTool?.(record.tool.requestId || record.tool.id, true);
        });

        denyBtn.connect('clicked', () => {
            allowBtn.reactive = false;
            denyBtn.reactive = false;
            record.tool.status = 'error';
            record.tool.summary = 'Rejected by user';
            this._refreshToolWidget(record);
            this._callbacks.onConfirmTool?.(record.tool.requestId || record.tool.id, false);
        });

        this._toolWidgets.set(tool.id, record);
        if (group)
            group.tools.push(tool);
        this._refreshToolWidget(record);
        toolBox.add_child(badge);
    }

    _refreshToolWidget(record) {
        const {
            tool,
            badge,
            statusLabel,
            detailsLabel,
            confirmCard,
            confirmPreviewLabel,
            confirmRow,
            allowBtn,
            denyBtn,
        } = record;
        for (const cls of ['running', 'ok', 'error', 'awaiting_confirm'])
            badge.remove_style_class_name(`bytey-tool-${cls}`);
        badge.add_style_class_name(`bytey-tool-${tool.status}`);

        statusLabel.text = this._formatToolStatus(tool.status);
        const detailText = this._formatToolDetails(tool);
        detailsLabel.text = detailText;

        const awaiting = tool.status === 'awaiting_confirm';
        const previewText = tool.diff || tool.command || '';
        if (previewText && awaiting) {
            confirmPreviewLabel.style_class = tool.diff ? 'bytey-confirm-diff' : 'bytey-confirm-cmd';
            confirmPreviewLabel.text = tool.command && !tool.diff ? `$ ${tool.command}` : previewText;
            confirmCard.visible = true;
        } else {
            confirmCard.visible = false;
        }

        if (awaiting) {
            record.expanded = true;
            allowBtn.reactive = true;
            denyBtn.reactive = true;
        }
        detailsLabel.visible = record.expanded && detailText.length > 0;
        confirmRow.visible = awaiting;
        if (record.group)
            this._refreshToolGroup(record.group);
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
        if (!this._container || this._scrollIdleId || !this._isOpen)
            return;

        this._scrollIdleId = GLib.idle_add(GLib.PRIORITY_DEFAULT_IDLE, () => {
            this._scrollIdleId = 0;
            if (!this._container || !this._scrollView)
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
