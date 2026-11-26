// SPDX-License-Identifier: GPL-3.0-or-later

import Cairo from 'cairo';

const NOTCH_TOP_FLARE_PX = 12;
const NOTCH_BOTTOM_RADIUS_PX = 16;

function hslToRgb(h, s, l) {
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

function capsulePoint(s, x0, y0, w, h, r) {
    const rc = Math.min(Math.max(0, r), w / 2, h / 2);
    const horiz = Math.max(0, w - 2 * rc);
    const vert = Math.max(0, h - 2 * rc);
    const arcLen = 0.5 * Math.PI * rc;
    const L = 2 * horiz + 2 * vert + 4 * arcLen;
    if (L <= 0)
        return [x0, y0];

    let d = ((s % L) + L) % L;

    if (d < horiz)
        return [x0 + rc + d, y0];
    d -= horiz;

    if (d < arcLen) {
        const angle = -Math.PI / 2 + (rc > 0 ? d / rc : 0);
        return [x0 + w - rc + rc * Math.cos(angle), y0 + rc + rc * Math.sin(angle)];
    }
    d -= arcLen;

    if (d < vert)
        return [x0 + w, y0 + rc + d];
    d -= vert;

    if (d < arcLen) {
        const angle = rc > 0 ? d / rc : 0;
        return [x0 + w - rc + rc * Math.cos(angle), y0 + h - rc + rc * Math.sin(angle)];
    }
    d -= arcLen;

    if (d < horiz)
        return [x0 + w - rc - d, y0 + h];
    d -= horiz;

    if (d < arcLen) {
        const angle = Math.PI / 2 + (rc > 0 ? d / rc : 0);
        return [x0 + rc + rc * Math.cos(angle), y0 + h - rc + rc * Math.sin(angle)];
    }
    d -= arcLen;

    if (d < vert)
        return [x0, y0 + h - rc - d];
    d -= vert;

    const angle = Math.PI + (rc > 0 ? d / rc : 0);
    return [x0 + rc + rc * Math.cos(angle), y0 + rc + rc * Math.sin(angle)];
}

function computeNotchGeometry(width, height) {
    const w = Math.max(1, width);
    const h = Math.max(1, height);
    const flare = Math.max(4, Math.min(NOTCH_TOP_FLARE_PX, Math.floor(h * 0.38), Math.floor(w * 0.14)));
    const bodyWidth = Math.max(4, w - 2 * flare);
    const botR = Math.max(4, Math.min(NOTCH_BOTTOM_RADIUS_PX, Math.floor(h - flare), Math.floor(bodyWidth / 2)));
    return {flare, botR, bodyWidth};
}

function notchBorderMetrics(width, height, inset) {
    const {flare, botR} = computeNotchGeometry(width, height);
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

function notchBorderPoint(s, width, height, inset) {
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
    } = notchBorderMetrics(width, height, inset);
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

function rainbowSegmentRgb(t, rainbowPhase, sat, light, palette = 'full') {
    const phasePos = (t - rainbowPhase + 1) % 1.0;
    if (palette === 'yellow') {
        const blend = 0.5 - 0.5 * Math.cos(2 * Math.PI * phasePos);
        return [
            (250 + (254 - 250) * blend) / 255,
            (204 + (240 - 204) * blend) / 255,
            (21 + (138 - 21) * blend) / 255,
        ];
    }
    if (palette === 'green') {
        const blend = 0.5 - 0.5 * Math.cos(2 * Math.PI * phasePos);
        return [
            (34 + (134 - 34) * blend) / 255,
            (197 + (239 - 197) * blend) / 255,
            (94 + (172 - 94) * blend) / 255,
        ];
    }
    return hslToRgb(phasePos, sat, light);
}

function strokeRainbowCapsule(cr, width, height, inset, lineWidth, sat, light, alpha, rainbowPhase, palette) {
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
        const [x1, y1] = capsulePoint(s0, inset, inset, w, h, rc);
        const [x2, y2] = capsulePoint(s1, inset, inset, w, h, rc);
        const [crR, crG, crB] = rainbowSegmentRgb(i / N, rainbowPhase, sat, light, palette);
        cr.setSourceRGBA(crR, crG, crB, alpha);
        cr.newSubPath();
        cr.moveTo(x1, y1);
        cr.lineTo(x2, y2);
        cr.stroke();
    }
    cr.restore();
}

function strokeStaticNotch(cr, width, height, border) {
    const {flare, botR, d, fOut, rIn, thetaTop, totalLength} = notchBorderMetrics(width, height, 0.75);
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

function strokeRainbowNotch(cr, width, height, inset, lineWidth, sat, light, alpha, rainbowPhase, palette) {
    const {totalLength} = notchBorderMetrics(width, height, inset);
    if (totalLength <= 0)
        return;

    const N = 72;
    cr.save();
    cr.setLineCap(Cairo.LineCap.ROUND);
    cr.setLineWidth(lineWidth);
    for (let i = 0; i < N; i++) {
        const s0 = (i / N) * totalLength;
        const s1 = Math.min(totalLength, ((i + 1.18) / N) * totalLength);
        const [x1, y1] = notchBorderPoint(s0, width, height, inset);
        const [x2, y2] = notchBorderPoint(s1, width, height, inset);
        const [crR, crG, crB] = rainbowSegmentRgb(i / N, rainbowPhase, sat, light, palette);
        cr.setSourceRGBA(crR, crG, crB, alpha);
        cr.newSubPath();
        cr.moveTo(x1, y1);
        cr.lineTo(x2, y2);
        cr.stroke();
    }
    cr.restore();
}

export function paintNotchBackground(cr, width, height, bgColor) {
    const {flare, botR} = computeNotchGeometry(width, height);
    const [r, g, b, a] = bgColor;

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
}

export function paintIslandBorder(cr, width, height, opts) {
    const {isNotch, state, mode, godMode, rainbowPhase, borderColor, dragging, hovered} = opts;

    let activityPalette = null;
    if (state === 'THINKING' && mode !== 'disabled')
        activityPalette = 'full';
    else if (state === 'TOOL_CALLING' && mode !== 'disabled')
        activityPalette = 'green';
    else if (godMode && state !== 'ERROR' && mode !== 'disabled')
        activityPalette = 'yellow';

    if (activityPalette) {
        const painter = isNotch ? strokeRainbowNotch : strokeRainbowCapsule;
        painter(cr, width, height, 2.0, 4.0, 0.92, 0.60, 0.24, rainbowPhase, activityPalette);
        painter(cr, width, height, 1.4, 2.2, 0.94, 0.64, 0.96, rainbowPhase, activityPalette);
        return;
    }

    let border;
    switch (state) {
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
        border = [borderColor[0], borderColor[1], borderColor[2], borderColor[3] * 0.55];
        break;
    case 'IDLE':
    default: {
        const [r, g, b, a] = borderColor;
        if (dragging) {
            border = [
                Math.min(1, r + 0.22),
                Math.min(1, g + 0.22),
                Math.min(1, b + 0.22),
                Math.min(1, a + 0.24),
            ];
        } else if (hovered) {
            border = [r, g, b, Math.min(1, a + 0.14)];
        } else {
            border = borderColor;
        }
        break;
    }
    }

    if (isNotch) {
        strokeStaticNotch(cr, width, height, border);
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
}
