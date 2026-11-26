// SPDX-License-Identifier: GPL-3.0-or-later

import GLib from 'gi://GLib';
import Gio from 'gi://Gio';

/**
 * File attachment picker using xdg-desktop-portal with zenity fallback.
 */
export class AttachmentPicker {
    constructor(callbacks = {}) {
        this._callbacks = callbacks;
        this._portalSubId = 0;
        this._cancellable = null;
    }

    pick() {
        this._callbacks?.onBegin?.();
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
            if (this._cancellable)
                this._cancellable.cancel();
            this._cancellable = new Gio.Cancellable();

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
                    this._callbacks?.onEnd?.();
                    if (!this._callbacks)
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
                    this._callbacks?.onPaths?.(paths);
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
                this._cancellable,
                (conn, res) => {
                    try {
                        conn.call_finish(res);
                    } catch (_err) {
                        if (this._portalSubId) {
                            Gio.DBus.session.signal_unsubscribe(this._portalSubId);
                            this._portalSubId = 0;
                        }
                        if (this._callbacks)
                            this._pickZenity();
                    }
                }
            );
        } catch (_e) {
            this._pickZenity();
        }
    }

    _pickZenity() {
        if (!this._callbacks)
            return;
        try {
            if (this._cancellable)
                this._cancellable.cancel();
            this._cancellable = new Gio.Cancellable();
            const proc = Gio.Subprocess.new(
                ['zenity', '--file-selection', '--multiple', '--separator=\n', '--title=Attach files'],
                Gio.SubprocessFlags.STDOUT_PIPE
            );
            proc.communicate_utf8_async(null, this._cancellable, (p, res) => {
                try {
                    const [, stdout] = p.communicate_utf8_finish(res);
                    if (p.get_successful() && stdout) {
                        const paths = stdout.split('\n').map(s => s.trim()).filter(Boolean);
                        this._callbacks?.onPaths?.(paths);
                    }
                } catch (_err) {
                    // User cancelled or zenity unavailable.
                } finally {
                    this._callbacks?.onEnd?.();
                }
            });
        } catch (_e) {
            this._callbacks?.onEnd?.();
        }
    }

    destroy() {
        if (this._portalSubId) {
            Gio.DBus.session.signal_unsubscribe(this._portalSubId);
            this._portalSubId = 0;
        }
        if (this._cancellable) {
            this._cancellable.cancel();
            this._cancellable = null;
        }
        this._callbacks = null;
    }
}
