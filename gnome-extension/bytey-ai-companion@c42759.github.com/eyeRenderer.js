// SPDX-License-Identifier: GPL-3.0-or-later

import Cairo from 'cairo';
import Clutter from 'gi://Clutter';
import GLib from 'gi://GLib';
import St from 'gi://St';

import {parseHexColor} from './colorUtils.js';

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
        this._godMode = false;
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
        if (!this._drawingArea)
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
        if (!this._drawingArea)
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
        if (!this._drawingArea)
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
        if (!this._drawingArea)
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
        if (!this._drawingArea)
            return;

        const nextHex = typeof hexColor === 'string' && hexColor.trim()
            ? hexColor.trim()
            : '#F7FAFF';
        const nextColor = parseHexColor(nextHex, [0.97, 0.98, 1.0, 1.0]);
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
        this._cleanupTimers();

        if (this._drawingArea) {
            if (this._repaintId) {
                this._drawingArea.disconnect(this._repaintId);
                this._repaintId = 0;
            }
            this._drawingArea.destroy();
            this._drawingArea = null;
        }
    }

    setGodMode(enabled) {
        const next = Boolean(enabled);
        if (this._godMode === next)
            return;
        this._godMode = next;
        this._queueRepaint();
    }

    _normalizeEmotion(emotion) {
        const norm = typeof emotion === 'string' ? emotion.toLowerCase().trim() : 'neutral';
        return VALID_EMOTIONS.has(norm) ? norm : 'neutral';
    }

    _queueRepaint() {
        if (this._drawingArea)
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
        if (!this._drawingArea)
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
        if (!this._drawingArea || this._animationMode !== 'full')
            return;

        const delayMs = Math.round(BLINK_MIN_MS + Math.random() * (BLINK_MAX_MS - BLINK_MIN_MS));
        this._blinkTimerId = GLib.timeout_add(GLib.PRIORITY_DEFAULT, delayMs, () => {
            this._blinkTimerId = 0;
            if (!this._drawingArea || this._animationMode !== 'full')
                return GLib.SOURCE_REMOVE;

            this._blinking = true;
            this._blinkStartUs = GLib.get_monotonic_time();
            this._ensureFrameLoopIfNeeded();
            return GLib.SOURCE_REMOVE;
        });
    }

    _startPointerPoll() {
        if (this._pointerPollId || !this._drawingArea)
            return;

        this._pointerPollId = GLib.timeout_add(GLib.PRIORITY_DEFAULT, POINTER_POLL_MS, () => {
            if (!this._drawingArea || this._animationMode !== 'full' || !this._cursorTracking || this._state !== 'IDLE') {
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
        if (!this._drawingArea || this._frameSourceId || !this._needsContinuousLoop())
            return;

        this._lastTickUs = GLib.get_monotonic_time();
        this._frameSourceId = GLib.timeout_add(GLib.PRIORITY_DEFAULT, FRAME_INTERVAL_MS, () => {
            if (!this._drawingArea || !this._needsContinuousLoop()) {
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
        const cy = height * 0.43;
        const baseW = Math.max(7, Math.min(width * 0.12, height * 0.42));
        const baseH = Math.max(9, height * 0.48);
        const sep = Math.max(baseW * 1.1, Math.min(width * 0.145, height * 0.56));
        const edgePadX = Math.max(4, width * 0.04);
        const maxTravelX = Math.max(4, (width / 2) - sep - (baseW * 0.6) - edgePadX);
        const maxTravelY = Math.max(2, (height * 0.4) - (baseH * 0.48));

        let offsetX = 0;
        let offsetY = 0;
        if (state === 'IDLE' && isFull && this._cursorTracking) {
            offsetX = this._gazeX * maxTravelX;
            offsetY = this._gazeY * maxTravelY;
        } else if ((state === 'THINKING' || state === 'TOOL_CALLING') && isFull) {
            offsetX = Math.sin(this._phase * 3.4) * (maxTravelX * 0.35);
        }

        const mic = isFull ? this._smoothMicLevel : 0.45;
        const tts = isFull ? this._smoothTtsLevel : 0.5;
        const speakScale = state === 'SPEAKING'
            ? (isFull ? (1.0 + 0.45 * tts + 0.12 * Math.sin(this._phase * 11.0)) : 1.15)
            : 1.0;

        const white = [0.98, 0.99, 1.0];
        const alpha = (state === 'MUTED' ? 0.52 : 0.98) * this._eyeColor[3];
        const mouthX = cx + offsetX * 0.6;
        const mouthY = height * 0.80 + offsetY * 0.35;
        const mouthW = Math.max(10, baseW * 1.35 * (state === 'SPEAKING' ? Math.min(1.35, speakScale) : 1.0));
        const mouthH = Math.max(2.4, height * 0.09);

        // 1. Muted state: neutral capsule eyes with black fill and white stroke
        if (state === 'MUTED') {
            this._drawOutlineCapsule(cr, cx - sep + offsetX, cy + offsetY, baseW * 0.95, baseH, [0, 0, 0], white, 1.6, 0.95 * this._eyeColor[3]);
            this._drawOutlineCapsule(cr, cx + sep + offsetX, cy + offsetY, baseW * 0.95, baseH, [0, 0, 0], white, 1.6, 0.95 * this._eyeColor[3]);
            return;
        }

        // 2. Error state or error emotion: X · X + coral-red mouth bar
        if (state === 'ERROR' || emotion === 'error') {
            const errRed = [0.97, 0.44, 0.44];
            const xSize = Math.min(baseW * 0.95, baseH * 0.72);
            this._drawEchoXEye(cr, cx - sep + offsetX, cy + offsetY, xSize, white, alpha);
            this._drawEchoXEye(cr, cx + sep + offsetX, cy + offsetY, xSize, white, alpha);
            this._drawEchoDot(cr, cx + offsetX, cy + offsetY, Math.max(1.8, baseW * 0.2), errRed, alpha);
            this._drawEchoMouthBar(cr, mouthX, mouthY, mouthW, mouthH, errRed, alpha);
            return;
        }

        // 3. Activity states: Processing (THINKING/TOOL_CALLING) or God Mode (determined white eyes)
        const isProcessing = state === 'THINKING' || state === 'TOOL_CALLING';
        const isGodActivity = this._godMode && (emotion === 'neutral' || isProcessing);
        if (isProcessing || isGodActivity) {
            const eyeW = baseW * 1.25;
            const eyeH = baseH * 0.76 * (state === 'IDLE' ? blink : 1.0);
            this._drawEchoDeterminedEye(cr, cx - sep + offsetX, cy + offsetY, eyeW, eyeH, 0.20, white, alpha);
            this._drawEchoDeterminedEye(cr, cx + sep + offsetX, cy + offsetY, eyeW, eyeH, -0.20, white, alpha);
            return;
        }

        // 4. Sleepy: lower semi-circle white eyes + indigo bar + 2 sleep dots
        if (emotion === 'sleepy') {
            const indigo = [0.39, 0.40, 0.95];
            const bob = isFull ? Math.sin(this._phase * 1.6) * (height * 0.025) : 0;
            const arcR = baseW * 0.68;
            this._drawLowerSemiCircleEye(cr, cx - sep + offsetX, cy + offsetY + bob - arcR * 0.15, arcR, white, alpha);
            this._drawLowerSemiCircleEye(cr, cx + sep + offsetX, cy + offsetY + bob - arcR * 0.15, arcR, white, alpha);
            this._drawEchoMouthBar(cr, mouthX, mouthY + bob * 0.5, mouthW, mouthH, indigo, alpha);
            const p = isFull ? (0.55 + 0.45 * Math.sin(this._phase * 2.2)) : 0.9;
            this._drawEchoDot(cr, cx + sep + baseW * 1.3, cy - baseH * 0.45, 1.5, indigo, alpha * p);
            this._drawEchoDot(cr, cx + sep + baseW * 1.75, cy - baseH * 0.65, 2.0, indigo, alpha * p);
            return;
        }

        // 5. Happy: upward white arc eyes + mint-green cheek dots + mint-green mouth bar
        if (emotion === 'happy' && !this._blinking) {
            const mint = [0.29, 0.87, 0.50];
            const bounce = isFull ? Math.sin(this._phase * 5.5) * (height * 0.03) : 0;
            this._drawHappyArcs(cr, cx - sep + offsetX, cy + offsetY + bounce, cx + sep + offsetX, cy + offsetY + bounce, baseW * 0.85, white, alpha);
            this._drawEchoDot(cr, cx - sep - baseW * 0.85 + offsetX, cy + baseH * 0.35 + offsetY + bounce, 1.8, mint, alpha);
            this._drawEchoDot(cr, cx + sep + baseW * 0.85 + offsetX, cy + baseH * 0.35 + offsetY + bounce, 1.8, mint, alpha);
            this._drawEchoMouthBar(cr, mouthX, mouthY + bounce * 0.5, mouthW, mouthH, mint, alpha);
            return;
        }

        // 6. Love: white heart eyes (keep hearts during blink) + pink cheek dashes + pink mouth bar
        if (emotion === 'love') {
            const pink = [0.96, 0.45, 0.71];
            const beat = isFull ? (1.0 + 0.1 * Math.max(0, Math.sin(this._phase * 5.2))) : 1.04;
            cr.save();
            cr.translate(0, cy + offsetY);
            cr.scale(1, blink);
            cr.translate(0, -(cy + offsetY));
            this._drawHeartEyes(cr, cx - sep + offsetX, cy + offsetY, cx + sep + offsetX, cy + offsetY, Math.min(baseW, baseH) * 0.64 * beat, white, alpha);
            cr.restore();
            this._drawEchoMouthBar(cr, cx - sep - baseW * 0.78 + offsetX, cy + baseH * 0.42 + offsetY, baseW * 0.55, mouthH * 0.85, pink, alpha);
            this._drawEchoMouthBar(cr, cx + sep + baseW * 0.78 + offsetX, cy + baseH * 0.42 + offsetY, baseW * 0.55, mouthH * 0.85, pink, alpha);
            this._drawEchoMouthBar(cr, mouthX, mouthY, mouthW, mouthH, pink, alpha);
            return;
        }

        // 7. Curious: tall left pill + smaller round right eye + purple bar + top-right sparkle
        if (emotion === 'curious') {
            const purple = [0.65, 0.55, 0.98];
            cr.setSourceRGBA(white[0], white[1], white[2], alpha);
            this._fillTransformedCapsule(cr, cx - sep + offsetX, cy - height * 0.03 + offsetY, baseW * 0.95, baseH * 1.12 * blink, 0, 0.5);
            this._fillTransformedCapsule(cr, cx + sep + offsetX, cy + height * 0.04 + offsetY, baseW * 0.92, baseW * 0.92 * blink, 0, 0.5);
            this._drawEchoMouthBar(cr, mouthX, mouthY, mouthW, mouthH, purple, alpha);
            const sp = isFull ? (0.65 + 0.35 * Math.sin(this._phase * 3.5)) : 1.0;
            this._drawSparkle(cr, cx + sep + baseW * 1.5, cy - baseH * 0.55, 3.2 * sp, purple, alpha * sp);
            return;
        }

        // 8. Surprised / Astonished: 2 large round white eyes + amber bar + 2 amber ticks
        if (emotion === 'surprised' || emotion === 'astonished') {
            const amber = [0.98, 0.75, 0.14];
            const d = Math.max(baseW * 1.05, baseH * 0.78);
            cr.setSourceRGBA(white[0], white[1], white[2], alpha);
            this._fillTransformedCapsule(cr, cx - sep + offsetX, cy + offsetY, d, d * blink, 0, 0.5);
            this._fillTransformedCapsule(cr, cx + sep + offsetX, cy + offsetY, d, d * blink, 0, 0.5);
            this._drawEchoMouthBar(cr, mouthX, mouthY, mouthW, mouthH, amber, alpha);
            this._drawEchoExclamationTicks(cr, cx + sep + baseW * 1.35, cy - baseH * 0.5, 4.0, amber, alpha);
            return;
        }

        // 9. Focus (angled) & Annoyed (horizontal, no rotation)
        if (emotion === 'focus' || emotion === 'annoyed') {
            const cyan = [0.13, 0.83, 0.93];
            const tiltL = emotion === 'annoyed' ? 0 : 0.20;
            const tiltR = emotion === 'annoyed' ? 0 : -0.20;
            this._drawEchoDeterminedEye(cr, cx - sep + offsetX, cy + offsetY, baseW * 1.25, baseH * 0.76 * blink, tiltL, white, alpha);
            this._drawEchoDeterminedEye(cr, cx + sep + offsetX, cy + offsetY, baseW * 1.25, baseH * 0.76 * blink, tiltR, white, alpha);
            this._drawEchoMouthBar(cr, mouthX, mouthY, mouthW, mouthH, cyan, alpha);
            return;
        }

        // 10. Neutral / Default (including LISTENING / SPEAKING): 2 tall white pill eyes + cyan-blue bar
        const blue = [0.38, 0.65, 0.98];
        if (state === 'LISTENING') {
            const auraStrength = isFull ? (0.25 + 0.65 * mic) : 0.45;
            this._drawListeningAura(cr, cx - sep + offsetX, cy + offsetY, baseH * 0.9, auraStrength);
            this._drawListeningAura(cr, cx + sep + offsetX, cy + offsetY, baseH * 0.9, auraStrength);
        }
        const pulseY = state === 'SPEAKING'
            ? (isFull ? Math.max(0.4, Math.min(1.18, 0.78 + 0.4 * tts + 0.08 * Math.sin(this._phase * 10.0))) : 0.95)
            : 1.0;
        cr.setSourceRGBA(white[0], white[1], white[2], alpha);
        this._fillTransformedCapsule(cr, cx - sep + offsetX, cy + offsetY, baseW * 0.95, baseH * blink * pulseY, 0, 0.5);
        this._fillTransformedCapsule(cr, cx + sep + offsetX, cy + offsetY, baseW * 0.95, baseH * blink * pulseY, 0, 0.5);
        this._drawEchoMouthBar(cr, mouthX, mouthY, mouthW, mouthH, blue, alpha);
    }

    _drawEchoMouthBar(cr, x, y, w, h, rgb, alpha) {
        cr.save();
        cr.setSourceRGBA(rgb[0], rgb[1], rgb[2], alpha);
        this._fillTransformedCapsule(cr, x, y, w, h, 0, 0.5);
        cr.restore();
    }

    _drawEchoDot(cr, x, y, r, rgb, alpha) {
        cr.save();
        cr.setSourceRGBA(rgb[0], rgb[1], rgb[2], alpha);
        cr.arc(x, y, r, 0, 2 * Math.PI);
        cr.fill();
        cr.restore();
    }

    _drawEchoXEye(cr, x, y, size, rgb, alpha) {
        const half = size * 0.46;
        cr.save();
        cr.setSourceRGBA(rgb[0], rgb[1], rgb[2], alpha);
        cr.setLineCap(Cairo.LineCap.ROUND);
        cr.setLineWidth(Math.max(2.6, size * 0.34));
        cr.moveTo(x - half, y - half);
        cr.lineTo(x + half, y + half);
        cr.moveTo(x + half, y - half);
        cr.lineTo(x - half, y + half);
        cr.stroke();
        cr.restore();
    }

    _drawEchoExclamationTicks(cr, x, y, size, rgb, alpha) {
        cr.save();
        cr.setSourceRGBA(rgb[0], rgb[1], rgb[2], alpha);
        cr.setLineCap(Cairo.LineCap.ROUND);
        cr.setLineWidth(1.8);
        cr.moveTo(x, y + size * 0.35);
        cr.lineTo(x + size * 0.35, y - size * 0.45);
        cr.moveTo(x + size * 0.65, y + size * 0.65);
        cr.lineTo(x + size * 1.15, y + size * 0.15);
        cr.stroke();
        cr.restore();
    }

    _drawEchoDeterminedEye(cr, x, y, w, h, tilt, rgb, alpha) {
        const r = Math.max(1.5, Math.min(w, h) * 0.45);
        cr.save();
        cr.translate(x, y);
        cr.rotate(tilt);
        cr.setSourceRGBA(rgb[0], rgb[1], rgb[2], alpha);
        cr.newSubPath();
        cr.moveTo(-w / 2, -h * 0.25);
        cr.lineTo(w / 2, -h * 0.25);
        cr.lineTo(w / 2, h / 2 - r);
        cr.arc(w / 2 - r, h / 2 - r, r, 0, Math.PI / 2);
        cr.lineTo(-w / 2 + r, h / 2);
        cr.arc(-w / 2 + r, h / 2 - r, r, Math.PI / 2, Math.PI);
        cr.closePath();
        cr.fill();
        cr.restore();
    }

    _drawOutlineCapsule(cr, x, y, w, h, fillRgb, strokeRgb, lineWidth, alpha) {
        const r = Math.min(w, h) * 0.5;
        const hw = w / 2;
        const hh = h / 2;
        cr.save();
        cr.translate(x, y);
        cr.newSubPath();
        cr.arc(-hw + r, -hh + r, r, Math.PI, 1.5 * Math.PI);
        cr.arc(hw - r, -hh + r, r, 1.5 * Math.PI, 0);
        cr.arc(hw - r, hh - r, r, 0, Math.PI / 2);
        cr.arc(-hw + r, hh - r, r, Math.PI / 2, Math.PI);
        cr.closePath();
        cr.setSourceRGBA(fillRgb[0], fillRgb[1], fillRgb[2], alpha);
        cr.fillPreserve();
        cr.setSourceRGBA(strokeRgb[0], strokeRgb[1], strokeRgb[2], alpha);
        cr.setLineWidth(lineWidth);
        cr.stroke();
        cr.restore();
    }

    _drawLowerSemiCircleEye(cr, x, y, radius, rgb, alpha) {
        cr.save();
        cr.setSourceRGBA(rgb[0], rgb[1], rgb[2], alpha);
        cr.setLineCap(Cairo.LineCap.ROUND);
        cr.setLineWidth(Math.max(2.6, radius * 0.42));
        cr.arc(x, y, radius, 0.12 * Math.PI, 0.88 * Math.PI);
        cr.stroke();
        cr.restore();
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
