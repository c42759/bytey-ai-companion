// SPDX-License-Identifier: GPL-3.0-or-later

import Cairo from 'cairo';
import Clutter from 'gi://Clutter';
import GLib from 'gi://GLib';
import Pango from 'gi://Pango';
import St from 'gi://St';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';

import {parseHexColor} from './colorUtils.js';
import {EyeRenderer} from './eyeRenderer.js';
import {paintIslandBorder, paintNotchBackground} from './islandBorderPainter.js';
import {connectTracked} from './sharedUtils.js';

const DRAG_THRESHOLD_PX = 5;
const EXPANDED_STATES = new Set(['LISTENING', 'THINKING', 'TOOL_CALLING', 'SPEAKING']);
const DEFAULT_BG_RGBA = [10 / 255, 10 / 255, 12 / 255, 1.0];
const DEFAULT_BORDER_RGBA = [42 / 255, 44 / 255, 52 / 255, 1.0];

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
        this._godMode = false;
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

        this._bgColor = parseHexColor('#0A0A0C', DEFAULT_BG_RGBA);
        this._borderColor = parseHexColor('#2A2C34', DEFAULT_BORDER_RGBA);
        this._rainbowPhase = 0;
        this._rainbowTimerId = 0;
        this._rainbowLastUs = 0;
        this._hovered = false;

        this._pressActive = false;
        this._dragging = false;
        this._pressStart = [0, 0];
        this._actorStart = [0, 0];
        this._dragGrab = null;
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
    _getIslandWidth() {
        const raw = Number(this._visualsConfig.island_width ?? 140);
        if (!Number.isFinite(raw))
            return 140;
        return Math.max(140, Math.min(450, Math.round(raw)));
    }

    getBounds() {
        const fixedW = this._getIslandWidth();
        if (!this._island)
            return {x: 0, y: 0, width: fixedW, height: 38};
        const [, natH] = this._island.get_preferred_height(-1);
        const h = Math.max(this._island.height || 0, natH || 38);
        return {
            x: this._island.x,
            y: this._island.y,
            width: fixedW,
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
        if (!this._island)
            return;
        this._island.remove_all_transitions();
        this._island.opacity = 255;
        this._island.show();
    }

    /**
     * Hide the island overlay.
     */
    hide() {
        if (!this._island)
            return;
        this._island.remove_all_transitions();
        this._island.hide();
        this._island.opacity = 255;
    }

    /**
     * Fade the island overlay in smoothly.
     *
     * @param {number} [durationMs=200]
     */
    fadeIn(durationMs = 200) {
        if (!this._island)
            return;
        const effectiveDuration = (St.ReducedMotion &&
            St.Settings.get().reducedMotion === St.ReducedMotion.REDUCE)
            ? 0
            : durationMs;
        this._island.remove_all_transitions();
        if (!this._island.visible) {
            this._island.opacity = 0;
            this._island.show();
        }
        this._island.ease({
            opacity: 255,
            duration: effectiveDuration,
            mode: Clutter.AnimationMode.EASE_OUT_QUAD,
        });
    }

    /**
     * Fade the island overlay out smoothly and hide it on completion.
     *
     * @param {number} [durationMs=200]
     */
    fadeOut(durationMs = 200) {
        if (!this._island)
            return;
        const effectiveDuration = (St.ReducedMotion &&
            St.Settings.get().reducedMotion === St.ReducedMotion.REDUCE)
            ? 0
            : durationMs;
        this._island.remove_all_transitions();
        if (!this._island.visible)
            return;
        this._island.ease({
            opacity: 0,
            duration: effectiveDuration,
            mode: Clutter.AnimationMode.EASE_OUT_QUAD,
            onComplete: () => {
                if (this._island && this._island.opacity === 0)
                    this._island.hide();
            },
        });
    }

    /**
     * Update daemon state, emotion, and optional status pill text.
     *
     * @param {string} state
     * @param {string} [emotion]
     * @param {string} [label]
     */
    setState(state, emotion = undefined, label = undefined) {
        if (!this._island)
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
        if (!this._island)
            return;
        this._eyeRenderer.setAudioLevel(source, rms);
    }

    /**
     * Apply updated visuals config (animation mode, cursor tracking, colors, position).
     *
     * @param {object} visualsConfig
     */
    applyVisualsConfig(visualsConfig) {
        if (!this._island || !visualsConfig)
            return;

        this._visualsConfig = {
            ...this._visualsConfig,
            ...visualsConfig,
        };

        this._bgColor = parseHexColor(this._visualsConfig.background_color, DEFAULT_BG_RGBA);
        this._borderColor = parseHexColor(this._visualsConfig.border_color, DEFAULT_BORDER_RGBA);

        this._eyeRenderer.setAnimationConfig(
            this._visualsConfig.animation_mode ?? 'full',
            this._visualsConfig.cursor_tracking ?? true
        );
        this._eyeRenderer.setEyeColor(this._visualsConfig.eye_color ?? '#F7FAFF');

        this._syncNotchMode();
        this._syncCustomColors();
        this._island.width = this._getIslandWidth();
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
        if (!this._island || this._dragging)
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
        this._stopRainbowTimer();
        this._releaseDragGrab();

        for (const [obj, id] of this._signalIds)
            obj.disconnect(id);
        this._signalIds = [];

        if (this._eyeRenderer) {
            this._eyeRenderer.destroy();
            this._eyeRenderer = null;
        }

        if (this._island) {
            this._island.remove_all_transitions();
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
        return connectTracked(this._signalIds, obj, signal, handler);
    }

    _isNotchMode() {
        return String(this._visualsConfig.position_mode ?? 'fixed').toLowerCase().trim() === 'notch';
    }

    _syncNotchMode() {
        if (!this._island)
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
        if (!this._island)
            return;
        if (this._isNotchMode())
            this._island.set_style('background-color: transparent;');
        else
            this._island.set_style(`background-color: ${_rgbaCss(this._bgColor)};`);
    }

    setGodMode(enabled) {
        const next = Boolean(enabled);
        if (this._godMode === next)
            return;
        this._godMode = next;
        this._eyeRenderer?.setGodMode(next);
        this._syncRainbowTimer();
        this._borderCanvas?.queue_repaint();
    }

    _shouldRunRainbow() {
        if (!this._island || (this._visualsConfig.animation_mode ?? 'full') !== 'full')
            return false;
        return this._state === 'THINKING' ||
            this._state === 'TOOL_CALLING' ||
            (this._godMode && this._state !== 'ERROR');
    }

    _syncRainbowTimer() {
        const shouldRun = this._shouldRunRainbow();

        if (!shouldRun) {
            this._stopRainbowTimer();
            return;
        }

        if (this._rainbowTimerId)
            return;

        this._rainbowLastUs = GLib.get_monotonic_time();
        this._rainbowTimerId = GLib.timeout_add(GLib.PRIORITY_DEFAULT, 16, () => {
            if (!this._shouldRunRainbow()) {
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
            paintNotchBackground(cr, width, height, this._bgColor);
        } finally {
            cr.$dispose();
        }
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
            paintIslandBorder(cr, width, height, {
                isNotch: this._isNotchMode(),
                state: this._state,
                mode: this._visualsConfig.animation_mode ?? 'full',
                godMode: this._godMode,
                rainbowPhase: this._rainbowPhase,
                borderColor: this._borderColor,
                dragging: this._dragging,
                hovered: this._hovered,
            });
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

        this._island.width = this._getIslandWidth();
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
            this._dragGrab = global.stage.grab(this._island);
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
            this._dragGrab.dismiss();
            this._dragGrab = null;
        }
    }
}
