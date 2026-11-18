import Clutter from 'gi://Clutter';
import GLib from 'gi://GLib';
import Pango from 'gi://Pango';
import St from 'gi://St';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';

const DRAWER_GAP_PX = 8;
const DRAWER_WIDTH_FALLBACK = 380;
const DRAWER_HEIGHT_FALLBACK = 400;

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
     * @param {(isOpen: boolean) => void} [callbacks.onOpenChanged]
     */
    constructor(positionManager, callbacks = {}) {
        this._positionManager = positionManager;
        this._callbacks = callbacks;

        this._isOpen = false;
        this._destroyed = false;
        this._online = false;
        this._listening = false;
        this._petName = 'AI Pet';
        this._monitorIndex = 0;

        this._messages = [];
        this._messageWidgets = new Map();
        this._toolWidgets = new Map();
        this._scrollIdleId = 0;
        this._signalIds = [];

        this._container = new St.BoxLayout({
            style_class: 'ai-pet-drawer',
            orientation: Clutter.Orientation.VERTICAL,
            reactive: true,
            visible: false,
            x_expand: false,
            y_expand: false,
        });

        this._buildHeader();
        this._buildMessageArea();
        this._buildFooter();

        this._connect(this._container, 'key-press-event', (_actor, event) => {
            if (event.get_key_symbol() === Clutter.KEY_Escape) {
                this.close();
                return Clutter.EVENT_STOP;
            }
            return Clutter.EVENT_PROPAGATE;
        });

        Main.layoutManager.addTopChrome(this._container);
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
            style_class: 'ai-pet-drawer-header',
            x_expand: true,
        });

        this._providerLabel = new St.Label({
            style_class: 'ai-pet-provider-pill',
            text: 'ollama',
            y_align: Clutter.ActorAlign.CENTER,
        });
        header.add_child(this._providerLabel);

        this._statusLabel = new St.Label({
            style_class: 'ai-pet-status-dot',
            text: 'Offline',
            x_expand: true,
            x_align: Clutter.ActorAlign.START,
            y_align: Clutter.ActorAlign.CENTER,
        });
        header.add_child(this._statusLabel);

        const clearBtn = new St.Button({
            style_class: 'ai-pet-clear-btn',
            label: 'New Chat',
            can_focus: true,
            y_align: Clutter.ActorAlign.CENTER,
        });
        this._connect(clearBtn, 'clicked', () => {
            this._callbacks.onClearChat?.();
        });
        header.add_child(clearBtn);

        const closeBtn = new St.Button({
            style_class: 'ai-pet-close-btn',
            icon_name: 'window-close-symbolic',
            can_focus: true,
            y_align: Clutter.ActorAlign.CENTER,
        });
        this._connect(closeBtn, 'clicked', () => this.close());
        header.add_child(closeBtn);

        this._container.add_child(header);
    }

    _buildMessageArea() {
        this._messagesBox = new St.BoxLayout({
            style_class: 'ai-pet-messages',
            orientation: Clutter.Orientation.VERTICAL,
            x_expand: true,
        });

        this._scrollView = new St.ScrollView({
            style_class: 'ai-pet-scroll',
            hscrollbar_policy: St.PolicyType.NEVER,
            vscrollbar_policy: St.PolicyType.AUTOMATIC,
            enable_mouse_scrolling: true,
            overlay_scrollbars: true,
            x_expand: true,
            y_expand: true,
            child: this._messagesBox,
        });

        this._emptyLabel = new St.Label({
            style_class: 'ai-pet-empty-state',
            text: 'No messages yet. Speak your wake phrase or type below.',
            x_expand: true,
            x_align: Clutter.ActorAlign.CENTER,
        });
        this._emptyLabel.clutter_text.line_wrap = true;
        this._emptyLabel.clutter_text.line_wrap_mode = Pango.WrapMode.WORD_CHAR;
        this._emptyLabel.clutter_text.ellipsize = Pango.EllipsizeMode.NONE;

        this._container.add_child(this._scrollView);
    }

    _buildFooter() {
        const footer = new St.BoxLayout({
            style_class: 'ai-pet-drawer-footer',
            x_expand: true,
        });

        this._entry = new St.Entry({
            style_class: 'ai-pet-entry',
            hint_text: 'Ask AI Pet...',
            can_focus: true,
            reactive: true,
            x_expand: true,
            y_align: Clutter.ActorAlign.CENTER,
        });

        this._connect(this._entry, 'button-press-event', () => {
            this._entry.clutter_text.grab_key_focus();
            return Clutter.EVENT_PROPAGATE;
        });

        this._connect(this._entry.clutter_text, 'activate', () => {
            this._submitEntry();
        });

        this._connect(this._entry.clutter_text, 'key-press-event', (_actor, event) => {
            if (event.get_key_symbol() === Clutter.KEY_Escape) {
                this.close();
                return Clutter.EVENT_STOP;
            }
            return Clutter.EVENT_PROPAGATE;
        });

        footer.add_child(this._entry);

        const sendBtn = new St.Button({
            style_class: 'ai-pet-send-btn',
            label: 'Send',
            can_focus: true,
            y_align: Clutter.ActorAlign.CENTER,
        });
        this._connect(sendBtn, 'clicked', () => this._submitEntry());
        footer.add_child(sendBtn);

        this._micBtn = new St.Button({
            style_class: 'ai-pet-mic-btn',
            icon_name: 'audio-input-microphone-symbolic',
            can_focus: true,
            y_align: Clutter.ActorAlign.CENTER,
        });
        this._connect(this._micBtn, 'clicked', () => {
            this._callbacks.onManualMic?.();
        });
        footer.add_child(this._micBtn);

        this._container.add_child(footer);
    }

    _submitEntry() {
        const text = (this._entry.text ?? '').trim();
        if (!text)
            return;
        this._entry.text = '';
        this._callbacks.onSendText?.(text);
    }

    /**
     * Update daemon connection status indicator.
     *
     * @param {boolean} online
     */
    setConnectionStatus(online) {
        if (this._destroyed)
            return;
        this._online = Boolean(online);
        this._statusLabel.text = this._online ? 'Connected' : 'Offline';
        if (this._online)
            this._statusLabel.add_style_class_name('ai-pet-status-dot-online');
        else
            this._statusLabel.remove_style_class_name('ai-pet-status-dot-online');
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
            this._micBtn.add_style_class_name('ai-pet-mic-btn-active');
        else
            this._micBtn.remove_style_class_name('ai-pet-mic-btn-active');
    }

    /**
     * Update active provider/model pill and pet display name from daemon config.
     *
     * @param {object} config
     */
    setConfig(config) {
        if (this._destroyed || !config)
            return;

        const petName = config.general?.pet_name ?? config.pet_name;
        if (typeof petName === 'string' && petName.trim())
            this._petName = petName.trim();

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
            if (this._messages.length === 0 && this._emptyLabel.get_parent() === this._messagesBox)
                this._messagesBox.remove_child(this._emptyLabel);

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

        // Attach to specified message_id or latest assistant message (creating one if needed).
        let msgRecord = tool.messageId ? this._messageWidgets.get(tool.messageId) : null;
        if (!msgRecord) {
            for (let i = this._messages.length - 1; i >= 0; i--) {
                if (this._messages[i].role === 'assistant') {
                    msgRecord = this._messageWidgets.get(this._messages[i].id);
                    break;
                }
            }
        }
        if (!msgRecord) {
            this.upsertMessageDelta({
                message_id: tool.messageId ?? `tool-msg-${tool.id}`,
                role: 'assistant',
                source: 'text',
                text: '',
                is_final: false,
            });
            msgRecord = this._messageWidgets.get(tool.messageId ?? `tool-msg-${tool.id}`);
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
    updatePosition(islandX, islandY, islandW, islandH, monitorIndex = this._monitorIndex) {
        if (this._destroyed)
            return;

        this._monitorIndex = monitorIndex;
        const [, natW] = this._container.get_preferred_width(-1);
        const [, natH] = this._container.get_preferred_height(-1);
        const drawerW = Math.max(DRAWER_WIDTH_FALLBACK, natW || DRAWER_WIDTH_FALLBACK);
        const drawerH = Math.min(440, Math.max(160, natH || DRAWER_HEIGHT_FALLBACK));

        const inLowerHalf = this._positionManager.isInLowerHalf(islandY, islandH, monitorIndex);
        const targetX = Math.round(islandX + islandW / 2 - drawerW / 2);
        const targetY = inLowerHalf
            ? Math.round(islandY - drawerH - DRAWER_GAP_PX)
            : Math.round(islandY + islandH + DRAWER_GAP_PX);

        const clamped = this._positionManager.clampToMonitor(
            targetX,
            targetY,
            drawerW,
            drawerH,
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
        if (this._destroyed)
            return;

        this._isOpen = true;
        this._container.visible = true;
        this.updatePosition(islandX, islandY, islandW, islandH, monitorIndex);

        const parent = this._container.get_parent();
        if (parent)
            parent.set_child_above_sibling(this._container, null);

        this._scheduleScrollToBottom();
        this._entry.clutter_text.grab_key_focus();
        this._callbacks.onOpenChanged?.(true);
    }

    /**
     * Hide the transcript drawer.
     */
    close() {
        if (this._destroyed || !this._isOpen)
            return;
        this._isOpen = false;
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

        if (this._scrollIdleId) {
            GLib.source_remove(this._scrollIdleId);
            this._scrollIdleId = 0;
        }

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

        if (this._container) {
            Main.layoutManager.removeChrome(this._container);
            this._container.destroy();
            this._container = null;
        }
    }

    _renderEmptyState() {
        if (this._emptyLabel.get_parent() !== this._messagesBox)
            this._messagesBox.add_child(this._emptyLabel);
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
        if (this._emptyLabel.get_parent() === this._messagesBox)
            this._messagesBox.remove_child(this._emptyLabel);

        const isUser = msg.role === 'user';
        const bubble = new St.BoxLayout({
            style_class: isUser ? 'ai-pet-msg-user' : 'ai-pet-msg-assistant',
            orientation: Clutter.Orientation.VERTICAL,
            x_expand: true,
        });

        const metaRow = new St.BoxLayout({
            style_class: 'ai-pet-msg-meta',
            x_expand: true,
        });

        const roleLabel = new St.Label({
            style_class: 'ai-pet-msg-role',
            text: isUser ? 'You' : this._petName,
            y_align: Clutter.ActorAlign.CENTER,
        });
        metaRow.add_child(roleLabel);

        if (isUser) {
            const isVoice = msg.input_mode === 'voice';
            const srcBadge = new St.Label({
                style_class: `ai-pet-source-badge ${isVoice ? 'ai-pet-source-voice' : 'ai-pet-source-text'}`,
                text: isVoice ? 'Voice' : 'Text',
                y_align: Clutter.ActorAlign.CENTER,
            });
            metaRow.add_child(srcBadge);
        }

        const emotionLabel = new St.Label({
            style_class: 'ai-pet-emotion-tag',
            text: msg.emotion,
            visible: !isUser && msg.emotion && msg.emotion !== 'neutral',
            y_align: Clutter.ActorAlign.CENTER,
        });
        metaRow.add_child(emotionLabel);

        const spacer = new St.Widget({x_expand: true});
        metaRow.add_child(spacer);

        let replayBtn = null;
        if (!isUser) {
            replayBtn = new St.Button({
                style_class: 'ai-pet-replay-btn',
                icon_name: 'audio-volume-high-symbolic',
                can_focus: true,
                visible: msg.content.trim().length > 0,
                y_align: Clutter.ActorAlign.CENTER,
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
            style_class: 'ai-pet-msg-text',
            text: msg.content,
            x_expand: true,
        });
        bodyLabel.clutter_text.line_wrap = true;
        bodyLabel.clutter_text.line_wrap_mode = Pango.WrapMode.WORD_CHAR;
        bodyLabel.clutter_text.ellipsize = Pango.EllipsizeMode.NONE;
        bubble.add_child(bodyLabel);

        const toolBox = new St.BoxLayout({
            style_class: 'ai-pet-tool-list',
            orientation: Clutter.Orientation.VERTICAL,
            x_expand: true,
            visible: msg.tool_calls.length > 0,
        });
        bubble.add_child(toolBox);

        for (const tool of msg.tool_calls)
            this._appendToolWidget(toolBox, tool);

        this._messagesBox.add_child(bubble);

        const record = {msg, box: bubble, bodyLabel, emotionLabel, toolBox, replayBtn};
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
            style_class: `ai-pet-tool-badge ai-pet-tool-${tool.status}`,
            orientation: Clutter.Orientation.VERTICAL,
            x_expand: true,
        });

        const headerBtn = new St.Button({
            style_class: 'ai-pet-tool-header-btn',
            x_expand: true,
            can_focus: true,
        });

        const headerRow = new St.BoxLayout({
            style_class: 'ai-pet-tool-header',
            x_expand: true,
        });
        const titleLabel = new St.Label({
            style_class: 'ai-pet-tool-title',
            text: `${tool.server} · ${tool.tool}`,
            x_expand: true,
            x_align: Clutter.ActorAlign.START,
            y_align: Clutter.ActorAlign.CENTER,
        });
        const statusLabel = new St.Label({
            style_class: 'ai-pet-tool-status-chip',
            text: this._formatToolStatus(tool.status),
            y_align: Clutter.ActorAlign.CENTER,
        });
        headerRow.add_child(titleLabel);
        headerRow.add_child(statusLabel);
        headerBtn.set_child(headerRow);
        badge.add_child(headerBtn);

        const detailsLabel = new St.Label({
            style_class: 'ai-pet-tool-details',
            text: this._formatToolDetails(tool),
            visible: tool.status === 'awaiting_confirm',
            x_expand: true,
        });
        detailsLabel.clutter_text.line_wrap = true;
        detailsLabel.clutter_text.line_wrap_mode = Pango.WrapMode.WORD_CHAR;
        detailsLabel.clutter_text.ellipsize = Pango.EllipsizeMode.NONE;
        badge.add_child(detailsLabel);

        const confirmRow = new St.BoxLayout({
            style_class: 'ai-pet-confirm-row',
            visible: tool.status === 'awaiting_confirm',
            x_expand: true,
        });
        const allowBtn = new St.Button({
            style_class: 'ai-pet-btn-allow',
            label: 'Allow',
            can_focus: true,
        });
        const denyBtn = new St.Button({
            style_class: 'ai-pet-btn-deny',
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
            badge.remove_style_class_name(`ai-pet-tool-${cls}`);
        badge.add_style_class_name(`ai-pet-tool-${tool.status}`);

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
