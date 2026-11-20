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
    'annoyed',
    'love',
    'focus',
    'astonished',
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
        this._eyeColor = [0.97, 0.98, 1.0, 1.0];
        this._eyeColorHex = '#F7FAFF';

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
     * Update eye color from a hex string (`#RGB`, `#RRGGBB`, `#RRGGBBAA`).
     *
     * @param {string} hexColor
     */
    setEyeColor(hexColor) {
        if (this._destroyed)
            return;

        const nextHex = typeof hexColor === 'string' && hexColor.trim()
            ? hexColor.trim()
            : '#F7FAFF';
        const nextColor = this._parseHexColor(nextHex);
        const changed = nextHex !== this._eyeColorHex ||
            nextColor.some((v, idx) => Math.abs(v - this._eyeColor[idx]) > 1e-4);

        this._eyeColor = nextColor;
        this._eyeColorHex = nextHex;
        if (changed)
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

    _parseHexColor(hex, fallback = [0.97, 0.98, 1.0, 1.0]) {
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
        if (this._state !== 'MUTED' && this._state !== 'ERROR' &&
            (this._emotion === 'love' || this._emotion === 'focus' ||
             this._emotion === 'astonished' || this._emotion === 'annoyed' ||
             this._emotion === 'happy' || this._emotion === 'sleepy'))
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

        cr.setSourceRGBA(
            this._eyeColor[0],
            this._eyeColor[1],
            this._eyeColor[2],
            0.95 * this._eyeColor[3]
        );
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
        let alpha = 0.98 * this._eyeColor[3];
        let color = [this._eyeColor[0], this._eyeColor[1], this._eyeColor[2]];

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
            break;
        }
        case 'TOOL_CALLING': {
            radiusRatio = 0.24;
            leftScaleX = 1.08;
            rightScaleX = 1.08;
            leftScaleY = 0.72;
            rightScaleY = 0.72;
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
            break;
        }
        case 'MUTED': {
            alpha = 0.48 * this._eyeColor[3];
            leftScaleY = 0.36;
            rightScaleY = 0.36;
            offsetY = height * 0.06;
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
            const faceCy = height * 0.42;
            const mouthX = cx + offsetX * 0.7;
            const mouthY = height * 0.74 + offsetY * 0.5;
            const blushY = faceCy + height * 0.18 + offsetY * 0.7;
            const blushSpread = sep + baseW * 0.78;
            const speakMouthScale = state === 'SPEAKING'
                ? (isFull ? (1.0 + 0.45 * tts + 0.1 * Math.sin(this._phase * 12.0)) : 1.15)
                : 1.0;

            if (emotion === 'happy' && !this._blinking) {
                const bounceY = isFull ? Math.sin(this._phase * 6.0) * (height * 0.04) : 0;
                this._drawHappyArcs(
                    cr,
                    cx - sep + offsetX,
                    faceCy + offsetY + bounceY,
                    cx + sep + offsetX,
                    faceCy + offsetY + bounceY,
                    baseW * 0.7,
                    color,
                    alpha
                );
                this._drawBlushCheeks(cr, cx + offsetX, blushY + bounceY * 0.6, blushSpread, baseW * 0.52, alpha);
                this._drawMouth(cr, mouthX, mouthY + bounceY * 0.6, 'open_smile', baseW * 0.62 * speakMouthScale, color, alpha);
                if (isFull) {
                    const s1 = 0.4 + 0.6 * Math.abs(Math.sin(this._phase * 3.4));
                    const s2 = 0.4 + 0.6 * Math.abs(Math.cos(this._phase * 2.7));
                    this._drawSparkle(cr, cx - sep - baseW * 1.15, faceCy - height * 0.16, 2.6 * s1, color, alpha * s1);
                    this._drawSparkle(cr, cx + sep + baseW * 1.15, faceCy - height * 0.2, 2.9 * s2, color, alpha * s2);
                }
                return;
            }
            if (emotion === 'love' && !this._blinking) {
                const beat = isFull ? (1.0 + 0.12 * Math.max(0, Math.sin(this._phase * 5.2))) : 1.05;
                this._drawHeartEyes(
                    cr,
                    cx - sep + offsetX,
                    faceCy + offsetY,
                    cx + sep + offsetX,
                    faceCy + offsetY,
                    Math.min(baseW, baseH) * 0.54 * beat,
                    color,
                    alpha
                );
                this._drawBlushCheeks(cr, cx + offsetX, blushY, blushSpread, baseW * 0.52, alpha);
                this._drawMouth(cr, mouthX, mouthY, state === 'SPEAKING' ? 'open_smile' : 'smile', baseW * 0.56 * speakMouthScale, color, alpha);
                if (isFull) {
                    const t = (this._phase * 0.7) % 1.0;
                    const floatY = (faceCy - height * 0.12) - t * (height * 0.28);
                    const floatAlpha = alpha * Math.sin(t * Math.PI);
                    this._drawMiniHeart(cr, cx + sep + baseW * 1.05, floatY, 2.6, floatAlpha);
                }
                return;
            }
            if (emotion === 'astonished' && !this._blinking) {
                const tremorX = isFull ? Math.sin(this._phase * 22.0) * 1.0 : 0;
                const tremorY = isFull ? Math.cos(this._phase * 19.0) * 0.7 : 0;
                const pulse = isFull ? (1.0 + 0.05 * Math.sin(this._phase * 10.0)) : 1.0;
                this._drawAstonishedEyes(
                    cr,
                    cx - sep + offsetX + tremorX,
                    faceCy + offsetY + tremorY,
                    cx + sep + offsetX + tremorX,
                    faceCy + offsetY + tremorY,
                    baseW * 0.62 * pulse,
                    baseH * 0.68 * pulse,
                    color,
                    alpha
                );
                this._drawMouth(cr, mouthX + tremorX * 0.5, mouthY + tremorY * 0.5, 'o', baseW * 0.34 * speakMouthScale, color, alpha);
                return;
            }
            if (emotion === 'surprised' && !this._blinking) {
                this._drawSurprisedRings(
                    cr,
                    cx - sep + offsetX,
                    faceCy + offsetY,
                    cx + sep + offsetX,
                    faceCy + offsetY,
                    Math.min(baseW, baseH) * 0.62,
                    color,
                    alpha
                );
                this._drawMouth(cr, mouthX, mouthY, 'o', baseW * 0.3 * speakMouthScale, color, alpha);
                return;
            }
            if (emotion === 'focus') {
                const scanX = isFull ? Math.sin(this._phase * 3.8) * (maxTravelX * 0.22) : 0;
                this._drawHalfMoonEye(
                    cr,
                    cx - sep + offsetX + scanX,
                    faceCy + offsetY,
                    baseW * 1.05,
                    baseH * 0.56,
                    0,
                    color,
                    alpha
                );
                this._drawHalfMoonEye(
                    cr,
                    cx + sep + offsetX + scanX,
                    faceCy + offsetY,
                    baseW * 1.05,
                    baseH * 0.56,
                    0,
                    color,
                    alpha
                );
                this._drawMouth(cr, mouthX + scanX * 0.5, mouthY, state === 'SPEAKING' ? 'o' : 'flat', baseW * (state === 'SPEAKING' ? 0.26 * speakMouthScale : 0.48), color, alpha);
                return;
            }
            if (emotion === 'annoyed') {
                const glanceX = isFull ? Math.sin(this._phase * 1.8) * (maxTravelX * 0.35) : 0;
                const annoyedColor = [1.0, 0.72, 0.38];
                this._drawHalfMoonEye(
                    cr,
                    cx - sep + offsetX + glanceX,
                    faceCy + offsetY,
                    baseW * 1.06,
                    baseH * 0.5,
                    0.18,
                    annoyedColor,
                    alpha
                );
                this._drawHalfMoonEye(
                    cr,
                    cx + sep + offsetX + glanceX,
                    faceCy + offsetY,
                    baseW * 1.06,
                    baseH * 0.5,
                    -0.18,
                    annoyedColor,
                    alpha
                );
                this._drawMouth(cr, mouthX + glanceX * 0.4, mouthY, state === 'SPEAKING' ? 'o' : 'flat', baseW * (state === 'SPEAKING' ? 0.26 * speakMouthScale : 0.5), annoyedColor, alpha);
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
                const bob = isFull ? Math.sin(this._phase * 1.6) * (height * 0.03) : 0;
                const sleepyAlpha = Math.min(alpha, 0.78 * this._eyeColor[3]);
                this._drawSleepyV(
                    cr,
                    cx - sep + offsetX,
                    faceCy + offsetY + bob,
                    baseW * 0.85,
                    color,
                    sleepyAlpha
                );
                this._drawSleepyV(
                    cr,
                    cx + sep + offsetX,
                    faceCy + offsetY + bob,
                    baseW * 0.85,
                    color,
                    sleepyAlpha
                );
                this._drawMouth(cr, mouthX, mouthY + bob * 0.6, state === 'SPEAKING' ? 'o' : 'dot', baseW * 0.22 * speakMouthScale, color, sleepyAlpha);
                if (isFull) {
                    const t = (this._phase * 0.55) % 1.0;
                    const zX = cx + sep + baseW * 0.95 + t * 5.0;
                    const zY = (faceCy - height * 0.08) - t * (height * 0.26);
                    const zAlpha = sleepyAlpha * Math.sin(t * Math.PI);
                    this._drawFloatingZ(cr, zX, zY, 4.5 + t * 1.5, color, zAlpha);
                }
                return;
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
        const [r, g, b, a] = this._eyeColor;
        const grad = new Cairo.RadialGradient(x, y, radius * 0.2, x, y, radius);
        grad.addColorStopRGBA(0, r, g, b, 0.55 * strength * a);
        grad.addColorStopRGBA(0.6, r, g, b, 0.22 * strength * a);
        grad.addColorStopRGBA(1, r, g, b, 0.0);
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
        const [hr, hg, hb] = this._eyeColor;
        for (const [ex, ey] of [[lx, ly], [rx, ry]]) {
            const halo = new Cairo.RadialGradient(ex, ey, radius * 0.4, ex, ey, radius * 1.5);
            halo.addColorStopRGBA(0, hr, hg, hb, 0.32 * alpha);
            halo.addColorStopRGBA(1, hr, hg, hb, 0.0);
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

    _drawHeartEyes(cr, lx, ly, rx, ry, size, color, alpha) {
        const [r, g, b] = color;
        for (const [ex, ey] of [[lx, ly], [rx, ry]]) {
            const halo = new Cairo.RadialGradient(ex, ey, size * 0.25, ex, ey, size * 1.5);
            halo.addColorStopRGBA(0, 1.0, 0.36, 0.56, 0.32 * alpha);
            halo.addColorStopRGBA(1, 1.0, 0.36, 0.56, 0.0);
            cr.setSource(halo);
            cr.arc(ex, ey, size * 1.5, 0, 2 * Math.PI);
            cr.fill();

            cr.save();
            cr.translate(ex, ey - size * 0.12);
            cr.setSourceRGBA(r, g, b, alpha);
            cr.newSubPath();
            cr.moveTo(0, size * 0.72);
            cr.curveTo(-size * 1.15, size * 0.05, -size * 0.95, -size * 0.85, 0, -size * 0.25);
            cr.curveTo(size * 0.95, -size * 0.85, size * 1.15, size * 0.05, 0, size * 0.72);
            cr.closePath();
            cr.fill();
            cr.restore();
        }
    }

    _drawAstonishedEyes(cr, lx, ly, rx, ry, rxRadius, ryRadius, color, alpha) {
        const [hr, hg, hb] = this._eyeColor;
        const specs = [
            [lx, ly, rxRadius * 1.18, ryRadius * 1.18],
            [rx, ry, rxRadius * 0.96, ryRadius * 0.96],
        ];
        for (const [ex, ey, ew, eh] of specs) {
            const haloR = Math.max(ew, eh) * 1.5;
            const halo = new Cairo.RadialGradient(ex, ey, ew * 0.3, ex, ey, haloR);
            halo.addColorStopRGBA(0, hr, hg, hb, 0.35 * alpha);
            halo.addColorStopRGBA(1, hr, hg, hb, 0.0);
            cr.setSource(halo);
            cr.arc(ex, ey, haloR, 0, 2 * Math.PI);
            cr.fill();

            cr.setSourceRGBA(color[0], color[1], color[2], alpha);
            this._fillCapsule(cr, ex, ey, ew * 1.65, eh * 1.65, 0.5);
        }
    }

    _drawHalfMoonEye(cr, x, y, w, h, tiltRad, color, alpha) {
        cr.save();
        cr.translate(x, y);
        if (tiltRad !== 0)
            cr.rotate(tiltRad);
        cr.setSourceRGBA(color[0], color[1], color[2], alpha);
        const hw = w * 0.5;
        cr.newSubPath();
        cr.moveTo(-hw, -h * 0.3);
        cr.lineTo(hw, -h * 0.3);
        cr.curveTo(hw, h * 0.65, -hw, h * 0.65, -hw, -h * 0.3);
        cr.closePath();
        cr.fill();
        cr.restore();
    }

    _drawSleepyV(cr, x, y, w, color, alpha) {
        cr.save();
        cr.setSourceRGBA(color[0], color[1], color[2], alpha);
        cr.setLineCap(Cairo.LineCap.ROUND);
        cr.setLineWidth(Math.max(1.8, w * 0.22));
        cr.newSubPath();
        cr.arc(x, y - w * 0.2, w * 0.52, Math.PI * 0.18, Math.PI * 0.82);
        cr.stroke();
        cr.restore();
    }

    _drawBlushCheeks(cr, cx, y, spread, w, alpha) {
        cr.save();
        cr.setSourceRGBA(1.0, 0.36, 0.48, 0.82 * alpha);
        const h = Math.max(2.2, w * 0.48);
        this._fillCapsule(cr, cx - spread, y, w, h, 0.5);
        this._fillCapsule(cr, cx + spread, y, w, h, 0.5);
        cr.restore();
    }

    _drawMouth(cr, x, y, kind, size, color, alpha) {
        cr.save();
        cr.setSourceRGBA(color[0], color[1], color[2], alpha);
        cr.setLineCap(Cairo.LineCap.ROUND);
        if (kind === 'open_smile') {
            const hw = size * 0.5;
            cr.newSubPath();
            cr.moveTo(x - hw, y - size * 0.12);
            cr.lineTo(x + hw, y - size * 0.12);
            cr.curveTo(x + hw * 0.85, y + size * 0.52, x - hw * 0.85, y + size * 0.52, x - hw, y - size * 0.12);
            cr.closePath();
            cr.fill();
        } else if (kind === 'smile') {
            cr.setLineWidth(Math.max(1.5, size * 0.22));
            cr.newSubPath();
            cr.arc(x, y - size * 0.18, size * 0.46, Math.PI * 0.18, Math.PI * 0.82);
            cr.stroke();
        } else if (kind === 'flat') {
            cr.setLineWidth(Math.max(1.5, size * 0.24));
            cr.newSubPath();
            cr.moveTo(x - size * 0.45, y);
            cr.lineTo(x + size * 0.45, y);
            cr.stroke();
        } else if (kind === 'o') {
            cr.newSubPath();
            cr.arc(x, y, Math.max(1.5, size * 0.38), 0, 2 * Math.PI);
            cr.fill();
        } else if (kind === 'dot') {
            cr.newSubPath();
            cr.arc(x, y, Math.max(1.2, size * 0.32), 0, 2 * Math.PI);
            cr.fill();
        }
        cr.restore();
    }

    _drawMiniHeart(cr, x, y, size, alpha) {
        cr.save();
        cr.translate(x, y);
        cr.setSourceRGBA(0.65, 0.92, 1.0, alpha);
        cr.newSubPath();
        cr.moveTo(0, size * 0.72);
        cr.curveTo(-size * 1.15, size * 0.05, -size * 0.95, -size * 0.85, 0, -size * 0.25);
        cr.curveTo(size * 0.95, -size * 0.85, size * 1.15, size * 0.05, 0, size * 0.72);
        cr.closePath();
        cr.fill();
        cr.restore();
    }

    _drawSparkle(cr, x, y, size, color, alpha) {
        cr.save();
        cr.translate(x, y);
        cr.setSourceRGBA(color[0], color[1], color[2], alpha);
        const inner = size * 0.28;
        cr.newSubPath();
        cr.moveTo(0, -size);
        cr.lineTo(inner, -inner);
        cr.lineTo(size, 0);
        cr.lineTo(inner, inner);
        cr.lineTo(0, size);
        cr.lineTo(-inner, inner);
        cr.lineTo(-size, 0);
        cr.lineTo(-inner, -inner);
        cr.closePath();
        cr.fill();
        cr.restore();
    }

    _drawFloatingZ(cr, x, y, size, color, alpha) {
        cr.save();
        cr.setSourceRGBA(color[0], color[1], color[2], alpha);
        cr.setLineCap(Cairo.LineCap.ROUND);
        cr.setLineJoin(Cairo.LineJoin.ROUND);
        cr.setLineWidth(Math.max(1.2, size * 0.22));
        const hs = size * 0.5;
        cr.newSubPath();
        cr.moveTo(x - hs, y - hs);
        cr.lineTo(x + hs, y - hs);
        cr.lineTo(x - hs, y + hs);
        cr.lineTo(x + hs, y + hs);
        cr.stroke();
        cr.restore();
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
