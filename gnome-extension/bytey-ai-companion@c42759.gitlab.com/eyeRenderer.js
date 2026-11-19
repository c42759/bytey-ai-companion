// SPDX-License-Identifier: GPL-3.0-or-later

import Cairo from 'cairo';
import Clutter from 'gi://Clutter';
import GLib from 'gi://GLib';
import St from 'gi://St';

const VALID_STATES = new Set([
    'IDLE',
    'LISTENING',
    'THINKING',
    'TOOL_CALLING',
    'SPEAKING',
    'MUTED',
    'ERROR',
]);

const VALID_EMOTIONS = new Set([
    'neutral',
    'happy',
    'curious',
    'surprised',
    'sleepy',
    'error',
]);

const VALID_ANIM_MODES = new Set(['full', 'state_only', 'disabled']);

const FRAME_INTERVAL_MS = 16;
const POINTER_POLL_MS = 120;
const BLINK_DURATION_MS = 175;
const BLINK_MIN_MS = 3500;
const BLINK_MAX_MS = 6000;
const CURSOR_IDLE_WANDER_US = 10 * 1_000_000;
const WANDER_MIN_HOLD_US = 1_100_000;
const WANDER_MAX_HOLD_US = 2_800_000;
const CURSOR_MOVE_THRESHOLD_PX = 3;

/**
 * Procedural Cairo vector eye renderer attached to an `St.DrawingArea`.
 */
export class EyeRenderer {
    /**
     * @param {object} [options]
     * @param {number} [options.width=78] Logical width of the drawing area
     * @param {number} [options.height=30] Logical height of the drawing area
     */
    constructor(options = {}) {
        this._width = options.width ?? 96;
        this._height = options.height ?? 30;

        this._state = 'IDLE';
        this._emotion = 'neutral';
        this._animationMode = 'full';
        this._cursorTracking = true;

        this._micLevel = 0;
        this._ttsLevel = 0;
        this._smoothMicLevel = 0;
        this._smoothTtsLevel = 0;

        this._gazeX = 0;
        this._gazeY = 0;
        this._targetGazeX = 0;
        this._targetGazeY = 0;
        this._lastPtrX = null;
        this._lastPtrY = null;
        this._lastCursorMoveUs = GLib.get_monotonic_time();
        this._nextWanderPickUs = 0;
        this._wandering = false;

        this._blinking = false;
        this._blinkStartUs = 0;
        this._phase = 0;
        this._lastTickUs = GLib.get_monotonic_time();

        this._frameSourceId = 0;
        this._blinkTimerId = 0;
        this._pointerPollId = 0;
        this._destroyed = false;

        this._drawingArea = new St.DrawingArea({
            width: this._width,
            height: this._height,
            x_expand: true,
            y_expand: true,
            x_align: Clutter.ActorAlign.FILL,
            y_align: Clutter.ActorAlign.FILL,
        });

        this._repaintId = this._drawingArea.connect('repaint', area => {
            this._onRepaint(area);
        });
        this._destroyId = this._drawingArea.connect('destroy', () => {
            this._cleanupTimers();
            this._destroyed = true;
        });

        this._syncAnimationTimers();
    }

    /**
     * @returns {St.DrawingArea}
     */
    get actor() {
        return this._drawingArea;
    }

    /**
     * Update daemon state and emotion.
     *
     * @param {string} state
     * @param {string} [emotion]
     */
    setState(state, emotion = undefined) {
        if (this._destroyed)
            return;

        const normState = typeof state === 'string' ? state.toUpperCase() : 'IDLE';
        const nextState = VALID_STATES.has(normState) ? normState : 'IDLE';
        const nextEmotion = emotion !== undefined ? this._normalizeEmotion(emotion) : this._emotion;

        const changed = nextState !== this._state || nextEmotion !== this._emotion;
        this._state = nextState;
        this._emotion = nextEmotion;

        if (nextState !== 'LISTENING')
            this._micLevel = 0;
        if (nextState !== 'SPEAKING')
            this._ttsLevel = 0;
        if (nextState !== 'IDLE') {
            this._targetGazeX = 0;
            this._targetGazeY = 0;
        }

        if (changed) {
            this._syncAnimationTimers();
            this._queueRepaint();
        }
    }

    /**
     * Update only the active emotion tag.
     *
     * @param {string} emotion
     */
    setEmotion(emotion) {
        if (this._destroyed)
            return;
        const nextEmotion = this._normalizeEmotion(emotion);
        if (nextEmotion === this._emotion)
            return;
        this._emotion = nextEmotion;
        this._syncAnimationTimers();
        this._queueRepaint();
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

        const clamped = Number.isFinite(rms) ? Math.max(0, Math.min(1, rms)) : 0;
        if (source === 'mic')
            this._micLevel = clamped;
        else if (source === 'tts')
            this._ttsLevel = clamped;

        if (this._animationMode === 'full')
            this._ensureFrameLoopIfNeeded();
    }

    /**
     * Update visual animation settings (`animation_mode`, `cursor_tracking`).
     *
     * @param {string} mode `'full' | 'state_only' | 'disabled'`
     * @param {boolean} cursorTracking
     */
    setAnimationConfig(mode, cursorTracking = true) {
        if (this._destroyed)
            return;

        const normMode = typeof mode === 'string' ? mode.toLowerCase() : 'full';
        const nextMode = VALID_ANIM_MODES.has(normMode) ? normMode : 'full';
        const nextTrack = Boolean(cursorTracking);

        if (nextMode === this._animationMode && nextTrack === this._cursorTracking)
            return;

        this._animationMode = nextMode;
        this._cursorTracking = nextTrack;

        if (this._animationMode !== 'full' || !this._cursorTracking) {
            this._gazeX = 0;
            this._gazeY = 0;
            this._targetGazeX = 0;
            this._targetGazeY = 0;
            this._blinking = false;
        }

        this._syncAnimationTimers();
        this._queueRepaint();
    }

    /**
     * Stop all timers, disconnect signals, and dispose resources.
     */
    destroy() {
        if (this._destroyed)
            return;
        this._destroyed = true;
        this._cleanupTimers();

        if (this._drawingArea) {
            if (this._repaintId) {
                this._drawingArea.disconnect(this._repaintId);
                this._repaintId = 0;
            }
            if (this._destroyId) {
                this._drawingArea.disconnect(this._destroyId);
                this._destroyId = 0;
            }
            this._drawingArea.destroy();
            this._drawingArea = null;
        }
    }

    _normalizeEmotion(emotion) {
        const norm = typeof emotion === 'string' ? emotion.toLowerCase().trim() : 'neutral';
        return VALID_EMOTIONS.has(norm) ? norm : 'neutral';
    }

    _queueRepaint() {
        if (!this._destroyed && this._drawingArea)
            this._drawingArea.queue_repaint();
    }

    _cleanupTimers() {
        this._stopFrameLoop();
        this._stopBlinkTimer();
        this._stopPointerPoll();
    }

    _stopFrameLoop() {
        if (this._frameSourceId) {
            GLib.Source.remove(this._frameSourceId);
            this._frameSourceId = 0;
        }
    }

    _stopBlinkTimer() {
        if (this._blinkTimerId) {
            GLib.Source.remove(this._blinkTimerId);
            this._blinkTimerId = 0;
        }
    }

    _stopPointerPoll() {
        if (this._pointerPollId) {
            GLib.Source.remove(this._pointerPollId);
            this._pointerPollId = 0;
        }
    }

    _syncAnimationTimers() {
        if (this._destroyed)
            return;

        if (this._animationMode !== 'full') {
            this._cleanupTimers();
            return;
        }

        if (this._state === 'IDLE' || this._state === 'LISTENING' || this._state === 'SPEAKING') {
            if (!this._blinkTimerId)
                this._scheduleNextBlink();
        } else {
            this._stopBlinkTimer();
            this._blinking = false;
        }

        if (this._state === 'IDLE' && this._cursorTracking)
            this._startPointerPoll();
        else
            this._stopPointerPoll();

        this._ensureFrameLoopIfNeeded();
    }

    _scheduleNextBlink() {
        this._stopBlinkTimer();
        if (this._destroyed || this._animationMode !== 'full')
            return;

        const delayMs = Math.round(BLINK_MIN_MS + Math.random() * (BLINK_MAX_MS - BLINK_MIN_MS));
        this._blinkTimerId = GLib.timeout_add(GLib.PRIORITY_DEFAULT, delayMs, () => {
            this._blinkTimerId = 0;
            if (this._destroyed || this._animationMode !== 'full')
                return GLib.SOURCE_REMOVE;

            this._blinking = true;
            this._blinkStartUs = GLib.get_monotonic_time();
            this._ensureFrameLoopIfNeeded();
            return GLib.SOURCE_REMOVE;
        });
    }

    _startPointerPoll() {
        if (this._pointerPollId || this._destroyed)
            return;

        this._pointerPollId = GLib.timeout_add(GLib.PRIORITY_DEFAULT, POINTER_POLL_MS, () => {
            if (this._destroyed || this._animationMode !== 'full' || !this._cursorTracking || this._state !== 'IDLE') {
                this._pointerPollId = 0;
                return GLib.SOURCE_REMOVE;
            }

            this._sampleCursorTarget();
            if (this._needsGazeEase())
                this._ensureFrameLoopIfNeeded();

            return GLib.SOURCE_CONTINUE;
        });
    }

    _sampleCursorTarget() {
        if (!this._drawingArea || !this._drawingArea.mapped)
            return;
        try {
            const [ptrX, ptrY] = global.get_pointer();
            const nowUs = GLib.get_monotonic_time();
            const moved = this._lastPtrX === null ||
                this._lastPtrY === null ||
                Math.hypot(ptrX - this._lastPtrX, ptrY - this._lastPtrY) >= CURSOR_MOVE_THRESHOLD_PX;

            if (moved) {
                this._lastPtrX = ptrX;
                this._lastPtrY = ptrY;
                this._lastCursorMoveUs = nowUs;
                this._wandering = false;
            }

            if ((nowUs - this._lastCursorMoveUs) >= CURSOR_IDLE_WANDER_US) {
                if (!this._wandering || nowUs >= this._nextWanderPickUs) {
                    this._wandering = true;
                    this._pickWanderTarget(nowUs);
                }
                return;
            }

            const [ax, ay] = this._drawingArea.get_transformed_position();
            const [aw, ah] = this._drawingArea.get_transformed_size();
            const cx = ax + aw / 2;
            const cy = ay + ah / 2;
            const dx = ptrX - cx;
            const dy = ptrY - cy;
            const maxDistX = 320;
            const maxDistY = 240;
            this._targetGazeX = Math.max(-1, Math.min(1, dx / maxDistX));
            this._targetGazeY = Math.max(-1, Math.min(1, dy / maxDistY));
        } catch (_e) {
            this._targetGazeX = 0;
            this._targetGazeY = 0;
        }
    }

    _pickWanderTarget(nowUs) {
        let nextX = 0;
        let nextY = 0;
        if (Math.random() < 0.2) {
            nextX = (Math.random() * 2 - 1) * 0.22;
            nextY = (Math.random() * 2 - 1) * 0.18;
        } else {
            for (let attempt = 0; attempt < 5; attempt++) {
                nextX = (Math.random() * 2 - 1) * 0.95;
                nextY = (Math.random() * 2 - 1) * 0.85;
                if (Math.hypot(nextX - this._targetGazeX, nextY - this._targetGazeY) >= 0.4)
                    break;
            }
        }
        this._targetGazeX = nextX;
        this._targetGazeY = nextY;
        const holdUs = Math.round(
            WANDER_MIN_HOLD_US + Math.random() * (WANDER_MAX_HOLD_US - WANDER_MIN_HOLD_US)
        );
        this._nextWanderPickUs = nowUs + holdUs;
    }

    _needsGazeEase() {
        return Math.abs(this._targetGazeX - this._gazeX) > 0.015 ||
            Math.abs(this._targetGazeY - this._gazeY) > 0.015;
    }

    _needsContinuousLoop() {
        if (this._animationMode !== 'full')
            return false;
        if (this._blinking)
            return true;
        if (this._state === 'LISTENING' || this._state === 'THINKING' ||
            this._state === 'TOOL_CALLING' || this._state === 'SPEAKING')
            return true;
        if (this._state === 'IDLE' && this._cursorTracking && this._needsGazeEase())
            return true;
        return false;
    }

    _ensureFrameLoopIfNeeded() {
        if (this._destroyed || this._frameSourceId || !this._needsContinuousLoop())
            return;

        this._lastTickUs = GLib.get_monotonic_time();
        this._frameSourceId = GLib.timeout_add(GLib.PRIORITY_DEFAULT, FRAME_INTERVAL_MS, () => {
            if (this._destroyed || !this._needsContinuousLoop()) {
                this._frameSourceId = 0;
                return GLib.SOURCE_REMOVE;
            }

            const nowUs = GLib.get_monotonic_time();
            const dt = Math.min(0.1, Math.max(0.001, (nowUs - this._lastTickUs) / 1_000_000));
            this._lastTickUs = nowUs;
            this._phase += dt;

            // Smooth audio levels for fluid Cairo geometry transitions
            const audioAlpha = 1 - Math.exp(-dt * 18);
            this._smoothMicLevel += (this._micLevel - this._smoothMicLevel) * audioAlpha;
            this._smoothTtsLevel += (this._ttsLevel - this._smoothTtsLevel) * audioAlpha;

            // Ease gaze offset smoothly toward cursor target
            const gazeAlpha = 1 - Math.exp(-dt * 10);
            this._gazeX += (this._targetGazeX - this._gazeX) * gazeAlpha;
            this._gazeY += (this._targetGazeY - this._gazeY) * gazeAlpha;
            if (!this._needsGazeEase()) {
                this._gazeX = this._targetGazeX;
                this._gazeY = this._targetGazeY;
            }

            if (this._blinking) {
                const elapsedMs = (nowUs - this._blinkStartUs) / 1000;
                if (elapsedMs >= BLINK_DURATION_MS) {
                    this._blinking = false;
                    this._scheduleNextBlink();
                }
            }

            this._queueRepaint();

            if (!this._needsContinuousLoop()) {
                this._frameSourceId = 0;
                return GLib.SOURCE_REMOVE;
            }
            return GLib.SOURCE_CONTINUE;
        });
    }

    _getBlinkFactor() {
        if (!this._blinking || this._animationMode !== 'full')
            return 1.0;
        const elapsedMs = (GLib.get_monotonic_time() - this._blinkStartUs) / 1000;
        const progress = Math.max(0, Math.min(1, elapsedMs / BLINK_DURATION_MS));
        return Math.max(0.06, 1.0 - Math.sin(progress * Math.PI));
    }

    _onRepaint(area) {
        const [width, height] = area.get_surface_size();
        if (width <= 0 || height <= 0)
            return;

        const cr = area.get_context();
        try {
            cr.setOperator(Cairo.Operator.CLEAR);
            cr.paint();
            cr.setOperator(Cairo.Operator.OVER);

            if (this._animationMode === 'disabled') {
                this._drawStaticDefaultEyes(cr, width, height);
                return;
            }

            this._drawDynamicEyes(cr, width, height);
        } finally {
            cr.$dispose();
        }
    }

    _drawStaticDefaultEyes(cr, width, height) {
        const cx = width / 2;
        const cy = height / 2;
        const eyeW = Math.max(8, Math.min(width * 0.14, height * 0.46));
        const eyeH = Math.max(10, height * 0.58);
        const sep = Math.max(eyeW * 1.05, Math.min(width * 0.16, height * 0.68));

        cr.setSourceRGBA(0.95, 0.96, 0.98, 0.95);
        this._fillCapsule(cr, cx - sep, cy, eyeW, eyeH);
        this._fillCapsule(cr, cx + sep, cy, eyeW, eyeH);
    }

    _drawDynamicEyes(cr, width, height) {
        const isFull = this._animationMode === 'full';
        const state = this._state;
        const emotion = this._emotion;
        const blink = this._getBlinkFactor();

        const cx = width / 2;
        const cy = height / 2;
        const baseW = Math.max(8, Math.min(width * 0.14, height * 0.46));
        const baseH = Math.max(10, height * 0.58);
        const sep = Math.max(baseW * 1.05, Math.min(width * 0.16, height * 0.68));
        const edgePadX = Math.max(4, width * 0.04);
        const maxTravelX = Math.max(6, (width / 2) - sep - (baseW * 0.55) - edgePadX);
        const maxTravelY = Math.max(3, (height / 2) - (baseH * 0.52) - 2);

        let offsetX = 0;
        let offsetY = 0;
        let leftScaleX = 1.0;
        let leftScaleY = 1.0;
        let rightScaleX = 1.0;
        let rightScaleY = 1.0;
        let leftDy = 0;
        let rightDy = 0;
        let leftTilt = 0;
        let rightTilt = 0;
        let radiusRatio = 0.5;
        let alpha = 0.98;
        let color = [0.97, 0.98, 1.0];

        const mic = isFull ? this._smoothMicLevel : 0.45;
        const tts = isFull ? this._smoothTtsLevel : 0.5;

        // 1. State baseline geometry
        switch (state) {
        case 'LISTENING': {
            const auraStrength = isFull ? (0.25 + 0.65 * mic) : 0.45;
            const pulse = isFull ? (1.0 + 0.16 * mic + 0.04 * Math.sin(this._phase * 6.0)) : 1.1;
            leftScaleX = 1.06 * pulse;
            rightScaleX = 1.06 * pulse;
            leftScaleY = 1.12 * pulse * blink;
            rightScaleY = 1.12 * pulse * blink;
            color = [0.82, 0.96, 1.0];
            this._drawListeningAura(cr, cx - sep, cy, baseH * 0.95, auraStrength);
            this._drawListeningAura(cr, cx + sep, cy, baseH * 0.95, auraStrength);
            break;
        }
        case 'THINKING': {
            const orbX = isFull ? Math.sin(this._phase * 2.8) * (maxTravelX * 0.75) : maxTravelX * 0.35;
            const orbY = isFull ? (-maxTravelY * 0.55 + Math.cos(this._phase * 5.6) * (maxTravelY * 0.35)) : -maxTravelY * 0.45;
            offsetX = orbX;
            offsetY = orbY;
            leftScaleY = 0.92;
            rightScaleY = 0.92;
            color = [0.92, 0.91, 1.0];
            break;
        }
        case 'TOOL_CALLING': {
            radiusRatio = 0.24;
            leftScaleX = 1.08;
            rightScaleX = 1.08;
            leftScaleY = 0.72;
            rightScaleY = 0.72;
            color = [1.0, 0.94, 0.78];
            if (isFull)
                offsetX = Math.sin(this._phase * 4.2) * (maxTravelX * 0.45);
            break;
        }
        case 'SPEAKING': {
            const pulse = isFull
                ? (0.72 + 0.55 * tts + 0.08 * Math.sin(this._phase * 11.0))
                : 0.95;
            leftScaleY = Math.max(0.32, Math.min(1.22, pulse)) * blink;
            rightScaleY = Math.max(0.32, Math.min(1.22, pulse)) * blink;
            leftScaleX = 1.05;
            rightScaleX = 1.05;
            color = [0.94, 0.98, 1.0];
            break;
        }
        case 'MUTED': {
            alpha = 0.48;
            leftScaleY = 0.36;
            rightScaleY = 0.36;
            offsetY = height * 0.06;
            color = [0.85, 0.87, 0.92];
            break;
        }
        case 'ERROR': {
            color = [1.0, 0.353, 0.373];
            leftScaleX = 1.08;
            rightScaleX = 1.08;
            leftScaleY = 0.46;
            rightScaleY = 0.46;
            leftTilt = 0.24;
            rightTilt = -0.24;
            break;
        }
        case 'IDLE':
        default: {
            if (isFull && this._cursorTracking) {
                offsetX = this._gazeX * maxTravelX;
                offsetY = this._gazeY * maxTravelY;
            }
            leftScaleY = blink;
            rightScaleY = blink;
            break;
        }
        }

        // 2. Emotion modulation (when not muted/tool-locked, or explicit error emotion)
        if (emotion === 'error') {
            color = [1.0, 0.353, 0.373];
            leftScaleX = 1.08;
            rightScaleX = 1.08;
            leftScaleY = 0.46;
            rightScaleY = 0.46;
            leftTilt = 0.24;
            rightTilt = -0.24;
        } else if (state !== 'MUTED' && state !== 'ERROR') {
            if (emotion === 'happy' && !this._blinking) {
                this._drawHappyArcs(
                    cr,
                    cx - sep + offsetX,
                    cy + offsetY,
                    cx + sep + offsetX,
                    cy + offsetY,
                    baseW * 0.72,
                    color,
                    alpha
                );
                return;
            }
            if (emotion === 'surprised' && !this._blinking) {
                this._drawSurprisedRings(
                    cr,
                    cx - sep + offsetX,
                    cy + offsetY,
                    cx + sep + offsetX,
                    cy + offsetY,
                    Math.min(baseW, baseH) * 0.68,
                    color,
                    alpha
                );
                return;
            }
            if (emotion === 'curious') {
                leftScaleX *= 1.15;
                leftScaleY *= 1.12;
                leftDy -= height * 0.05;
                leftTilt -= 0.09;

                rightScaleX *= 0.86;
                rightScaleY *= 0.78;
                rightDy += height * 0.04;
                rightTilt += 0.07;
            } else if (emotion === 'sleepy') {
                alpha = Math.min(alpha, 0.72);
                leftScaleY *= 0.42;
                rightScaleY *= 0.42;
                leftDy += height * 0.07;
                rightDy += height * 0.07;
            }
        }

        // 3. Draw left & right eyes
        cr.setSourceRGBA(color[0], color[1], color[2], alpha);

        if (state === 'MUTED' || emotion === 'sleepy') {
            this._drawDroopingLidEye(
                cr,
                cx - sep + offsetX,
                cy + offsetY + leftDy,
                baseW * leftScaleX,
                baseH * leftScaleY
            );
            this._drawDroopingLidEye(
                cr,
                cx + sep + offsetX,
                cy + offsetY + rightDy,
                baseW * rightScaleX,
                baseH * rightScaleY
            );
            return;
        }

        this._fillTransformedCapsule(
            cr,
            cx - sep + offsetX,
            cy + offsetY + leftDy,
            baseW * leftScaleX,
            baseH * leftScaleY,
            leftTilt,
            radiusRatio
        );
        this._fillTransformedCapsule(
            cr,
            cx + sep + offsetX,
            cy + offsetY + rightDy,
            baseW * rightScaleX,
            baseH * rightScaleY,
            rightTilt,
            radiusRatio
        );
    }

    _drawListeningAura(cr, x, y, radius, strength) {
        const grad = new Cairo.RadialGradient(x, y, radius * 0.2, x, y, radius);
        grad.addColorStopRGBA(0, 0.22, 0.74, 0.97, 0.55 * strength);
        grad.addColorStopRGBA(0.6, 0.38, 0.85, 1.0, 0.22 * strength);
        grad.addColorStopRGBA(1, 0.22, 0.74, 0.97, 0.0);
        cr.setSource(grad);
        cr.arc(x, y, radius, 0, 2 * Math.PI);
        cr.fill();
    }

    _drawHappyArcs(cr, lx, ly, rx, ry, radius, color, alpha) {
        cr.save();
        cr.setSourceRGBA(color[0], color[1], color[2], alpha);
        cr.setLineCap(Cairo.LineCap.ROUND);
        cr.setLineWidth(Math.max(2.6, radius * 0.36));

        const cyOffset = radius * 0.28;
        cr.newSubPath();
        cr.arc(lx, ly + cyOffset, radius, Math.PI * 1.15, Math.PI * 1.85);
        cr.stroke();

        cr.newSubPath();
        cr.arc(rx, ry + cyOffset, radius, Math.PI * 1.15, Math.PI * 1.85);
        cr.stroke();
        cr.restore();
    }

    _drawSurprisedRings(cr, lx, ly, rx, ry, radius, color, alpha) {
        for (const [ex, ey] of [[lx, ly], [rx, ry]]) {
            const halo = new Cairo.RadialGradient(ex, ey, radius * 0.4, ex, ey, radius * 1.5);
            halo.addColorStopRGBA(0, 0.75, 0.92, 1.0, 0.32 * alpha);
            halo.addColorStopRGBA(1, 0.75, 0.92, 1.0, 0.0);
            cr.setSource(halo);
            cr.arc(ex, ey, radius * 1.5, 0, 2 * Math.PI);
            cr.fill();

            cr.setSourceRGBA(color[0], color[1], color[2], alpha);
            cr.setLineWidth(Math.max(2.2, radius * 0.32));
            cr.newSubPath();
            cr.arc(ex, ey, radius, 0, 2 * Math.PI);
            cr.stroke();
        }
    }

    _drawDroopingLidEye(cr, x, y, w, h) {
        const clampedH = Math.max(2.2, h);
        this._fillCapsule(cr, x, y, w, clampedH, 0.5);

        cr.save();
        cr.setLineCap(Cairo.LineCap.ROUND);
        cr.setLineWidth(Math.max(1.8, w * 0.16));
        cr.newSubPath();
        cr.moveTo(x - w * 0.55, y - clampedH * 0.25);
        cr.curveTo(
            x - w * 0.2, y + clampedH * 0.35,
            x + w * 0.2, y + clampedH * 0.35,
            x + w * 0.55, y - clampedH * 0.25
        );
        cr.stroke();
        cr.restore();
    }

    _fillTransformedCapsule(cr, x, y, w, h, tiltRad, radiusRatio = 0.5) {
        cr.save();
        cr.translate(x, y);
        if (tiltRad !== 0)
            cr.rotate(tiltRad);
        this._fillCapsule(cr, 0, 0, w, h, radiusRatio);
        cr.restore();
    }

    _fillCapsule(cr, cx, cy, w, h, radiusRatio = 0.5) {
        const safeW = Math.max(2, w);
        const safeH = Math.max(2, h);
        const r = Math.min(safeW / 2, safeH / 2, Math.min(safeW, safeH) * radiusRatio);
        const x0 = cx - safeW / 2;
        const y0 = cy - safeH / 2;

        cr.newSubPath();
        cr.arc(x0 + safeW - r, y0 + r, r, -Math.PI / 2, 0);
        cr.arc(x0 + safeW - r, y0 + safeH - r, r, 0, Math.PI / 2);
        cr.arc(x0 + r, y0 + safeH - r, r, Math.PI / 2, Math.PI);
        cr.arc(x0 + r, y0 + r, r, Math.PI, (3 * Math.PI) / 2);
        cr.closePath();
        cr.fill();
    }
}
