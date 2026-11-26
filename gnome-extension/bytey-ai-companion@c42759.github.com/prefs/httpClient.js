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

    const abortAll = () => {
        cancellable.cancel();
        httpSession.abort();
        longHttpSession.abort();
    };

    return {cancellable, resolveBaseUrl, httpJson, abortAll};
}
