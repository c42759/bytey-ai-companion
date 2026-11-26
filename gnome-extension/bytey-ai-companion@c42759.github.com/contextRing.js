// SPDX-License-Identifier: GPL-3.0-or-later

import Cairo from 'cairo';

function formatSpaceNumber(n) {
    const num = Math.max(0, Math.round(Number(n) || 0));
    return String(num).replace(/\B(?=(\d{3})+(?!\d))/g, ' ');
}

export function computeContextStats(latestTokenUsage, contextWindowTokens) {
    const usage = latestTokenUsage;
    let used = null;
    if (usage && typeof usage === 'object') {
        if (typeof usage.total_tokens === 'number' && Number.isFinite(usage.total_tokens) && usage.total_tokens >= 0) {
            used = usage.total_tokens;
        } else {
            const p = typeof usage.prompt_tokens === 'number' && Number.isFinite(usage.prompt_tokens) ? usage.prompt_tokens : null;
            const c = typeof usage.completion_tokens === 'number' && Number.isFinite(usage.completion_tokens) ? usage.completion_tokens : null;
            if (p !== null || c !== null)
                used = (p ?? 0) + (c ?? 0);
        }
    }
    const limit = (typeof contextWindowTokens === 'number' &&
        Number.isFinite(contextWindowTokens) &&
        contextWindowTokens > 0)
        ? contextWindowTokens
        : null;
    const pct = (used !== null && limit !== null)
        ? Math.max(0, Math.min(100, Math.round((used / limit) * 100)))
        : null;
    return {used, limit, pct};
}

export function formatContextTooltipText(latestTokenUsage, contextWindowTokens) {
    const {used, limit} = computeContextStats(latestTokenUsage, contextWindowTokens);
    if (used !== null && limit !== null)
        return `${formatSpaceNumber(used)}/${formatSpaceNumber(limit)}`;
    if (used !== null)
        return `${formatSpaceNumber(used)} / —`;
    if (limit !== null)
        return `0/${formatSpaceNumber(limit)}`;
    return 'Context usage unavailable';
}

export function drawContextRing(area, latestTokenUsage, contextWindowTokens) {
    const cr = area.get_context();
    const [w, h] = area.get_surface_size();
    const cx = w / 2;
    const cy = h / 2;
    const stroke = 3.2;
    const radius = Math.max(4, Math.min(w, h) / 2 - stroke / 2 - 1);

    cr.setLineCap(Cairo.LineCap.ROUND);
    cr.setLineWidth(stroke);

    // Background track
    cr.setSourceRGBA(1, 1, 1, 0.18);
    cr.arc(cx, cy, radius, 0, Math.PI * 2);
    cr.stroke();

    const {pct} = computeContextStats(latestTokenUsage, contextWindowTokens);
    let r = 0.72;
    let g = 0.75;
    let b = 0.80;
    if (pct !== null) {
        if (pct <= 65) {
            r = 0.133; g = 0.773; b = 0.369; // #22C55E green
        } else if (pct <= 89) {
            r = 0.918; g = 0.702; b = 0.031; // #EAB308 yellow
        } else {
            r = 0.937; g = 0.267; b = 0.267; // #EF4444 red
        }
        if (pct > 0) {
            const startAngle = -Math.PI / 2;
            const endAngle = startAngle + (pct / 100) * Math.PI * 2;
            cr.setSourceRGBA(r, g, b, 1.0);
            cr.arc(cx, cy, radius, startAngle, endAngle);
            cr.stroke();
        }
    }

    const label = pct !== null ? `${pct}%` : '--';
    cr.selectFontFace('Sans', Cairo.FontSlant.NORMAL, Cairo.FontWeight.BOLD);
    cr.setFontSize(pct === 100 ? 8.2 : 9.2);
    const ext = cr.textExtents(label);
    cr.setSourceRGBA(r, g, b, 1.0);
    cr.moveTo(cx - ext.width / 2 - ext.xBearing, cy - ext.height / 2 - ext.yBearing);
    cr.showText(label);

    cr.$dispose();
}
