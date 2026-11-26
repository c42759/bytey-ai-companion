// SPDX-License-Identifier: GPL-3.0-or-later

/**
 * Parse `#RGB`, `#RRGGBB`, or `#RRGGBBAA` into normalized `[r, g, b, a]` (`0..1`).
 *
 * @param {string} hex
 * @param {number[]} fallback
 * @returns {number[]}
 */
export function parseHexColor(hex, fallback) {
    if (typeof hex !== 'string')
        return [...fallback];
    const clean = hex.trim();
    if (!clean.startsWith('#'))
        return [...fallback];
    const raw = clean.slice(1);
    if (!/^[0-9a-fA-F]+$/.test(raw))
        return [...fallback];

    if (raw.length === 3) {
        return [
            parseInt(raw[0] + raw[0], 16) / 255,
            parseInt(raw[1] + raw[1], 16) / 255,
            parseInt(raw[2] + raw[2], 16) / 255,
            1.0,
        ];
    }
    if (raw.length === 6) {
        return [
            parseInt(raw.slice(0, 2), 16) / 255,
            parseInt(raw.slice(2, 4), 16) / 255,
            parseInt(raw.slice(4, 6), 16) / 255,
            1.0,
        ];
    }
    if (raw.length === 8) {
        return [
            parseInt(raw.slice(0, 2), 16) / 255,
            parseInt(raw.slice(2, 4), 16) / 255,
            parseInt(raw.slice(4, 6), 16) / 255,
            parseInt(raw.slice(6, 8), 16) / 255,
        ];
    }
    return [...fallback];
}
