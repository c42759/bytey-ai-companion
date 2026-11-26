// SPDX-License-Identifier: GPL-3.0-or-later

import GLib from 'gi://GLib';
import Gio from 'gi://Gio';
import Soup from 'gi://Soup?version=3.0';

import {normalizeHttpUrl} from './sharedUtils.js';

const DEFAULT_BASE_URL = 'http://127.0.0.1:8765';
const WS_BACKOFF_MIN_MS = 1000;
const WS_BACKOFF_MAX_MS = 30000;
// Match daemon _MAX_BODY_BYTES so large MCP tool results are not rejected by libsoup (default 128 KiB).
const WS_MAX_INCOMING_PAYLOAD = 16 * 1024 * 1024;

/**
 * HTTP REST and WebSocket IPC client for the Bytey core daemon.
 */
export class DaemonClient {
    constructor(settings, callbacks = {}) {
        this._settings = settings;
        this._callbacks = callbacks;
        this._exitedByUser = false;
        this._wsConn = null;
        this._wsSignalIds = [];
        this._wsBackoffMs = WS_BACKOFF_MIN_MS;
        this._wsHadError = false;
        this._reconnectTimerId = 0;

        this._cancellable = new Gio.Cancellable();
        this._httpSession = new Soup.Session({timeout: 10});
        this._baseUrl = DEFAULT_BASE_URL;
        this._wsUrl = `${DEFAULT_BASE_URL.replace(/^http/i, 'ws')}/ws`;
        this.resolveUrls();
    }

    get active() {
        return Boolean(this._httpSession);
    }

    get exitedByUser() {
        return this._exitedByUser;
    }

    resolveUrls() {
        const envUrl = normalizeHttpUrl(GLib.getenv('BYTEY_DAEMON_URL') || GLib.getenv('AI_PET_DAEMON_URL'));
        const settingsUrl = normalizeHttpUrl(this._settings?.get_string('daemon-url'));
        this._baseUrl = envUrl ?? settingsUrl ?? DEFAULT_BASE_URL;
        this._wsUrl = `${this._baseUrl.replace(/^http/i, 'ws')}/ws`;
    }

    onDaemonUrlChanged() {
        if (!this._httpSession)
            return false;
        const prev = this._baseUrl;
        this.resolveUrls();
        if (this._baseUrl !== prev) {
            this._exitedByUser = false;
            this._wsBackoffMs = WS_BACKOFF_MIN_MS;
            this.connectWebSocket();
            return true;
        }
        return false;
    }

    markExited() {
        this._exitedByUser = true;
        this._wsHadError = false;
        this._clearReconnectTimer();
    }

    destroy() {
        this._clearReconnectTimer();

        if (this._cancellable) {
            this._cancellable.cancel();
            this._cancellable = null;
        }

        this.closeWebSocket();

        if (this._httpSession) {
            this._httpSession.abort();
            this._httpSession = null;
        }

        this._settings = null;
        this._callbacks = null;
        this._baseUrl = null;
        this._wsUrl = null;
        this._exitedByUser = false;
        this._wsHadError = false;
        this._wsBackoffMs = WS_BACKOFF_MIN_MS;
    }

    httpJson(method, path, body = undefined) {
        return new Promise(resolve => {
            if (!this._httpSession) {
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

    connectWebSocket() {
        if (!this._httpSession || this._exitedByUser)
            return;

        this._clearReconnectTimer();
        this.closeWebSocket();

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
                    if (this._httpSession && !this._exitedByUser) {
                        this._wsHadError = true;
                        this._callbacks?.onStatus?.('error');
                        this._scheduleReconnect();
                    }
                    return;
                }

                if (!this._httpSession || this._exitedByUser || !conn) {
                    if (conn)
                        conn.close(Soup.WebsocketCloseCode.NORMAL, null);
                    return;
                }

                this._wsConn = conn;
                conn.max_incoming_payload_size = WS_MAX_INCOMING_PAYLOAD;
                this._wsBackoffMs = WS_BACKOFF_MIN_MS;
                this._wsHadError = false;
                this._callbacks?.onConnected?.();

                const msgId = conn.connect('message', (_c, dataType, bytes) => {
                    if (dataType !== Soup.WebsocketDataType.TEXT || !bytes)
                        return;
                    try {
                        const text = new TextDecoder().decode(bytes.toArray());
                        const event = JSON.parse(text);
                        this._callbacks?.onEvent?.(event);
                    } catch (_e) {
                        // Ignore malformed JSON frames.
                    }
                });

                const errId = conn.connect('error', () => {
                    this._wsHadError = true;
                    if (this._httpSession)
                        this._callbacks?.onStatus?.('error');
                });

                const closedId = conn.connect('closed', () => {
                    const closedAfterError = this._wsHadError;
                    this.closeWebSocket();
                    if (this._httpSession && !this._exitedByUser) {
                        this._callbacks?.onStatus?.(closedAfterError ? 'error' : 'disconnected');
                        this._scheduleReconnect();
                    }
                });

                this._wsSignalIds = [msgId, errId, closedId];
            }
        );
    }

    closeWebSocket() {
        if (this._wsConn) {
            for (const id of this._wsSignalIds)
                this._wsConn.disconnect(id);
            this._wsSignalIds = [];
            if (this._wsConn.get_state() === Soup.WebsocketState.OPEN)
                this._wsConn.close(Soup.WebsocketCloseCode.NORMAL, null);
            this._wsConn = null;
        }
    }

    _scheduleReconnect() {
        if (!this._httpSession || this._exitedByUser || this._reconnectTimerId)
            return;

        const delayMs = this._wsBackoffMs;
        this._wsBackoffMs = Math.min(WS_BACKOFF_MAX_MS, this._wsBackoffMs * 2);

        if (this._reconnectTimerId) {
            GLib.Source.remove(this._reconnectTimerId);
            this._reconnectTimerId = 0;
        }
        this._reconnectTimerId = GLib.timeout_add(GLib.PRIORITY_DEFAULT, delayMs, () => {
            this._reconnectTimerId = 0;
            this.connectWebSocket();
            return GLib.SOURCE_REMOVE;
        });
    }

    _clearReconnectTimer() {
        if (this._reconnectTimerId) {
            GLib.Source.remove(this._reconnectTimerId);
            this._reconnectTimerId = 0;
        }
    }

    getConfig() {
        return this.httpJson('GET', '/api/v1/config');
    }

    patchConfig(patch) {
        return this.httpJson('PATCH', '/api/v1/config', patch);
    }

    async getActiveSessionOrList() {
        let resp = await this.httpJson('GET', '/api/v1/sessions/active');
        if (!resp.ok && resp.status === 404)
            resp = await this.httpJson('GET', '/api/v1/sessions');
        return resp;
    }

    getSessions() {
        return this.httpJson('GET', '/api/v1/sessions');
    }

    getSessionMessages(sessionId) {
        return this.httpJson('GET', `/api/v1/sessions/${encodeURIComponent(sessionId)}/messages`);
    }

    activateSession(sessionId) {
        return this.httpJson('POST', `/api/v1/sessions/${encodeURIComponent(sessionId)}/activate`, {});
    }

    deleteSession(sessionId) {
        return this.httpJson('DELETE', `/api/v1/sessions/${encodeURIComponent(sessionId)}`);
    }

    renameSession(sessionId, title) {
        return this.httpJson('PATCH', `/api/v1/sessions/${encodeURIComponent(sessionId)}`, {title});
    }

    async createOrResetSession(activeSessionId) {
        let resp = await this.httpJson('POST', '/api/v1/sessions', {title: 'New Chat'});
        if (!resp.ok && resp.status === 405 && activeSessionId)
            resp = await this.deleteSession(activeSessionId);
        return resp;
    }

    async sendMessage(activeSessionId, body) {
        const sessionTarget = activeSessionId ? encodeURIComponent(activeSessionId) : 'active';
        let resp = await this.httpJson('POST', `/api/v1/sessions/${sessionTarget}/messages`, body);
        if (!resp.ok && resp.status === 404) {
            resp = await this.httpJson('POST', '/api/v1/sessions', {
                action: 'message',
                session_id: activeSessionId,
                ...body,
            });
        }
        return resp;
    }

    deleteMessage(sessionId, messageId) {
        const sid = encodeURIComponent(sessionId ?? 'active');
        const mid = encodeURIComponent(messageId);
        return this.httpJson('DELETE', `/api/v1/sessions/${sid}/messages/${mid}`);
    }

    async replayTts(activeSessionId, messageId, text) {
        const resp = await this.httpJson('POST', '/api/v1/tts/replay', {
            message_id: messageId,
            text,
        });
        if (!resp.ok && resp.status === 404) {
            const sid = encodeURIComponent(activeSessionId ?? 'active');
            const mid = encodeURIComponent(messageId);
            return this.httpJson('POST', `/api/v1/sessions/${sid}/messages/${mid}/replay`, {text});
        }
        return resp;
    }

    pauseTts() {
        return this.httpJson('POST', '/api/v1/tts/pause', {});
    }

    triggerVoice(bargeIn) {
        return this.httpJson('POST', bargeIn ? '/api/v1/voice/barge-in' : '/api/v1/voice/trigger', {});
    }

    async confirmTool(requestId, approved) {
        const flag = Boolean(approved);
        let resp = await this.httpJson('POST', '/api/v1/tools/confirm', {
            request_id: requestId,
            approved: flag,
        });
        if (!resp.ok && resp.status === 404) {
            resp = await this.httpJson('POST', '/api/v1/mcp/confirm', {
                call_id: requestId,
                approved: flag,
            });
        }
        if (!resp.ok && resp.status === 404) {
            resp = await this.httpJson(
                'POST',
                `/api/v1/tools/${encodeURIComponent(requestId)}/confirm`,
                {approved: flag}
            );
        }
        return resp;
    }

    getWorkspace() {
        return this.httpJson('GET', '/api/v1/workspace');
    }

    setWorkspace(path) {
        return this.httpJson('PUT', '/api/v1/workspace', {path: String(path ?? '').trim()});
    }

    listModels(provider) {
        return this.httpJson('GET', `/api/v1/providers/${encodeURIComponent(String(provider))}/models`);
    }

    getSkills() {
        return this.httpJson('GET', '/api/v1/skills');
    }

    exitSystem() {
        return this.httpJson('POST', '/api/v1/system/exit', {});
    }
}
