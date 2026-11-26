// SPDX-License-Identifier: GPL-3.0-or-later

/**
 * Validate and normalize an HTTP(S) URL by trimming whitespace and trailing slashes.
 *
 * @param {string} raw
 * @returns {string|null}
 */
export function normalizeHttpUrl(raw) {
    if (typeof raw !== 'string')
        return null;
    const trimmed = raw.trim();
    if (!/^https?:\/\/\S+$/i.test(trimmed))
        return null;
    const stripped = trimmed.replace(/\/+$/, '');
    return /^https?:\/\/\S+$/i.test(stripped) ? stripped : null;
}

/**
 * Connect a GObject signal and record `[obj, id]` for deterministic teardown.
 *
 * @param {Array} signalIds
 * @param {object} obj
 * @param {string} signal
 * @param {Function} handler
 * @returns {number}
 */
export function connectTracked(signalIds, obj, signal, handler) {
    const id = obj.connect(signal, handler);
    signalIds.push([obj, id]);
    return id;
}
