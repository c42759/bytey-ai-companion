import GLib from 'gi://GLib';
import Gio from 'gi://Gio';
import Soup from 'gi://Soup';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';

import {IslandContextMenu} from './contextMenu.js';
import {IslandWidget} from './islandWidget.js';
import {PositionManager} from './positionManager.js';
import {TranscriptDrawer} from './transcriptDrawer.js';

const DEFAULT_BASE_URL = 'http://127.0.0.1:8765';
const WS_BACKOFF_MIN_MS = 1000;
const WS_BACKOFF_MAX_MS = 30000;

/**
 * GNOME Shell 46-50 Extension entrypoint for AI Pet Dynamic Island.
 */
export default class AiPetExtension extends Extension {
    enable() {
        this._disabled = false;
        this._exitedByUser = false;

        const envUrl = GLib.getenv('AI_PET_DAEMON_URL');
        this._baseUrl = (envUrl && envUrl.trim() ? envUrl.trim() : DEFAULT_BASE_URL).replace(/\/+$/, '');
        this._wsUrl = `${this._baseUrl.replace(/^http/i, 'ws')}/ws`;

        this._config = {
            general: {pet_name: 'Nova'},
            provider: {active_provider: 'ollama', model: ''},
            voice_in: {mic_muted: false},
            voice_out: {tts_muted: false},
            visuals: {
                animation_mode: 'full',
                cursor_tracking: true,
                position_mode: 'fixed',
                monitor_index: 0,
                fixed_alignment: 'top-center',
                offset_x: 0,
                offset_y: 12,
                float_x: 500,
                float_y: 40,
                chat_width: 380,
                chat_height: 420,
                chat_density: 'comfortable',
            },
        };

        this._activeSessionId = null;
        this._wsConn = null;
        this._wsSignalIds = [];
        this._wsBackoffMs = WS_BACKOFF_MIN_MS;
        this._reconnectTimerId = 0;

        this._cancellable = new Gio.Cancellable();
        this._httpSession = new Soup.Session({timeout: 10});

        this._positionManager = new PositionManager();

        this._drawer = new TranscriptDrawer(this._positionManager, {
            onSendText: text => this._handleSendText(text),
            onClearChat: () => this._handleNewChat(),
            onReplayTts: (messageId, text) => this._handleReplayTts(messageId, text),
            onManualMic: () => this._handleManualMic(),
            onConfirmTool: (callId, approved) => this._handleConfirmTool(callId, approved),
            onResizeCommit: (width, height) => {
                this._patchConfig({
                    visuals: {
                        chat_width: Math.round(width),
                        chat_height: Math.round(height),
                    },
                }).catch(() => {});
            },
            onDensityChange: density => {
                this._patchConfig({
                    visuals: {
                        chat_density: String(density),
                    },
                }).catch(() => {});
            },
            onOpenChanged: isOpen => {
                if (isOpen)
                    this._refreshActiveSession().catch(() => {});
            },
        });

        this._islandWidget = new IslandWidget(this._positionManager, {
            onLeftClick: () => this._handleLeftClick(),
            onRightClick: () => this._handleRightClick(),
            onMoved: bounds => {
                if (this._drawer?.isOpen) {
                    const monIdx = this._config.visuals?.monitor_index ?? 0;
                    this._drawer.updatePosition(
                        bounds.x,
                        bounds.y,
                        bounds.width,
                        bounds.height,
                        monIdx
                    );
                }
            },
            onDragCommit: (x, y) => this._persistFloatingPosition(x, y),
        });

        this._contextMenu = new IslandContextMenu(this._islandWidget.actor, {
            onToggleMicMute: muted => this._patchConfig({voice_in: {mic_muted: muted}}),
            onToggleTtsMute: muted => this._patchConfig({voice_out: {tts_muted: muted}}),
            onToggleFloating: floating => this._toggleFloatingMode(floating),
            onNewChat: () => this._handleNewChat(),
            onExit: () => this._handleExit(),
        });

        this._monitorsChangedId = Main.layoutManager.connect('monitors-changed', () => {
            if (this._disabled)
                return;
            this._islandWidget?.reposition();
            this._syncDrawerPosition();
        });

        this._applyConfig(this._config);
        this._connectWebSocket();
        this._loadInitialData().catch(() => {});
    }

    disable() {
        this._disabled = true;
        this._clearReconnectTimer();

        if (this._cancellable) {
            this._cancellable.cancel();
            this._cancellable = null;
        }

        this._closeWebSocket();

        if (this._httpSession) {
            try {
                this._httpSession.abort();
            } catch (_e) {
                // Ignore abort errors during teardown.
            }
            this._httpSession = null;
        }

        if (this._monitorsChangedId) {
            Main.layoutManager.disconnect(this._monitorsChangedId);
            this._monitorsChangedId = 0;
        }

        if (this._contextMenu) {
            this._contextMenu.destroy();
            this._contextMenu = null;
        }

        if (this._drawer) {
            this._drawer.destroy();
            this._drawer = null;
        }

        if (this._islandWidget) {
            this._islandWidget.destroy();
            this._islandWidget = null;
        }

        this._positionManager = null;
        this._config = null;
        this._activeSessionId = null;
    }

    _handleLeftClick() {
        if (this._disabled || !this._islandWidget || !this._drawer)
            return;
        this._contextMenu?.close();
        const b = this._islandWidget.getBounds();
        const monIdx = this._config?.visuals?.monitor_index ?? 0;
        this._drawer.toggle(b.x, b.y, b.width, b.height, monIdx);
    }

    _handleRightClick() {
        if (this._disabled || !this._islandWidget || !this._contextMenu)
            return;
        this._drawer?.close();
        this._contextMenu.toggle(this._islandWidget.isInLowerHalf());
    }

    _syncDrawerPosition() {
        if (this._disabled || !this._drawer?.isOpen || !this._islandWidget)
            return;
        const b = this._islandWidget.getBounds();
        const monIdx = this._config?.visuals?.monitor_index ?? 0;
        this._drawer.updatePosition(b.x, b.y, b.width, b.height, monIdx);
    }

    _applyConfig(config) {
        if (this._disabled || !config)
            return;

        this._config = {
            ...this._config,
            ...config,
            general: {...(this._config?.general ?? {}), ...(config.general ?? {})},
            provider: {...(this._config?.provider ?? {}), ...(config.provider ?? {})},
            voice_in: {...(this._config?.voice_in ?? {}), ...(config.voice_in ?? {})},
            voice_out: {...(this._config?.voice_out ?? {}), ...(config.voice_out ?? {})},
            visuals: {...(this._config?.visuals ?? {}), ...(config.visuals ?? {})},
        };

        this._islandWidget?.applyVisualsConfig(this._config.visuals);
        this._contextMenu?.syncFromConfig(this._config);
        this._drawer?.setConfig(this._config);
        this._syncDrawerPosition();
    }

    async _loadInitialData() {
        const cfgResp = await this._httpJson('GET', '/api/v1/config');
        if (cfgResp.ok && cfgResp.data)
            this._applyConfig(cfgResp.data);
        await this._refreshActiveSession();
    }

    async _refreshActiveSession() {
        let sessionResp = await this._httpJson('GET', '/api/v1/sessions/active');
        if (!sessionResp.ok && sessionResp.status === 404)
            sessionResp = await this._httpJson('GET', '/api/v1/sessions');

        if (!sessionResp.ok || !sessionResp.data)
            return;

        const payload = sessionResp.data;
        let sessionId = null;
        let messages = null;

        if (Array.isArray(payload)) {
            sessionId = payload[0]?.id ?? null;
        } else if (typeof payload === 'object') {
            sessionId = payload.active_session_id ??
                payload.session?.id ??
                payload.id ??
                payload.sessions?.[0]?.id ??
                null;
            if (Array.isArray(payload.messages))
                messages = payload.messages;
        }

        this._activeSessionId = sessionId;

        if (messages === null && sessionId) {
            const msgResp = await this._httpJson(
                'GET',
                `/api/v1/sessions/${encodeURIComponent(sessionId)}/messages`
            );
            if (msgResp.ok && msgResp.data) {
                messages = Array.isArray(msgResp.data)
                    ? msgResp.data
                    : (msgResp.data.messages ?? []);
            }
        }

        if (!this._disabled && Array.isArray(messages))
            this._drawer?.setMessages(messages);
    }

    async _patchConfig(patch) {
        this._applyConfig(patch);
        const resp = await this._httpJson('PATCH', '/api/v1/config', patch);
        if (resp.ok && resp.data && typeof resp.data === 'object')
            this._applyConfig(resp.data);
    }

    _persistFloatingPosition(x, y) {
        const roundedX = Math.round(x);
        const roundedY = Math.round(y);
        this._patchConfig({
            visuals: {
                float_x: roundedX,
                float_y: roundedY,
            },
        }).catch(() => {});
    }

    _toggleFloatingMode(floating) {
        const bounds = this._islandWidget?.getBounds() ?? {x: 500, y: 40};
        const visualsPatch = {
            position_mode: floating ? 'floating' : 'fixed',
        };
        if (floating) {
            visualsPatch.float_x = Math.round(bounds.x);
            visualsPatch.float_y = Math.round(bounds.y);
        }
        this._patchConfig({visuals: visualsPatch}).catch(() => {});
    }

    async _handleSendText(text) {
        const sessionTarget = this._activeSessionId
            ? encodeURIComponent(this._activeSessionId)
            : 'active';
        const body = {
            content: text,
            text,
            input_mode: 'text',
            source: 'text',
        };

        let resp = await this._httpJson('POST', `/api/v1/sessions/${sessionTarget}/messages`, body);
        if (!resp.ok && resp.status === 404) {
            resp = await this._httpJson('POST', '/api/v1/sessions', {
                action: 'message',
                session_id: this._activeSessionId,
                ...body,
            });
        }

        if (resp.ok && resp.data) {
            const msg = resp.data.user_message ?? resp.data.message ?? resp.data;
            if (msg && (msg.id || msg.message_id) && (msg.content || msg.text)) {
                this._drawer?.upsertMessageDelta({
                    message_id: msg.id ?? msg.message_id,
                    role: msg.role ?? 'user',
                    source: msg.input_mode ?? 'text',
                    text: msg.content ?? msg.text,
                    is_final: true,
                });
            }
            if (resp.data.session_id)
                this._activeSessionId = resp.data.session_id;
        }
    }

    async _handleNewChat() {
        let resp = await this._httpJson('POST', '/api/v1/sessions', {title: 'New Chat'});
        if (!resp.ok && resp.status === 405 && this._activeSessionId) {
            resp = await this._httpJson(
                'DELETE',
                `/api/v1/sessions/${encodeURIComponent(this._activeSessionId)}`
            );
        }
        if (!this._disabled) {
            if (resp.ok && resp.data)
                this._activeSessionId = resp.data.id ?? resp.data.session?.id ?? null;
            this._drawer?.clearMessages();
        }
    }

    async _handleReplayTts(messageId, text) {
        let resp = await this._httpJson('POST', '/api/v1/tts/replay', {
            message_id: messageId,
            text,
        });
        if (!resp.ok && resp.status === 404) {
            const sid = encodeURIComponent(this._activeSessionId ?? 'active');
            const mid = encodeURIComponent(messageId);
            resp = await this._httpJson('POST', `/api/v1/sessions/${sid}/messages/${mid}/replay`, {
                text,
            });
        }
    }

    async _handleManualMic() {
        const state = this._islandWidget?.state ?? 'IDLE';
        if (state === 'SPEAKING')
            await this._httpJson('POST', '/api/v1/voice/barge-in', {});
        else
            await this._httpJson('POST', '/api/v1/voice/trigger', {});
    }

    async _handleConfirmTool(callId, approved) {
        let resp = await this._httpJson('POST', '/api/v1/mcp/confirm', {
            call_id: callId,
            approved: Boolean(approved),
        });
        if (!resp.ok && resp.status === 404) {
            resp = await this._httpJson(
                'POST',
                `/api/v1/tools/${encodeURIComponent(callId)}/confirm`,
                {approved: Boolean(approved)}
            );
        }
    }

    async _handleExit() {
        this._exitedByUser = true;
        this._clearReconnectTimer();
        this._drawer?.close();
        this._contextMenu?.close();
        this._islandWidget?.hide();

        try {
            await this._httpJson('POST', '/api/v1/system/exit', {});
        } catch (_e) {
            // Daemon may terminate immediately after accepting exit.
        }
        this._closeWebSocket();
    }

    _httpJson(method, path, body = undefined) {
        return new Promise(resolve => {
            if (this._disabled || !this._httpSession) {
                resolve({ok: false, status: 0, data: null});
                return;
            }

            const url = `${this._baseUrl}${path}`;
            const msg = Soup.Message.new(method, url);
            if (!msg) {
                resolve({ok: false, status: 0, data: null});
                return;
            }

            if (body !== undefined) {
                const payload = new GLib.Bytes(new TextEncoder().encode(JSON.stringify(body)));
                msg.set_request_body_from_bytes('application/json', payload);
            }

            this._httpSession.send_and_read_async(
                msg,
                GLib.PRIORITY_DEFAULT,
                this._cancellable,
                (session, res) => {
                    try {
                        const bytes = session.send_and_read_finish(res);
                        const status = msg.get_status();
                        const arr = bytes ? bytes.toArray() : new Uint8Array(0);
                        const rawText = arr.length > 0 ? new TextDecoder().decode(arr) : '';
                        let data = null;
                        if (rawText) {
                            try {
                                data = JSON.parse(rawText);
                            } catch (_parseErr) {
                                data = rawText;
                            }
                        }
                        resolve({
                            ok: status >= 200 && status < 300,
                            status,
                            data,
                        });
                    } catch (_err) {
                        resolve({ok: false, status: 0, data: null});
                    }
                }
            );
        });
    }

    _connectWebSocket() {
        if (this._disabled || this._exitedByUser || !this._httpSession)
            return;

        this._clearReconnectTimer();
        this._closeWebSocket();

        const msg = Soup.Message.new('GET', this._wsUrl);
        if (!msg) {
            this._scheduleReconnect();
            return;
        }

        this._httpSession.websocket_connect_async(
            msg,
            null,
            null,
            GLib.PRIORITY_DEFAULT,
            this._cancellable,
            (session, res) => {
                let conn = null;
                try {
                    conn = session.websocket_connect_finish(res);
                } catch (_err) {
                    if (!this._disabled && !this._exitedByUser) {
                        this._drawer?.setConnectionStatus(false);
                        this._scheduleReconnect();
                    }
                    return;
                }

                if (this._disabled || this._exitedByUser || !conn) {
                    try {
                        conn?.close(Soup.WebsocketCloseCode.NORMAL, null);
                    } catch (_e) {
                        // Ignore close errors.
                    }
                    return;
                }

                this._wsConn = conn;
                this._wsBackoffMs = WS_BACKOFF_MIN_MS;
                this._drawer?.setConnectionStatus(true);
                this._islandWidget?.show();

                const msgId = conn.connect('message', (_c, dataType, bytes) => {
                    if (dataType !== Soup.WebsocketDataType.TEXT || !bytes)
                        return;
                    try {
                        const text = new TextDecoder().decode(bytes.toArray());
                        const event = JSON.parse(text);
                        this._handleWsEvent(event);
                    } catch (_e) {
                        // Ignore malformed JSON frames.
                    }
                });

                const errId = conn.connect('error', () => {
                    if (!this._disabled)
                        this._drawer?.setConnectionStatus(false);
                });

                const closedId = conn.connect('closed', () => {
                    this._closeWebSocket();
                    if (!this._disabled && !this._exitedByUser) {
                        this._drawer?.setConnectionStatus(false);
                        this._scheduleReconnect();
                    }
                });

                this._wsSignalIds = [msgId, errId, closedId];
                this._loadInitialData().catch(() => {});
            }
        );
    }

    _closeWebSocket() {
        if (this._wsConn) {
            for (const id of this._wsSignalIds) {
                try {
                    this._wsConn.disconnect(id);
                } catch (_e) {
                    // Ignore disconnect errors.
                }
            }
            this._wsSignalIds = [];
            try {
                if (this._wsConn.get_state() === Soup.WebsocketState.OPEN)
                    this._wsConn.close(Soup.WebsocketCloseCode.NORMAL, null);
            } catch (_e) {
                // Ignore close errors.
            }
            this._wsConn = null;
        }
    }

    _scheduleReconnect() {
        if (this._disabled || this._exitedByUser || this._reconnectTimerId)
            return;

        const delayMs = this._wsBackoffMs;
        this._wsBackoffMs = Math.min(WS_BACKOFF_MAX_MS, this._wsBackoffMs * 2);

        this._reconnectTimerId = GLib.timeout_add(GLib.PRIORITY_DEFAULT, delayMs, () => {
            this._reconnectTimerId = 0;
            this._connectWebSocket();
            return GLib.SOURCE_REMOVE;
        });
    }

    _clearReconnectTimer() {
        if (this._reconnectTimerId) {
            GLib.source_remove(this._reconnectTimerId);
            this._reconnectTimerId = 0;
        }
    }

    _handleWsEvent(event) {
        if (this._disabled || !event || typeof event !== 'object')
            return;

        switch (event.type) {
        case 'state_changed': {
            const state = event.state ?? 'IDLE';
            this._islandWidget?.setState(state, event.emotion, event.label);
            this._drawer?.setListening(String(state).toUpperCase() === 'LISTENING');
            break;
        }
        case 'audio_level': {
            this._islandWidget?.setAudioLevel(event.source, event.rms);
            break;
        }
        case 'transcript_delta': {
            if (event.session_id && event.session_id !== this._activeSessionId) {
                if (this._activeSessionId !== null)
                    this._drawer?.clearMessages();
                this._activeSessionId = event.session_id;
            }
            this._drawer?.upsertMessageDelta(event);
            break;
        }
        case 'tool_call_status': {
            this._drawer?.updateToolCall(event);
            const status = String(event.status ?? '').toLowerCase();
            if (status === 'running' && event.tool)
                this._islandWidget?.setState('TOOL_CALLING', undefined, String(event.tool));
            if (status === 'awaiting_confirm' && !this._drawer?.isOpen && this._islandWidget) {
                const b = this._islandWidget.getBounds();
                const monIdx = this._config?.visuals?.monitor_index ?? 0;
                this._drawer.open(b.x, b.y, b.width, b.height, monIdx);
            }
            break;
        }
        case 'config_updated': {
            if (event.config && typeof event.config === 'object')
                this._applyConfig(event.config);
            break;
        }
        case 'position_updated': {
            const visuals = event.visuals ?? event;
            this._applyConfig({visuals});
            break;
        }
        case 'shutdown': {
            this._exitedByUser = true;
            this._clearReconnectTimer();
            this._drawer?.close();
            this._contextMenu?.close();
            this._islandWidget?.hide();
            this._closeWebSocket();
            break;
        }
        default:
            break;
        }
    }
}
