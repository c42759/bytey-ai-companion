// SPDX-License-Identifier: GPL-3.0-or-later

import Cairo from 'cairo';
import Clutter from 'gi://Clutter';
import GLib from 'gi://GLib';
import Pango from 'gi://Pango';
import St from 'gi://St';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';

import {EyeRenderer} from './eyeRenderer.js';

const DRAG_THRESHOLD_PX = 5;
const EXPANDED_STATES = new Set(['LISTENING', 'THINKING', 'TOOL_CALLING', 'SPEAKING']);
const DEFAULT_BG_RGBA = [10 / 255, 10 / 255, 12 / 255, 1.0];
const DEFAULT_BORDER_RGBA = [42 / 255, 44 / 255, 52 / 255, 1.0];
const NOTCH_TOP_FLARE_PX = 12;
const NOTCH_BOTTOM_RADIUS_PX = 16;

/**
 * Parse `#RGB`, `#RRGGBB`, or `#RRGGBBAA` into normalized `[r, g, b, a]` in `0..1`.
 *
 * @param {string} hex
 * @param {number[]} fallback
 * @returns {number[]}
 */
function _parseHexColor(hex, fallback) {
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

/**
 * Format `[r, g, b, a]` (`0..1`) as CSS `rgba(R, G, B, A)`.
 *
 * @param {number[]} rgba
 * @returns {string}
 */
function _rgbaCss(rgba) {
    const r = Math.round(Math.max(0, Math.min(1, rgba[0] ?? 0)) * 255);
    const g = Math.round(Math.max(0, Math.min(1, rgba[1] ?? 0)) * 255);
    const b = Math.round(Math.max(0, Math.min(1, rgba[2] ?? 0)) * 255);
    const a = Math.max(0, Math.min(1, rgba[3] ?? 1));
    return `rgba(${r}, ${g}, ${b}, ${Number(a.toFixed(3))})`;
}

/**
 * Convert HSL (`h, s, l` in `0..1`) to `[r, g, b]` (`0..1`).
 *
 * @param {number} h
 * @param {number} s
 * @param {number} l
 * @returns {number[]}
 */
function _hslToRgb(h, s, l) {
    const hn = ((h % 1) + 1) % 1;
    if (s === 0)
        return [l, l, l];
    const q = l < 0.5 ? l * (1 + s) : l + s - l * s;
    const p = 2 * l - q;
    const hue2rgb = t => {
        let tn = t;
        if (tn < 0)
            tn += 1;
        if (tn > 1)
            tn -= 1;
        if (tn < 1 / 6)
            return p + (q - p) * 6 * tn;
        if (tn < 1 / 2)
            return q;
        if (tn < 2 / 3)
            return p + (q - p) * (2 / 3 - tn) * 6;
        return p;
    };
    return [hue2rgb(hn + 1 / 3), hue2rgb(hn), hue2rgb(hn - 1 / 3)];
}

/**
 * Sample `[px, py]` at arc-length `s` along a closed rounded-pill perimeter.
 *
 * @param {number} s
 * @param {number} x0
 * @param {number} y0
 * @param {number} w
 * @param {number} h
 * @param {number} r
 * @returns {number[]}
 */
function _capsulePoint(s, x0, y0, w, h, r) {
    const rc = Math.min(Math.max(0, r), w / 2, h / 2);
    const horiz = Math.max(0, w - 2 * rc);
    const vert = Math.max(0, h - 2 * rc);
    const arcLen = 0.5 * Math.PI * rc;
    const L = 2 * horiz + 2 * vert + 4 * arcLen;
    if (L <= 0)
        return [x0, y0];

    let d = ((s % L) + L) % L;

    // 1. Top straight
    if (d < horiz)
        return [x0 + rc + d, y0];
    d -= horiz;

    // 2. Top-right arc
    if (d < arcLen) {
        const angle = -Math.PI / 2 + (rc > 0 ? d / rc : 0);
        return [x0 + w - rc + rc * Math.cos(angle), y0 + rc + rc * Math.sin(angle)];
    }
    d -= arcLen;

    // 3. Right straight
    if (d < vert)
        return [x0 + w, y0 + rc + d];
    d -= vert;

    // 4. Bottom-right arc
    if (d < arcLen) {
        const angle = rc > 0 ? d / rc : 0;
        return [x0 + w - rc + rc * Math.cos(angle), y0 + h - rc + rc * Math.sin(angle)];
    }
    d -= arcLen;

    // 5. Bottom straight
    if (d < horiz)
        return [x0 + w - rc - d, y0 + h];
    d -= horiz;

    // 6. Bottom-left arc
    if (d < arcLen) {
        const angle = Math.PI / 2 + (rc > 0 ? d / rc : 0);
        return [x0 + rc + rc * Math.cos(angle), y0 + h - rc + rc * Math.sin(angle)];
    }
    d -= arcLen;

    // 7. Left straight
    if (d < vert)
        return [x0, y0 + h - rc - d];
    d -= vert;

    // 8. Top-left arc
    const angle = Math.PI + (rc > 0 ? d / rc : 0);
    return [x0 + rc + rc * Math.cos(angle), y0 + rc + rc * Math.sin(angle)];
}

/**
 * Compute MacBook-notch top flare and bottom corner radii for a given box size.
 *
 * @param {number} width
 * @param {number} height
 * @returns {{ flare: number, botR: number, bodyWidth: number }}
 */
function _computeNotchGeometry(width, height) {
    const w = Math.max(1, width);
    const h = Math.max(1, height);
    const flare = Math.max(4, Math.min(NOTCH_TOP_FLARE_PX, Math.floor(h * 0.38), Math.floor(w * 0.14)));
    const bodyWidth = Math.max(4, w - 2 * flare);
    const botR = Math.max(4, Math.min(NOTCH_BOTTOM_RADIUS_PX, Math.floor(h - flare), Math.floor(bodyWidth / 2)));
    return {flare, botR, bodyWidth};
}

/**
 * Compute arc-length and segment metrics for the inset open U-contour of a notch.
 *
 * @param {number} width
 * @param {number} height
 * @param {number} inset
 * @returns {object}
 */
function _notchBorderMetrics(width, height, inset) {
    const {flare, botR} = _computeNotchGeometry(width, height);
    const d = Math.max(0, inset);
    const fOut = flare + d;
    const rIn = Math.max(1, botR - d);
    const thetaTop = Math.asin(Math.min(1, flare / fOut));
    const topArcLen = fOut * thetaTop;
    const vert = Math.max(0, height - flare - botR);
    const botArcLen = 0.5 * Math.PI * rIn;
    const horiz = Math.max(0, width - 2 * flare - 2 * botR);
    const totalLength = 2 * topArcLen + 2 * vert + 2 * botArcLen + horiz;
    return {flare, botR, d, fOut, rIn, thetaTop, topArcLen, vert, botArcLen, horiz, totalLength};
}

/**
 * Sample `[px, py]` at arc-length `s` along the open 7-segment notch U-contour.
 *
 * @param {number} s
 * @param {number} width
 * @param {number} height
 * @param {number} inset
 * @returns {number[]}
 */
function _notchBorderPoint(s, width, height, inset) {
    const {
        flare,
        botR,
        d,
        fOut,
        rIn,
        thetaTop,
        topArcLen,
        vert,
        botArcLen,
        horiz,
        totalLength,
    } = _notchBorderMetrics(width, height, inset);
    if (totalLength <= 0)
        return [width / 2, height / 2];

    let u = Math.max(0, Math.min(totalLength, s));

    if (u <= topArcLen) {
        const angle = -thetaTop + (fOut > 0 ? u / fOut : 0);
        return [fOut * Math.cos(angle), flare + fOut * Math.sin(angle)];
    }
    u -= topArcLen;

    if (u <= vert)
        return [flare + d, flare + u];
    u -= vert;

    if (u <= botArcLen) {
        const angle = Math.PI - (rIn > 0 ? u / rIn : 0);
        return [flare + botR + rIn * Math.cos(angle), height - botR + rIn * Math.sin(angle)];
    }
    u -= botArcLen;

    if (u <= horiz)
        return [flare + botR + u, height - d];
    u -= horiz;

    if (u <= botArcLen) {
        const angle = Math.PI / 2 - (rIn > 0 ? u / rIn : 0);
        return [width - flare - botR + rIn * Math.cos(angle), height - botR + rIn * Math.sin(angle)];
    }
    u -= botArcLen;

    if (u <= vert)
        return [width - flare - d, height - botR - u];
    u -= vert;

    const angle = Math.PI + (fOut > 0 ? Math.min(topArcLen, u) / fOut : 0);
    return [width + fOut * Math.cos(angle), flare + fOut * Math.sin(angle)];
}

/**
 * Always-on-top Dynamic Island pill widget placed via `Main.layoutManager.addTopChrome`.
 */
export class IslandWidget {
    /**
     * @param {import('./positionManager.js').PositionManager} positionManager
     * @param {object} callbacks
     * @param {() => void} [callbacks.onLeftClick]
     * @param {() => void} [callbacks.onRightClick]
     * @param {(bounds: {x: number, y: number, width: number, height: number}) => void} [callbacks.onMoved]
     * @param {(x: number, y: number) => void} [callbacks.onDragCommit]
     */
    constructor(positionManager, callbacks = {}) {
        this._positionManager = positionManager;
        this._callbacks = callbacks;

        this._state = 'IDLE';
        this._emotion = 'neutral';
        this._labelText = '';
        this._visualsConfig = {
            animation_mode: 'full',
            cursor_tracking: true,
            position_mode: 'fixed',
            monitor_index: 0,
            fixed_alignment: 'top-center',
            offset_x: 0,
            offset_y: 12,
            float_x: 500,
            float_y: 40,
            eye_color: '#F7FAFF',
            background_color: '#0A0A0C',
            border_color: '#2A2C34',
        };

        this._bgColor = _parseHexColor('#0A0A0C', DEFAULT_BG_RGBA);
        this._borderColor = _parseHexColor('#2A2C34', DEFAULT_BORDER_RGBA);
        this._rainbowPhase = 0;
        this._rainbowTimerId = 0;
        this._rainbowLastUs = 0;
        this._hovered = false;

        this._pressActive = false;
        this._dragging = false;
        this._pressStart = [0, 0];
        this._actorStart = [0, 0];
        this._dragGrab = null;
        this._destroyed = false;
        this._signalIds = [];

        this._island = new St.Widget({
            style_class: 'bytey-island',
            layout_manager: new Clutter.BinLayout(),
            reactive: true,
            track_hover: true,
            can_focus: false,
            x_align: Clutter.ActorAlign.CENTER,
            y_align: Clutter.ActorAlign.CENTER,
        });

        this._contentBox = new St.BoxLayout({
            style_class: 'bytey-island-content',
            x_expand: true,
            y_expand: true,
            x_align: Clutter.ActorAlign.FILL,
            y_align: Clutter.ActorAlign.CENTER,
        });

        this._eyeRenderer = new EyeRenderer({width: 96, height: 30});
        this._contentBox.add_child(this._eyeRenderer.actor);

        this._statusLabel = new St.Label({
            style_class: 'bytey-island-label',
            text: '',
            visible: false,
            y_align: Clutter.ActorAlign.CENTER,
        });
        this._statusLabel.clutter_text.ellipsize = Pango.EllipsizeMode.END;
        this._contentBox.add_child(this._statusLabel);

        this._bgCanvas = new St.DrawingArea({
            reactive: false,
            visible: false,
            x_expand: true,
            y_expand: true,
            x_align: Clutter.ActorAlign.FILL,
            y_align: Clutter.ActorAlign.FILL,
        });

        this._borderCanvas = new St.DrawingArea({
            reactive: false,
            x_expand: true,
            y_expand: true,
            x_align: Clutter.ActorAlign.FILL,
            y_align: Clutter.ActorAlign.FILL,
        });

        this._island.add_child(this._bgCanvas);
        this._island.add_child(this._contentBox);
        this._island.add_child(this._borderCanvas);

        this._connect(this._bgCanvas, 'repaint', area => this._onBgRepaint(area));
        this._connect(this._borderCanvas, 'repaint', area => this._onBorderRepaint(area));
        this._connect(this._island, 'notify::hover', () => {
            this._hovered = Boolean(this._island.hover);
            this._borderCanvas?.queue_repaint();
        });
        this._connect(this._island, 'button-press-event', (_a, event) => this._onButtonPress(event));
        this._connect(this._island, 'motion-event', (_a, event) => this._onMotion(event));
        this._connect(this._island, 'button-release-event', (_a, event) => this._onButtonRelease(event));

        this._syncNotchMode();
        this._syncCustomColors();
        Main.layoutManager.addTopChrome(this._island);
        this.reposition();
    }

    /**
     * @returns {St.Widget}
     */
    get actor() {
        return this._island;
    }

    /**
     * @returns {string}
     */
    get state() {
        return this._state;
    }

    /**
     * Return current `{ x, y, width, height }` stage bounds of the island.
     *
     * @returns {{ x: number, y: number, width: number, height: number }}
     */
    getBounds() {
        if (!this._island)
            return {x: 0, y: 0, width: 120, height: 38};
        const [, natW] = this._island.get_preferred_width(-1);
        const [, natH] = this._island.get_preferred_height(-1);
        const w = Math.max(this._island.width || 0, natW || 120);
        const h = Math.max(this._island.height || 0, natH || 38);
        return {
            x: this._island.x,
            y: this._island.y,
            width: w,
            height: h,
        };
    }

    /**
     * Return true if the island currently lies in the lower half of its monitor.
     *
     * @returns {boolean}
     */
    isInLowerHalf() {
        const bounds = this.getBounds();
        const monIdx = this._visualsConfig.monitor_index ?? 0;
        return this._positionManager.isInLowerHalf(bounds.y, bounds.height, monIdx);
    }

    /**
     * Show the island overlay.
     */
    show() {
        if (!this._destroyed && this._island)
            this._island.show();
    }

    /**
     * Hide the island overlay.
     */
    hide() {
        if (!this._destroyed && this._island)
            this._island.hide();
    }

    /**
     * Update daemon state, emotion, and optional status pill text.
     *
     * @param {string} state
     * @param {string} [emotion]
     * @param {string} [label]
     */
    setState(state, emotion = undefined, label = undefined) {
        if (this._destroyed || !this._island)
            return;

        const normState = typeof state === 'string' ? state.toUpperCase() : 'IDLE';
        this._state = normState;
        if (emotion !== undefined)
            this._emotion = emotion;
        if (label !== undefined)
            this._labelText = typeof label === 'string' ? label.trim() : '';
        else if (normState === 'IDLE')
            this._labelText = '';

        this._eyeRenderer.setState(normState, emotion);
        this._syncStateStyles();
        this._syncRainbowTimer();
        this._bgCanvas?.queue_repaint();
        this._borderCanvas?.queue_repaint();
        this.reposition();
    }

    /**
     * Update audio RMS level (`0.0..1.0`) for `mic` or `tts`.
     *
     * @param {'mic'|'tts'} source
     * @param {number} rms
     */
    setAudioLevel(source, rms) {
        if (this._destroyed)
            return;
        this._eyeRenderer.setAudioLevel(source, rms);
    }

    /**
     * Apply updated visuals config (animation mode, cursor tracking, colors, position).
     *
     * @param {object} visualsConfig
     */
    applyVisualsConfig(visualsConfig) {
        if (this._destroyed || !visualsConfig)
            return;

        this._visualsConfig = {
            ...this._visualsConfig,
            ...visualsConfig,
        };

        this._bgColor = _parseHexColor(this._visualsConfig.background_color, DEFAULT_BG_RGBA);
        this._borderColor = _parseHexColor(this._visualsConfig.border_color, DEFAULT_BORDER_RGBA);

        this._eyeRenderer.setAnimationConfig(
            this._visualsConfig.animation_mode ?? 'full',
            this._visualsConfig.cursor_tracking ?? true
        );
        this._eyeRenderer.setEyeColor(this._visualsConfig.eye_color ?? '#F7FAFF');

        this._syncNotchMode();
        this._syncCustomColors();
        this._syncRainbowTimer();
        this._bgCanvas?.queue_repaint();
        this._borderCanvas?.queue_repaint();

        if (!this._dragging)
            this.reposition();
    }

    /**
     * Recompute and apply island position from current visuals config and monitor layout.
     */
    reposition() {
        if (this._destroyed || !this._island || this._dragging)
            return;

        const bounds = this.getBounds();
        const pos = this._positionManager.computePosition(
            this._visualsConfig,
            bounds.width,
            bounds.height
        );
        this._island.set_position(pos.x, pos.y);
        this._callbacks.onMoved?.({...bounds, x: pos.x, y: pos.y});
    }

    /**
     * Release grabs, destroy renderer, and remove chrome actor.
     */
    destroy() {
        if (this._destroyed)
            return;
        this._destroyed = true;
        this._stopRainbowTimer();
        this._releaseDragGrab();

        for (const [obj, id] of this._signalIds) {
            try {
                obj.disconnect(id);
            } catch (_e) {
                // Ignore if already disposed.
            }
        }
        this._signalIds = [];

        if (this._eyeRenderer) {
            this._eyeRenderer.destroy();
            this._eyeRenderer = null;
        }

        if (this._island) {
            Main.layoutManager.removeChrome(this._island);
            this._island.destroy();
            this._island = null;
        }
        this._bgCanvas = null;
        this._borderCanvas = null;
        this._contentBox = null;
        this._statusLabel = null;
        this._positionManager = null;
        this._callbacks = null;
    }

    _connect(obj, signal, handler) {
        const id = obj.connect(signal, handler);
        this._signalIds.push([obj, id]);
        return id;
    }

    _isNotchMode() {
        return String(this._visualsConfig.position_mode ?? 'fixed').toLowerCase().trim() === 'notch';
    }

    _syncNotchMode() {
        if (this._destroyed || !this._island)
            return;

        const isNotch = this._isNotchMode();
        if (isNotch)
            this._island.add_style_class_name('bytey-island-notch');
        else
            this._island.remove_style_class_name('bytey-island-notch');

        if (this._bgCanvas)
            this._bgCanvas.visible = isNotch;

        if (isNotch && (this._dragging || this._pressActive)) {
            this._dragging = false;
            this._pressActive = false;
            this._releaseDragGrab();
            this._island.remove_style_class_name('bytey-island-dragging');
        }
    }

    _syncCustomColors() {
        if (this._destroyed || !this._island)
            return;
        if (this._isNotchMode())
            this._island.set_style('background-color: transparent;');
        else
            this._island.set_style(`background-color: ${_rgbaCss(this._bgColor)};`);
    }

    _syncRainbowTimer() {
        const shouldRun = !this._destroyed &&
            this._state === 'THINKING' &&
            (this._visualsConfig.animation_mode ?? 'full') === 'full';

        if (!shouldRun) {
            this._stopRainbowTimer();
            return;
        }

        if (this._rainbowTimerId)
            return;

        this._rainbowLastUs = GLib.get_monotonic_time();
        this._rainbowTimerId = GLib.timeout_add(GLib.PRIORITY_DEFAULT, 16, () => {
            if (this._destroyed ||
                this._state !== 'THINKING' ||
                (this._visualsConfig.animation_mode ?? 'full') !== 'full') {
                this._rainbowTimerId = 0;
                return GLib.SOURCE_REMOVE;
            }

            const nowUs = GLib.get_monotonic_time();
            const dt = Math.min(0.1, Math.max(0.001, (nowUs - this._rainbowLastUs) / 1_000_000));
            this._rainbowLastUs = nowUs;
            this._rainbowPhase = (this._rainbowPhase + dt * 0.65) % 1.0;
            this._borderCanvas?.queue_repaint();
            return GLib.SOURCE_CONTINUE;
        });
    }

    _stopRainbowTimer() {
        if (this._rainbowTimerId) {
            GLib.Source.remove(this._rainbowTimerId);
            this._rainbowTimerId = 0;
        }
    }

    _strokeRainbowCapsule(cr, width, height, inset, lineWidth, sat, light, alpha) {
        const w = width - 2 * inset;
        const h = height - 2 * inset;
        if (w <= 0 || h <= 0)
            return;

        const r = Math.max(2, Math.min(w, h) / 2);
        const rc = Math.min(r, w / 2, h / 2);
        const horiz = Math.max(0, w - 2 * rc);
        const vert = Math.max(0, h - 2 * rc);
        const L = 2 * horiz + 2 * vert + 2 * Math.PI * rc;
        if (L <= 0)
            return;

        const N = 96;
        cr.save();
        cr.setLineCap(Cairo.LineCap.ROUND);
        cr.setLineWidth(lineWidth);
        for (let i = 0; i < N; i++) {
            const s0 = (i / N) * L;
            const s1 = ((i + 1.18) / N) * L;
            const [x1, y1] = _capsulePoint(s0, inset, inset, w, h, rc);
            const [x2, y2] = _capsulePoint(s1, inset, inset, w, h, rc);
            const hue = ((i / N) - this._rainbowPhase + 1) % 1.0;
            const [crR, crG, crB] = _hslToRgb(hue, sat, light);
            cr.setSourceRGBA(crR, crG, crB, alpha);
            cr.newSubPath();
            cr.moveTo(x1, y1);
            cr.lineTo(x2, y2);
            cr.stroke();
        }
        cr.restore();
    }

    _onBgRepaint(area) {
        const [width, height] = area.get_surface_size();
        if (width <= 2 || height <= 2)
            return;

        const cr = area.get_context();
        try {
            cr.setOperator(Cairo.Operator.CLEAR);
            cr.paint();
            if (!this._isNotchMode())
                return;

            cr.setOperator(Cairo.Operator.OVER);
            const {flare, botR} = _computeNotchGeometry(width, height);
            const [r, g, b, a] = this._bgColor;

            cr.save();
            cr.newSubPath();
            cr.moveTo(0, 0);
            cr.lineTo(width, 0);
            cr.arcNegative(width, flare, flare, -Math.PI / 2, -Math.PI);
            cr.lineTo(width - flare, height - botR);
            cr.arc(width - flare - botR, height - botR, botR, 0, Math.PI / 2);
            cr.lineTo(flare + botR, height);
            cr.arc(flare + botR, height - botR, botR, Math.PI / 2, Math.PI);
            cr.lineTo(flare, flare);
            cr.arcNegative(0, flare, flare, 0, -Math.PI / 2);
            cr.closePath();
            cr.setSourceRGBA(r, g, b, a);
            cr.fill();
            cr.restore();
        } finally {
            cr.$dispose();
        }
    }

    _strokeStaticNotch(cr, width, height, border) {
        const {flare, botR, d, fOut, rIn, thetaTop, totalLength} = _notchBorderMetrics(width, height, 0.75);
        if (totalLength <= 0)
            return;

        cr.save();
        cr.setSourceRGBA(border[0], border[1], border[2], border[3]);
        cr.setLineWidth(1.5);
        cr.setLineCap(Cairo.LineCap.BUTT);
        cr.setLineJoin(Cairo.LineJoin.ROUND);
        cr.newSubPath();
        cr.arc(0, flare, fOut, -thetaTop, 0);
        cr.lineTo(flare + d, height - botR);
        cr.arcNegative(flare + botR, height - botR, rIn, Math.PI, Math.PI / 2);
        cr.lineTo(width - flare - botR, height - d);
        cr.arcNegative(width - flare - botR, height - botR, rIn, Math.PI / 2, 0);
        cr.lineTo(width - flare - d, flare);
        cr.arc(width, flare, fOut, Math.PI, Math.PI + thetaTop);
        cr.stroke();
        cr.restore();
    }

    _strokeRainbowNotch(cr, width, height, inset, lineWidth, sat, light, alpha) {
        const {totalLength} = _notchBorderMetrics(width, height, inset);
        if (totalLength <= 0)
            return;

        const N = 72;
        cr.save();
        cr.setLineCap(Cairo.LineCap.ROUND);
        cr.setLineWidth(lineWidth);
        for (let i = 0; i < N; i++) {
            const s0 = (i / N) * totalLength;
            const s1 = Math.min(totalLength, ((i + 1.18) / N) * totalLength);
            const [x1, y1] = _notchBorderPoint(s0, width, height, inset);
            const [x2, y2] = _notchBorderPoint(s1, width, height, inset);
            const hue = ((i / N) - this._rainbowPhase + 1) % 1.0;
            const [crR, crG, crB] = _hslToRgb(hue, sat, light);
            cr.setSourceRGBA(crR, crG, crB, alpha);
            cr.newSubPath();
            cr.moveTo(x1, y1);
            cr.lineTo(x2, y2);
            cr.stroke();
        }
        cr.restore();
    }

    _onBorderRepaint(area) {
        const [width, height] = area.get_surface_size();
        if (width <= 2 || height <= 2)
            return;

        const cr = area.get_context();
        try {
            cr.setOperator(Cairo.Operator.CLEAR);
            cr.paint();
            cr.setOperator(Cairo.Operator.OVER);

            const mode = this._visualsConfig.animation_mode ?? 'full';
            const isNotch = this._isNotchMode();
            if (this._state === 'THINKING' && mode !== 'disabled') {
                if (isNotch) {
                    this._strokeRainbowNotch(cr, width, height, 2.0, 4.0, 0.92, 0.60, 0.24);
                    this._strokeRainbowNotch(cr, width, height, 1.4, 2.2, 0.94, 0.64, 0.96);
                } else {
                    this._strokeRainbowCapsule(cr, width, height, 2.0, 4.0, 0.92, 0.60, 0.24);
                    this._strokeRainbowCapsule(cr, width, height, 1.4, 2.2, 0.94, 0.64, 0.96);
                }
                return;
            }

            let border;
            switch (this._state) {
            case 'LISTENING':
                border = [56 / 255, 189 / 255, 248 / 255, 0.78];
                break;
            case 'TOOL_CALLING':
                border = [251 / 255, 191 / 255, 36 / 255, 0.78];
                break;
            case 'SPEAKING':
                border = [96 / 255, 165 / 255, 250 / 255, 0.78];
                break;
            case 'ERROR':
                border = [1.0, 90 / 255, 95 / 255, 0.92];
                break;
            case 'MUTED':
                border = [
                    this._borderColor[0],
                    this._borderColor[1],
                    this._borderColor[2],
                    this._borderColor[3] * 0.55,
                ];
                break;
            case 'IDLE':
            default: {
                const [r, g, b, a] = this._borderColor;
                if (this._dragging) {
                    border = [
                        Math.min(1, r + 0.22),
                        Math.min(1, g + 0.22),
                        Math.min(1, b + 0.22),
                        Math.min(1, a + 0.24),
                    ];
                } else if (this._hovered) {
                    border = [r, g, b, Math.min(1, a + 0.14)];
                } else {
                    border = this._borderColor;
                }
                break;
            }
            }

            if (isNotch) {
                this._strokeStaticNotch(cr, width, height, border);
                return;
            }

            const inset = 1.0;
            const w = width - 2 * inset;
            const h = height - 2 * inset;
            if (w <= 2 || h <= 2)
                return;
            const r = Math.min(w / 2, h / 2);

            cr.save();
            cr.setSourceRGBA(border[0], border[1], border[2], border[3]);
            cr.setLineWidth(1.5);
            cr.newSubPath();
            cr.arc(inset + w - r, inset + r, r, -Math.PI / 2, 0);
            cr.arc(inset + w - r, inset + h - r, r, 0, Math.PI / 2);
            cr.arc(inset + r, inset + h - r, r, Math.PI / 2, Math.PI);
            cr.arc(inset + r, inset + r, r, Math.PI, (3 * Math.PI) / 2);
            cr.closePath();
            cr.stroke();
            cr.restore();
        } finally {
            cr.$dispose();
        }
    }

    _syncStateStyles() {
        const stateClasses = [
            'bytey-island-expanded',
            'bytey-island-listening',
            'bytey-island-thinking',
            'bytey-island-tool',
            'bytey-island-speaking',
            'bytey-island-muted',
            'bytey-island-error',
        ];
        for (const cls of stateClasses)
            this._island.remove_style_class_name(cls);

        const stateClassMap = {
            LISTENING: 'bytey-island-listening',
            THINKING: 'bytey-island-thinking',
            TOOL_CALLING: 'bytey-island-tool',
            SPEAKING: 'bytey-island-speaking',
            MUTED: 'bytey-island-muted',
            ERROR: 'bytey-island-error',
        };
        const cls = stateClassMap[this._state];
        if (cls)
            this._island.add_style_class_name(cls);

        const showLabel = this._state !== 'IDLE' && this._labelText.length > 0;
        this._statusLabel.text = showLabel ? this._labelText : '';
        this._statusLabel.visible = showLabel;

        if (EXPANDED_STATES.has(this._state) || showLabel)
            this._island.add_style_class_name('bytey-island-expanded');
    }

    _onButtonPress(event) {
        const button = event.get_button();
        if (button === Clutter.BUTTON_SECONDARY) {
            this._pressActive = false;
            this._releaseDragGrab();
            this._callbacks.onRightClick?.();
            return Clutter.EVENT_STOP;
        }

        if (button !== Clutter.BUTTON_PRIMARY)
            return Clutter.EVENT_PROPAGATE;

        const hadDragging = this._dragging;
        this._pressActive = true;
        this._dragging = false;
        if (hadDragging) {
            this._island.remove_style_class_name('bytey-island-dragging');
            this._borderCanvas?.queue_repaint();
        }
        this._pressStart = event.get_coords();
        this._actorStart = [this._island.x, this._island.y];

        if (this._visualsConfig.position_mode === 'floating') {
            this._releaseDragGrab();
            try {
                this._dragGrab = global.stage.grab(this._island);
            } catch (_e) {
                this._dragGrab = null;
            }
        }
        return Clutter.EVENT_STOP;
    }

    _onMotion(event) {
        if (!this._pressActive || this._visualsConfig.position_mode !== 'floating')
            return Clutter.EVENT_PROPAGATE;

        const [curX, curY] = event.get_coords();
        const dx = curX - this._pressStart[0];
        const dy = curY - this._pressStart[1];

        if (!this._dragging) {
            if (Math.hypot(dx, dy) < DRAG_THRESHOLD_PX)
                return Clutter.EVENT_PROPAGATE;
            this._dragging = true;
            this._island.add_style_class_name('bytey-island-dragging');
            this._borderCanvas?.queue_repaint();
        }

        const bounds = this.getBounds();
        const monIdx = this._visualsConfig.monitor_index ?? 0;
        const clamped = this._positionManager.clampToMonitor(
            this._actorStart[0] + dx,
            this._actorStart[1] + dy,
            bounds.width,
            bounds.height,
            monIdx
        );

        this._island.set_position(clamped.x, clamped.y);
        this._callbacks.onMoved?.({...bounds, x: clamped.x, y: clamped.y});
        return Clutter.EVENT_STOP;
    }

    _onButtonRelease(event) {
        if (!this._pressActive || event.get_button() !== Clutter.BUTTON_PRIMARY)
            return Clutter.EVENT_PROPAGATE;

        this._pressActive = false;
        this._releaseDragGrab();

        const [curX, curY] = event.get_coords();
        const dx = curX - this._pressStart[0];
        const dy = curY - this._pressStart[1];
        const travel = Math.hypot(dx, dy);

        const hadDragging = this._dragging;
        const wasDragging = this._dragging ||
            (this._visualsConfig.position_mode === 'floating' && travel >= DRAG_THRESHOLD_PX);
        this._dragging = false;
        this._island.remove_style_class_name('bytey-island-dragging');
        if (hadDragging)
            this._borderCanvas?.queue_repaint();

        if (wasDragging) {
            const bounds = this.getBounds();
            const monIdx = this._visualsConfig.monitor_index ?? 0;
            const clamped = this._positionManager.clampToMonitor(
                this._actorStart[0] + dx,
                this._actorStart[1] + dy,
                bounds.width,
                bounds.height,
                monIdx
            );
            this._island.set_position(clamped.x, clamped.y);
            this._visualsConfig.float_x = clamped.x;
            this._visualsConfig.float_y = clamped.y;
            this._callbacks.onMoved?.({...bounds, x: clamped.x, y: clamped.y});
            this._callbacks.onDragCommit?.(clamped.x, clamped.y);
        } else {
            this._callbacks.onLeftClick?.();
        }

        return Clutter.EVENT_STOP;
    }

    _releaseDragGrab() {
        if (this._dragGrab) {
            try {
                this._dragGrab.dismiss();
            } catch (_e) {
                // Ignore if stage grab was already dismissed.
            }
            this._dragGrab = null;
        }
    }
}
