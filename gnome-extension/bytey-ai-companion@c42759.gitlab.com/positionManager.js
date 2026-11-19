// SPDX-License-Identifier: GPL-3.0-or-later

import * as Main from 'resource:///org/gnome/shell/ui/main.js';

const DEFAULT_FALLBACK_MONITOR = {
    x: 0,
    y: 0,
    width: 1920,
    height: 1080,
};

/**
 * Resolves monitor geometry, 9-point Fixed alignments, and Floating coordinate bounds.
 */
export class PositionManager {
    /**
     * Resolve a valid monitor index from `Main.layoutManager.monitors`.
     *
     * @param {number} [monitorIndex=0]
     * @returns {number}
     */
    resolveMonitorIndex(monitorIndex = 0) {
        const monitors = Main.layoutManager?.monitors ?? [];
        if (monitors.length === 0)
            return -1;

        if (Number.isInteger(monitorIndex) && monitorIndex >= 0 && monitorIndex < monitors.length)
            return monitorIndex;

        const primary = Main.layoutManager.primaryIndex;
        if (Number.isInteger(primary) && primary >= 0 && primary < monitors.length)
            return primary;

        return 0;
    }

    /**
     * Return full `{ x, y, width, height }` bounds for the resolved monitor.
     *
     * @param {number} [monitorIndex=0]
     * @returns {{ x: number, y: number, width: number, height: number }}
     */
    getMonitorGeometry(monitorIndex = 0) {
        const monitors = Main.layoutManager?.monitors ?? [];
        const idx = this.resolveMonitorIndex(monitorIndex);
        if (idx >= 0 && monitors[idx]) {
            const m = monitors[idx];
            return {x: m.x, y: m.y, width: m.width, height: m.height};
        }
        if (Main.layoutManager?.primaryMonitor) {
            const p = Main.layoutManager.primaryMonitor;
            return {x: p.x, y: p.y, width: p.width, height: p.height};
        }
        return {...DEFAULT_FALLBACK_MONITOR};
    }

    /**
     * Return usable work area (excluding top bar/docks) for fixed anchoring.
     *
     * @param {number} [monitorIndex=0]
     * @returns {{ x: number, y: number, width: number, height: number }}
     */
    getWorkArea(monitorIndex = 0) {
        const idx = this.resolveMonitorIndex(monitorIndex);
        if (idx >= 0 && typeof Main.layoutManager?.getWorkAreaForMonitor === 'function') {
            try {
                const area = Main.layoutManager.getWorkAreaForMonitor(idx);
                if (area && area.width > 0 && area.height > 0)
                    return {x: area.x, y: area.y, width: area.width, height: area.height};
            } catch (_e) {
                // Fall back to full monitor geometry during early shell startup.
            }
        }
        return this.getMonitorGeometry(monitorIndex);
    }

    /**
     * Clamp `(x, y)` so a widget of size `(w, h)` stays completely within monitor bounds.
     *
     * @param {number} x
     * @param {number} y
     * @param {number} w
     * @param {number} h
     * @param {number} [monitorIndex=0]
     * @returns {{ x: number, y: number }}
     */
    clampToMonitor(x, y, w, h, monitorIndex = 0) {
        const mon = this.getMonitorGeometry(monitorIndex);
        const safeW = Math.max(1, w || 1);
        const safeH = Math.max(1, h || 1);

        const minX = mon.x;
        const maxX = mon.x + Math.max(0, mon.width - safeW);
        const minY = mon.y;
        const maxY = mon.y + Math.max(0, mon.height - safeH);

        const clampedX = Math.round(Math.max(minX, Math.min(maxX, Number.isFinite(x) ? x : minX)));
        const clampedY = Math.round(Math.max(minY, Math.min(maxY, Number.isFinite(y) ? y : minY)));

        return {x: clampedX, y: clampedY};
    }

    /**
     * Compute widget `(x, y)` coordinates from `visualsConfig` and widget dimensions.
     *
     * @param {object} visualsConfig
     * @param {number} widgetWidth
     * @param {number} widgetHeight
     * @returns {{ x: number, y: number }}
     */
    computePosition(visualsConfig, widgetWidth, widgetHeight) {
        const cfg = visualsConfig ?? {};
        const monitorIndex = Number.isInteger(cfg.monitor_index) ? cfg.monitor_index : 0;
        const w = Math.max(1, widgetWidth || 120);
        const h = Math.max(1, widgetHeight || 38);

        const mode = typeof cfg.position_mode === 'string'
            ? cfg.position_mode.toLowerCase().trim()
            : 'fixed';

        if (mode === 'notch') {
            const mon = this.getMonitorGeometry(monitorIndex);
            const area = this.getWorkArea(monitorIndex);
            const x = mon.x + (mon.width - w) / 2;
            const y = area.y;
            return this.clampToMonitor(x, y, w, h, monitorIndex);
        }

        if (mode === 'floating') {
            const floatX = Number.isFinite(cfg.float_x) ? cfg.float_x : this._defaultCenterX(monitorIndex, w);
            const floatY = Number.isFinite(cfg.float_y) ? cfg.float_y : this.getWorkArea(monitorIndex).y + 12;
            return this.clampToMonitor(floatX, floatY, w, h, monitorIndex);
        }

        const area = this.getWorkArea(monitorIndex);
        const alignment = typeof cfg.fixed_alignment === 'string'
            ? cfg.fixed_alignment.toLowerCase().trim()
            : 'top-center';
        const offsetX = Number.isFinite(cfg.offset_x) ? cfg.offset_x : 0;
        const offsetY = Number.isFinite(cfg.offset_y) ? cfg.offset_y : 12;

        let x = area.x + (area.width - w) / 2;
        let y = area.y;

        const [vPart, hPart] = this._parseAlignment(alignment);

        if (hPart === 'left')
            x = area.x;
        else if (hPart === 'right')
            x = area.x + area.width - w;
        else
            x = area.x + (area.width - w) / 2;

        if (vPart === 'bottom')
            y = area.y + area.height - h;
        else if (vPart === 'center')
            y = area.y + (area.height - h) / 2;
        else
            y = area.y;

        return this.clampToMonitor(x + offsetX, y + offsetY, w, h, monitorIndex);
    }

    /**
     * Return true when the widget center sits in the lower half of the target monitor.
     *
     * @param {number} y
     * @param {number} widgetHeight
     * @param {number} [monitorIndex=0]
     * @returns {boolean}
     */
    isInLowerHalf(y, widgetHeight, monitorIndex = 0) {
        const mon = this.getMonitorGeometry(monitorIndex);
        const centerY = y + (widgetHeight || 0) / 2;
        return centerY > (mon.y + mon.height / 2);
    }

    _defaultCenterX(monitorIndex, width) {
        const mon = this.getMonitorGeometry(monitorIndex);
        return Math.round(mon.x + (mon.width - width) / 2);
    }

    _parseAlignment(alignment) {
        switch (alignment) {
        case 'top-left':
            return ['top', 'left'];
        case 'top-center':
            return ['top', 'center'];
        case 'top-right':
            return ['top', 'right'];
        case 'center-left':
            return ['center', 'left'];
        case 'center':
            return ['center', 'center'];
        case 'center-right':
            return ['center', 'right'];
        case 'bottom-left':
            return ['bottom', 'left'];
        case 'bottom-center':
            return ['bottom', 'center'];
        case 'bottom-right':
            return ['bottom', 'right'];
        default:
            return ['top', 'center'];
        }
    }
}
