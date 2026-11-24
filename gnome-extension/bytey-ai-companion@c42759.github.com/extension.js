// SPDX-License-Identifier: GPL-3.0-or-later

import GLib from 'gi://GLib';
import Gio from 'gi://Gio';
import Soup from 'gi://Soup?version=3.0';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';

import {IslandContextMenu} from './contextMenu.js';
import {IslandWidget} from './islandWidget.js';
import {PositionManager} from './positionManager.js';
import {TranscriptDrawer} from './transcriptDrawer.js';

const DEFAULT_BASE_URL = 'http://127.0.0.1:8765';
const WS_BACKOFF_MIN_MS = 1000;
const WS_BACKOFF_MAX_MS = 30000;
// Match daemon _MAX_BODY_BYTES so large MCP tool results are not rejected by libsoup (default 128 KiB).
const WS_MAX_INCOMING_PAYLOAD = 16 * 1024 * 1024;

/**
 * GNOME Shell 48-51 Extension entrypoint for Bytey AI Companion Dynamic Island.
 */
export default class ByteyExtension extends Extension {
    enable() {
        this._disabled = false;
        this._exitedByUser = false;

        this._settings = this.getSettings();
        this._resolveDaemonUrls();
        this._settingsChangedId = this._settings.connect('changed::daemon-url', () => {
            if (this._disabled)
                return;
            const prev = this._baseUrl;
            this._resolveDaemonUrls();
            if (this._baseUrl !== prev) {
                this._exitedByUser = false;
                this._wsBackoffMs = WS_BACKOFF_MIN_MS;
                this._connectWebSocket();
                this._loadInitialData().catch(() => {});
            }
        });

        this._config = {
            general: {pet_name: 'Bytey'},
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
                eye_color: '#F7FAFF',
                background_color: '#0A0A0C',
                border_color: '#2A2C34',
            },
        };

        this._activeSessionId = null;
        this._sessions = [];
        this._daemonState = 'IDLE';
        this._wsConn = null;
        this._wsSignalIds = [];
        this._wsBackoffMs = WS_BACKOFF_MIN_MS;
        this._wsHadError = false;
        this._reconnectTimerId = 0;

        this._cancellable = new Gio.Cancellable();
        this._httpSession = new Soup.Session({timeout: 10});

        this._positionManager = new PositionManager();

        this._drawer = new TranscriptDrawer(this._positionManager, {
            onSendText: text => this._handleSendText(text),
            onClearChat: () => this._handleNewChat(),
            onReplayTts: (messageId, text) => this._handleReplayTts(messageId, text),
            onDeleteMessage: messageId => this._handleDeleteMessage(messageId),
            onTogglePauseTts: () => this._handleTogglePauseTts(),
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
            onAutoAcceptChange: enabled => {
                this._patchConfig({
                    workspace: {
                        require_write_approval: !enabled,
                        require_command_approval: !enabled,
                    },
                }).catch(() => {});
            },
            onGodModeChange: enabled => {
                this._patchConfig({
                    agents: {
                        god_mode_enabled: Boolean(enabled),
                    },
                }).catch(() => {});
            },
            onOpenChanged: isOpen => {
                if (isOpen && !this._drawer?.hasPendingConfirmation() && !this._isDaemonBusy())
                    this._refreshActiveSession().catch(() => {});
            },
            onSelectSession: sessionId => this._handleSelectSession(sessionId),
            onDeleteSession: sessionId => this._handleDeleteSession(sessionId),
            onRenameSession: (sessionId, title) => this._handleRenameSession(sessionId, title),
            onCreateSession: () => this._handleNewChat(),
            onToggleSidebar: isOpen => {
                if (isOpen)
                    this._refreshSessionsList().catch(() => {});
            },
            onSelectWorkspace: path => this._handleSelectWorkspace(path),
            onOpenWorkspaceSettings: () => this.openPreferences(),
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
            onOpenPreferences: () => this.openPreferences(),
            onToggleMicMute: muted => this._patchConfig({voice_in: {mic_muted: muted}}),
            onToggleTtsMute: muted => this._patchConfig({voice_out: {tts_muted: muted}}),
            onNewChat: () => this._handleNewChat(),
            onExit: () => this._handleExit(),
        });

        this._monitorsChangedId = Main.layoutManager.connect('monitors-changed', () => {
            if (this._disabled)
                return;
            this._islandWidget?.reposition();
            this._syncDrawerPosition();
        });
        this._workareasChangedId = global.display.connect('workareas-changed', () => {
            if (this._disabled)
                return;
            this._islandWidget?.reposition();
            this._syncDrawerPosition();
        });
        this._overviewShowingId = Main.overview.connect('showing', () => {
            if (this._disabled)
                return;
            this._drawer?.close();
            this._contextMenu?.close();
            this._islandWidget?.fadeOut();
        });
        this._overviewHiddenId = Main.overview.connect('hidden', () => {
            if (this._disabled || this._exitedByUser)
                return;
            this._islandWidget?.fadeIn();
        });
        if (Main.overview.visible)
            this._islandWidget?.hide();

        this._applyConfig(this._config);
        this._connectWebSocket();
        this._loadInitialData().catch(() => {});
    }

    disable() {
        this._disabled = true;
        this._clearReconnectTimer();

        if (this._settings && this._settingsChangedId) {
            this._settings.disconnect(this._settingsChangedId);
            this._settingsChangedId = 0;
        }
        this._settings = null;

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
        if (this._workareasChangedId) {
            global.display.disconnect(this._workareasChangedId);
            this._workareasChangedId = 0;
        }
        if (this._overviewShowingId) {
            Main.overview.disconnect(this._overviewShowingId);
            this._overviewShowingId = 0;
        }
        if (this._overviewHiddenId) {
            Main.overview.disconnect(this._overviewHiddenId);
            this._overviewHiddenId = 0;
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
        this._sessions = [];
        this._daemonState = 'IDLE';
        this._baseUrl = null;
        this._wsUrl = null;
        this._wsSignalIds = [];
        this._exitedByUser = false;
        this._wsHadError = false;
        this._wsBackoffMs = WS_BACKOFF_MIN_MS;
    }

    _isValidHttpUrl(url) {
        if (typeof url !== 'string')
            return false;
        const trimmed = url.trim();
        if (!/^https?:\/\/\S+$/i.test(trimmed))
            return false;
        const stripped = trimmed.replace(/\/+$/, '');
        return /^https?:\/\/\S+$/i.test(stripped);
    }

    _resolveDaemonUrls() {
        const envUrl = GLib.getenv('BYTEY_DAEMON_URL') || GLib.getenv('AI_PET_DAEMON_URL');
        const settingsUrl = this._settings?.get_string('daemon-url');
        let chosen = DEFAULT_BASE_URL;
        if (this._isValidHttpUrl(envUrl))
            chosen = envUrl.trim().replace(/\/+$/, '');
        else if (this._isValidHttpUrl(settingsUrl))
            chosen = settingsUrl.trim().replace(/\/+$/, '');
        this._baseUrl = chosen;
        this._wsUrl = `${this._baseUrl.replace(/^http/i, 'ws')}/ws`;
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
            workspace: {...(this._config?.workspace ?? {}), ...(config.workspace ?? {})},
            agents: {...(this._config?.agents ?? {}), ...(config.agents ?? {})},
        };

        this._islandWidget?.applyVisualsConfig(this._config.visuals);
        this._islandWidget?.setGodMode(Boolean(this._config.agents?.god_mode_enabled));
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
            this._sessions = payload;
        } else if (typeof payload === 'object') {
            sessionId = payload.active_session_id ??
                payload.session?.id ??
                payload.id ??
                payload.sessions?.[0]?.id ??
                null;
            if (Array.isArray(payload.messages))
                messages = payload.messages;
            if (Array.isArray(payload.sessions))
                this._sessions = payload.sessions;
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

        await this._refreshSessionsList();
        await this._refreshSkillsList();
        await this._refreshWorkspace();
    }

    async _refreshWorkspace() {
        const resp = await this._httpJson('GET', '/api/v1/workspace');
        if (this._disabled || !resp.ok || !resp.data)
            return;
        this._drawer?.setWorkspace(resp.data);
    }

    async _handleSelectWorkspace(path) {
        if (this._disabled)
            return;
        const resp = await this._httpJson('PUT', '/api/v1/workspace', {
            path: String(path ?? '').trim(),
        });
        if (this._disabled || !resp.ok)
            return;
        if (resp.data)
            this._drawer?.setWorkspace(resp.data);
        this._refreshSkillsList().catch(() => {});
    }

    async _refreshSkillsList() {
        const resp = await this._httpJson('GET', '/api/v1/skills');
        if (this._disabled || !resp.ok || !resp.data)
            return;

        const payload = resp.data;
        const skills = Array.isArray(payload)
            ? payload
            : (Array.isArray(payload.skills) ? payload.skills : []);
        const enabled = typeof payload.enabled === 'boolean'
            ? payload.enabled
            : true;
        this._drawer?.setSkills(skills, enabled);
    }

    async _refreshSessionsList() {
        const resp = await this._httpJson('GET', '/api/v1/sessions');
        if (this._disabled || !resp.ok || !resp.data)
            return;

        const payload = resp.data;
        const sessions = Array.isArray(payload)
            ? payload
            : (Array.isArray(payload.sessions) ? payload.sessions : []);
        this._sessions = sessions;

        if (!this._activeSessionId && payload && typeof payload === 'object') {
            this._activeSessionId = payload.active_session_id ??
                sessions[0]?.id ??
                null;
        }

        this._drawer?.setSessions(this._sessions, this._activeSessionId);
    }

    _isDaemonBusy() {
        const state = String(this._daemonState ?? this._islandWidget?.state ?? 'IDLE').toUpperCase();
        return state === 'THINKING' || state === 'TOOL_CALLING' || state === 'SPEAKING';
    }

    async _handleSelectSession(sessionId) {
        if (this._disabled || !sessionId || this._isDaemonBusy())
            return;

        if (sessionId === this._activeSessionId) {
            this._drawer?.focusInput();
            return;
        }

        const resp = await this._httpJson(
            'POST',
            `/api/v1/sessions/${encodeURIComponent(sessionId)}/activate`,
            {}
        );
        if (this._disabled || !resp.ok || !resp.data)
            return;

        const nextId = resp.data.active_session_id ?? sessionId;
        const messages = Array.isArray(resp.data.messages) ? resp.data.messages : [];
        this._activeSessionId = nextId;
        this._drawer?.setSessions(this._sessions, this._activeSessionId);
        this._drawer?.loadMessages(messages);
        this._drawer?.focusInput();
    }

    async _handleDeleteSession(sessionId) {
        if (this._disabled || !sessionId)
            return;

        const wasActive = sessionId === this._activeSessionId;
        if (wasActive && this._isDaemonBusy())
            return;

        const resp = await this._httpJson(
            'DELETE',
            `/api/v1/sessions/${encodeURIComponent(sessionId)}`
        );
        if (this._disabled || !resp.ok)
            return;

        const nextActiveId = resp.data?.active_session_id ?? null;
        if (wasActive || (nextActiveId && nextActiveId !== this._activeSessionId)) {
            this._activeSessionId = nextActiveId;
            if (this._activeSessionId) {
                const msgResp = await this._httpJson(
                    'GET',
                    `/api/v1/sessions/${encodeURIComponent(this._activeSessionId)}/messages`
                );
                if (!this._disabled && msgResp.ok && msgResp.data) {
                    const messages = Array.isArray(msgResp.data)
                        ? msgResp.data
                        : (msgResp.data.messages ?? []);
                    this._drawer?.loadMessages(messages);
                }
            } else {
                this._drawer?.clearMessages();
            }
        }

        await this._refreshSessionsList();
    }

    async _handleRenameSession(sessionId, title) {
        if (this._disabled || !sessionId)
            return;

        const cleanTitle = typeof title === 'string' ? title.trim() : '';
        if (!cleanTitle)
            return;

        const resp = await this._httpJson(
            'PATCH',
            `/api/v1/sessions/${encodeURIComponent(sessionId)}`,
            {title: cleanTitle}
        );
        if (this._disabled || !resp.ok)
            return;

        await this._refreshSessionsList();
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
        if (this._disabled || this._isDaemonBusy())
            return;

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
            await this._refreshSessionsList();
            this._drawer?.focusInput();
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

    async _handleDeleteMessage(messageId) {
        if (this._disabled || !messageId)
            return;
        const sid = encodeURIComponent(this._activeSessionId ?? 'active');
        const mid = encodeURIComponent(messageId);
        const resp = await this._httpJson('DELETE', `/api/v1/sessions/${sid}/messages/${mid}`);
        if (this._disabled || !resp.ok)
            return;
        this._drawer?.removeMessage(messageId);
        await this._refreshSessionsList();
    }

    async _handleTogglePauseTts() {
        if (this._disabled)
            return;
        await this._httpJson('POST', '/api/v1/tts/pause', {});
    }

    async _handleManualMic() {
        const state = this._islandWidget?.state ?? 'IDLE';
        if (state === 'SPEAKING')
            await this._httpJson('POST', '/api/v1/voice/barge-in', {});
        else
            await this._httpJson('POST', '/api/v1/voice/trigger', {});
    }

    async _handleConfirmTool(requestId, approved) {
        let resp = await this._httpJson('POST', '/api/v1/tools/confirm', {
            request_id: requestId,
            approved: Boolean(approved),
        });
        if (!resp.ok && resp.status === 404) {
            resp = await this._httpJson('POST', '/api/v1/mcp/confirm', {
                call_id: requestId,
                approved: Boolean(approved),
            });
        }
        if (!resp.ok && resp.status === 404) {
            resp = await this._httpJson(
                'POST',
                `/api/v1/tools/${encodeURIComponent(requestId)}/confirm`,
                {approved: Boolean(approved)}
            );
        }
    }

    async _handleExit() {
        this._exitedByUser = true;
        this._wsHadError = false;
        this._clearReconnectTimer();
        this._drawer?.setConnectionStatus('disconnected');
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
                        this._wsHadError = true;
                        this._drawer?.setConnectionStatus('error');
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
                try {
                    conn.max_incoming_payload_size = WS_MAX_INCOMING_PAYLOAD;
                } catch (_e) {
                    // Older libsoup: keep default limit.
                }
                this._wsBackoffMs = WS_BACKOFF_MIN_MS;
                this._wsHadError = false;
                this._drawer?.setConnectionStatus('connected');
                if (!Main.overview.visible)
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
                    this._wsHadError = true;
                    if (!this._disabled)
                        this._drawer?.setConnectionStatus('error');
                });

                const closedId = conn.connect('closed', () => {
                    const closedAfterError = this._wsHadError;
                    this._closeWebSocket();
                    if (!this._disabled && !this._exitedByUser) {
                        this._drawer?.setConnectionStatus(closedAfterError ? 'error' : 'disconnected');
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
            GLib.Source.remove(this._reconnectTimerId);
            this._reconnectTimerId = 0;
        }
    }

    _handleWsEvent(event) {
        if (this._disabled || !event || typeof event !== 'object')
            return;

        switch (event.type) {
        case 'state_changed': {
            const state = event.state ?? 'IDLE';
            this._daemonState = String(state).toUpperCase();
            this._islandWidget?.setState(state, event.emotion, event.label);
            this._drawer?.setListening(this._daemonState === 'LISTENING');
            if (this._daemonState !== 'SPEAKING')
                this._drawer?.setSpeakingMessage(null, 'idle');
            else if (event.speaking_message_id)
                this._drawer?.setSpeakingMessage(event.speaking_message_id, 'playing');
            break;
        }
        case 'tts_playback': {
            this._drawer?.setSpeakingMessage(
                event.message_id ?? null,
                event.status ?? 'idle'
            );
            break;
        }
        case 'message_deleted': {
            this._drawer?.removeMessage(event.message_id);
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
            const status = String(event.status ?? '').toLowerCase();
            if (status === 'running' && event.tool)
                this._islandWidget?.setState('TOOL_CALLING', undefined, String(event.tool));
            if (status === 'awaiting_confirm' && !this._drawer?.isOpen && this._islandWidget) {
                const b = this._islandWidget.getBounds();
                const monIdx = this._config?.visuals?.monitor_index ?? 0;
                this._drawer.open(b.x, b.y, b.width, b.height, monIdx);
            }
            this._drawer?.updateToolCall(event);
            break;
        }
        case 'tool_confirmation_required': {
            if (!this._drawer?.isOpen && this._islandWidget) {
                const b = this._islandWidget.getBounds();
                const monIdx = this._config?.visuals?.monitor_index ?? 0;
                this._drawer.open(b.x, b.y, b.width, b.height, monIdx);
            }
            this._drawer?.showToolConfirmation(event);
            break;
        }
        case 'workspace_updated': {
            this._drawer?.setWorkspace(event.workspace ?? event);
            this._refreshSkillsList().catch(() => {});
            break;
        }
        case 'config_updated': {
            if (event.config && typeof event.config === 'object') {
                this._applyConfig(event.config);
                if (event.config.skills !== undefined)
                    this._refreshSkillsList().catch(() => {});
            }
            break;
        }
        case 'skills_updated': {
            if (Array.isArray(event.skills))
                this._drawer?.setSkills(event.skills, event.enabled ?? true);
            else
                this._refreshSkillsList().catch(() => {});
            break;
        }
        case 'position_updated': {
            const visuals = event.visuals ?? event;
            this._applyConfig({visuals});
            break;
        }
        case 'sessions_updated': {
            if (Array.isArray(event.sessions)) {
                this._sessions = event.sessions;
                if (event.active_session_id && event.active_session_id !== this._activeSessionId) {
                    this._activeSessionId = event.active_session_id;
                    if (Array.isArray(event.messages))
                        this._drawer?.loadMessages(event.messages);
                }
                this._drawer?.setSessions(this._sessions, this._activeSessionId);
            } else {
                this._refreshSessionsList().catch(() => {});
            }
            break;
        }
        case 'session_activated': {
            const nextId = event.session_id ?? event.active_session_id ?? null;
            if (nextId)
                this._activeSessionId = nextId;
            if (Array.isArray(event.messages)) {
                this._drawer?.loadMessages(event.messages);
            } else if (this._activeSessionId) {
                this._httpJson(
                    'GET',
                    `/api/v1/sessions/${encodeURIComponent(this._activeSessionId)}/messages`
                ).then(msgResp => {
                    if (!this._disabled && msgResp.ok && msgResp.data) {
                        const msgs = Array.isArray(msgResp.data)
                            ? msgResp.data
                            : (msgResp.data.messages ?? []);
                        this._drawer?.loadMessages(msgs);
                    }
                }).catch(() => {});
            }
            this._drawer?.setSessions(this._sessions, this._activeSessionId);
            break;
        }
        case 'shutdown': {
            this._exitedByUser = true;
            this._wsHadError = false;
            this._clearReconnectTimer();
            this._drawer?.setConnectionStatus('disconnected');
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
