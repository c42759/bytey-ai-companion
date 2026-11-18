import Clutter from 'gi://Clutter';
import Pango from 'gi://Pango';
import St from 'gi://St';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';

import {EyeRenderer} from './eyeRenderer.js';

const DRAG_THRESHOLD_PX = 5;
const EXPANDED_STATES = new Set(['LISTENING', 'THINKING', 'TOOL_CALLING', 'SPEAKING']);

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
        };

        this._pressActive = false;
        this._dragging = false;
        this._pressStart = [0, 0];
        this._actorStart = [0, 0];
        this._dragGrab = null;
        this._destroyed = false;
        this._signalIds = [];

        this._island = new St.BoxLayout({
            style_class: 'bytey-island',
            reactive: true,
            track_hover: true,
            can_focus: false,
            x_align: Clutter.ActorAlign.CENTER,
            y_align: Clutter.ActorAlign.CENTER,
        });

        this._eyeRenderer = new EyeRenderer({width: 96, height: 30});
        this._island.add_child(this._eyeRenderer.actor);

        this._statusLabel = new St.Label({
            style_class: 'bytey-island-label',
            text: '',
            visible: false,
            y_align: Clutter.ActorAlign.CENTER,
        });
        this._statusLabel.clutter_text.ellipsize = Pango.EllipsizeMode.END;
        this._island.add_child(this._statusLabel);

        this._connect(this._island, 'button-press-event', (_a, event) => this._onButtonPress(event));
        this._connect(this._island, 'motion-event', (_a, event) => this._onMotion(event));
        this._connect(this._island, 'button-release-event', (_a, event) => this._onButtonRelease(event));

        Main.layoutManager.addTopChrome(this._island);
        this.reposition();
    }

    /**
     * @returns {St.BoxLayout}
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
     * Apply updated visuals config (animation mode, cursor tracking, fixed/float position).
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

        this._eyeRenderer.setAnimationConfig(
            this._visualsConfig.animation_mode ?? 'full',
            this._visualsConfig.cursor_tracking ?? true
        );

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
    }

    _connect(obj, signal, handler) {
        const id = obj.connect(signal, handler);
        this._signalIds.push([obj, id]);
        return id;
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

        this._pressActive = true;
        this._dragging = false;
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

        const wasDragging = this._dragging ||
            (this._visualsConfig.position_mode === 'floating' && travel >= DRAG_THRESHOLD_PX);
        this._dragging = false;
        this._island.remove_style_class_name('bytey-island-dragging');

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
