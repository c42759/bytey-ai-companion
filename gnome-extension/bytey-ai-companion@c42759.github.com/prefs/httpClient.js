// SPDX-License-Identifier: GPL-3.0-or-later

import GLib from 'gi://GLib';
import Gio from 'gi://Gio';
import Soup from 'gi://Soup?version=3.0';

import {DEFAULT_DAEMON_URL, normalizeUrl} from './constants.js';

export function createPrefsHttpClient(settings) {
    const cancellable = new Gio.Cancellable();
    const httpSession = new Soup.Session({timeout: 8});
    const longHttpSession = new Soup.Session({timeout: 300});

    const resolveBaseUrl = () => {
        const envUrl = normalizeUrl(GLib.getenv('BYTEY_DAEMON_URL') || GLib.getenv('AI_PET_DAEMON_URL'));
        if (envUrl)
            return envUrl;
        const storedUrl = normalizeUrl(settings.get_string('daemon-url'));
        return storedUrl ?? DEFAULT_DAEMON_URL;
    };

    const httpJson = (method, path, body = undefined, {longTimeout = false} = {}) => new Promise(resolve => {
        const url = `${resolveBaseUrl()}${path}`;
        const msg = Soup.Message.new(method, url);
        if (!msg) {
            resolve({ok: false, status: 0, data: null, error: 'Invalid URL'});
            return;
        }
        if (body !== undefined) {
            const bytes = new GLib.Bytes(new TextEncoder().encode(JSON.stringify(body)));
            msg.set_request_body_from_bytes('application/json', bytes);
        }
        const session = longTimeout ? longHttpSession : httpSession;
        session.send_and_read_async(msg, GLib.PRIORITY_DEFAULT, cancellable, (s, res) => {
            try {
                const bytes = s.send_and_read_finish(res);
                const status = msg.get_status();
                const arr = bytes ? bytes.toArray() : new Uint8Array(0);
                const text = arr.length > 0 ? new TextDecoder().decode(arr) : '';
                let data = null;
                if (text) {
                    try {
                        data = JSON.parse(text);
                    } catch (_e) {
                        data = text;
                    }
                }
                resolve({ok: status >= 200 && status < 300, status, data, error: null});
            } catch (err) {
                resolve({ok: false, status: 0, data: null, error: err?.message ?? 'Request failed'});
            }
        });
    });

    let wsConn = null;
    let wsReconnectId = 0;

    const clearWsReconnect = () => {
        if (wsReconnectId) {
            GLib.Source.remove(wsReconnectId);
            wsReconnectId = 0;
        }
    };

    const connectConfigSocket = onConfigUpdated => {
        clearWsReconnect();
        if (cancellable.is_cancelled())
            return;
        const wsUrl = `${resolveBaseUrl().replace(/^http/, 'ws')}/ws`;
        const msg = Soup.Message.new('GET', wsUrl);
        if (!msg)
            return;
        httpSession.websocket_connect_async(msg, null, null, GLib.PRIORITY_DEFAULT, cancellable, (session, res) => {
            if (cancellable.is_cancelled())
                return;
            let conn = null;
            try {
                conn = session.websocket_connect_finish(res);
            } catch (_err) {
                if (!cancellable.is_cancelled()) {
                    clearWsReconnect();
                    wsReconnectId = GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, 3, () => {
                        wsReconnectId = 0;
                        connectConfigSocket(onConfigUpdated);
                        return GLib.SOURCE_REMOVE;
                    });
                }
                return;
            }
            wsConn = conn;
            conn.connect('message', (_c, type, bytes) => {
                if (type !== Soup.WebsocketDataType.TEXT || cancellable.is_cancelled())
                    return;
                const text = new TextDecoder().decode(bytes.toArray());
                let event = null;
                try {
                    event = JSON.parse(text);
                } catch (_err) {
                    return;
                }
                if (event && event.type === 'config_updated' && event.config && typeof event.config === 'object')
                    onConfigUpdated(event.config);
            });
            conn.connect('closed', () => {
                if (wsConn === conn)
                    wsConn = null;
                if (!cancellable.is_cancelled()) {
                    clearWsReconnect();
                    wsReconnectId = GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, 3, () => {
                        wsReconnectId = 0;
                        connectConfigSocket(onConfigUpdated);
                        return GLib.SOURCE_REMOVE;
                    });
                }
            });
        });
    };

    const abortAll = () => {
        clearWsReconnect();
        cancellable.cancel();
        if (wsConn) {
            wsConn.close(Soup.WebsocketCloseCode.NORMAL, null);
            wsConn = null;
        }
        httpSession.abort();
        longHttpSession.abort();
    };

    return {cancellable, resolveBaseUrl, httpJson, connectConfigSocket, abortAll};
}
