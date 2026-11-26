// SPDX-License-Identifier: GPL-3.0-or-later

import Clutter from 'gi://Clutter';
import GLib from 'gi://GLib';
import Pango from 'gi://Pango';
import St from 'gi://St';

export function parseMessageSegments(text) {
    const src = String(text ?? '');
    const segments = [];
    let pos = 0;

    while (pos < src.length) {
        const tickIdx = src.indexOf('```', pos);
        const quoteIdx = src.indexOf("'''", pos);
        let fenceIdx = -1;
        let marker = '';
        if (tickIdx !== -1 && (quoteIdx === -1 || tickIdx <= quoteIdx)) {
            fenceIdx = tickIdx;
            marker = '```';
        } else if (quoteIdx !== -1) {
            fenceIdx = quoteIdx;
            marker = "'''";
        }

        if (fenceIdx === -1) {
            const tail = src.slice(pos);
            if (tail)
                segments.push({type: 'text', text: tail});
            break;
        }

        if (fenceIdx > pos)
            segments.push({type: 'text', text: src.slice(pos, fenceIdx)});

        const afterFence = fenceIdx + 3;
        const nlIdx = src.indexOf('\n', afterFence);
        let lang = '';
        let codeStart = afterFence;
        if (nlIdx !== -1) {
            const header = src.slice(afterFence, nlIdx).trim();
            if (/^[a-zA-Z0-9_+#.-]{0,32}$/.test(header)) {
                lang = header;
                codeStart = nlIdx + 1;
            }
        }

        const closeIdx = src.indexOf(marker, codeStart);
        if (closeIdx === -1) {
            segments.push({type: 'code', lang, code: src.slice(codeStart).replace(/\n$/, '')});
            break;
        }

        segments.push({type: 'code', lang, code: src.slice(codeStart, closeIdx).replace(/\n$/, '')});
        pos = closeIdx + 3;
    }

    return segments;
}

export function formatInlineMarkup(text) {
    const src = String(text ?? '');
    if (!src)
        return '';

    const codeTokens = [];
    const withPlaceholders = src.replace(/`([^`\n]+)`/g, (_m, code) => {
        const idx = codeTokens.length;
        const escaped = GLib.markup_escape_text(code, -1);
        codeTokens.push(`<tt><span background="#1e293b" foreground="#e2e8f0"> ${escaped} </span></tt>`);
        return `\u0000CODE${idx}\u0000`;
    });

    const formatChunk = chunk => {
        if (!chunk)
            return '';
        const pattern = new RegExp([
            '\\*\\*\\*[^*\\n]+?\\*\\*\\*',
            '___[^_\\n]+?___',
            '\\*\\*[^*\\n]+?\\*\\*',
            '__[^_\\n]+?__',
            '~~[^~\\n]+?~~',
            '(?<![*\\w])\\*[^*\\n]+?\\*(?!\\*)',
            '(?<![_\\w])_[^_\\n]+?_(?!_)',
            '\\[[^\\]\\n]+\\]\\(https?:\\/\\/[^\\s)]+\\)',
            '\u0000CODE\\d+\\u0000',
        ].join('|'), 'g');
        let out = '';
        let lastIdx = 0;
        let match;
        while ((match = pattern.exec(chunk)) !== null) {
            if (match.index > lastIdx)
                out += GLib.markup_escape_text(chunk.slice(lastIdx, match.index), -1);

            const tok = match[0];
            if (tok.startsWith('\u0000CODE') && tok.endsWith('\u0000')) {
                const idx = Number.parseInt(tok.slice(5, -1), 10);
                out += codeTokens[idx] ?? '';
            } else if ((tok.startsWith('***') && tok.endsWith('***')) || (tok.startsWith('___') && tok.endsWith('___'))) {
                out += `<b><i>${formatChunk(tok.slice(3, -3))}</i></b>`;
            } else if ((tok.startsWith('**') && tok.endsWith('**')) || (tok.startsWith('__') && tok.endsWith('__'))) {
                out += `<b>${formatChunk(tok.slice(2, -2))}</b>`;
            } else if (tok.startsWith('~~') && tok.endsWith('~~')) {
                out += `<s>${formatChunk(tok.slice(2, -2))}</s>`;
            } else if ((tok.startsWith('*') && tok.endsWith('*')) || (tok.startsWith('_') && tok.endsWith('_'))) {
                out += `<i>${formatChunk(tok.slice(1, -1))}</i>`;
            } else if (tok.startsWith('[')) {
                const linkMatch = /^\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)$/.exec(tok);
                if (linkMatch) {
                    const label = formatChunk(linkMatch[1]);
                    out += `<span foreground="#38bdf8" underline="single">${label}</span>`;
                } else {
                    out += GLib.markup_escape_text(tok, -1);
                }
            } else {
                out += GLib.markup_escape_text(tok, -1);
            }
            lastIdx = pattern.lastIndex;
        }
        if (lastIdx < chunk.length)
            out += GLib.markup_escape_text(chunk.slice(lastIdx), -1);
        return out;
    };

    return formatChunk(withPlaceholders);
}

function parseTableRow(line) {
    const raw = String(line ?? '');
    if (!raw.includes('|'))
        return [];
    const cells = [];
    let current = '';
    for (let i = 0; i < raw.length; i++) {
        const ch = raw[i];
        if (ch === '\\' && raw[i + 1] === '|') {
            current += '|';
            i++;
        } else if (ch === '|') {
            cells.push(current);
            current = '';
        } else {
            current += ch;
        }
    }
    cells.push(current);
    if (cells.length > 0 && cells[0].trim() === '')
        cells.shift();
    if (cells.length > 0 && cells[cells.length - 1].trim() === '')
        cells.pop();
    return cells.map(c => c.trim());
}

function parseTableSeparator(line, expectedCols) {
    const cells = parseTableRow(line);
    if (cells.length === 0 || cells.length !== expectedCols)
        return null;
    const alignments = [];
    for (const cell of cells) {
        if (!/^:?-{1,}:?$/.test(cell))
            return null;
        if (cell.startsWith(':') && cell.endsWith(':'))
            alignments.push('center');
        else if (cell.endsWith(':'))
            alignments.push('right');
        else
            alignments.push('left');
    }
    return alignments;
}

export function parseMarkdownBlocks(text) {
    const lines = String(text ?? '').split('\n');
    const blocks = [];
    let paraLines = [];
    let quoteLines = [];

    const flushPara = () => {
        if (paraLines.length > 0) {
            blocks.push({type: 'paragraph', text: paraLines.join('\n')});
            paraLines = [];
        }
    };
    const flushQuote = () => {
        if (quoteLines.length > 0) {
            blocks.push({type: 'blockquote', text: quoteLines.join('\n')});
            quoteLines = [];
        }
    };

    for (let i = 0; i < lines.length; i++) {
        const line = lines[i];
        const quoteMatch = /^\s*>\s?(.*)$/.exec(line);
        if (quoteMatch) {
            flushPara();
            quoteLines.push(quoteMatch[1]);
            continue;
        }
        flushQuote();

        if (!line.trim()) {
            flushPara();
            continue;
        }

        if (/^\s{0,3}(?:---+|\*\*\*+|___+)\s*$/.test(line)) {
            flushPara();
            blocks.push({type: 'hr'});
            continue;
        }

        const headingMatch = /^(#{1,6})\s+(.+?)\s*#*$/.exec(line);
        if (headingMatch) {
            flushPara();
            blocks.push({
                type: 'heading',
                level: headingMatch[1].length,
                text: headingMatch[2],
            });
            continue;
        }

        if (i + 1 < lines.length && line.includes('|')) {
            const headers = parseTableRow(line);
            const alignments = headers.length > 0
                ? parseTableSeparator(lines[i + 1], headers.length)
                : null;
            if (alignments) {
                flushPara();
                const rows = [];
                let j = i + 2;
                while (j < lines.length) {
                    const rowLine = lines[j];
                    if (!rowLine.trim() || !rowLine.includes('|'))
                        break;
                    if (/^\s*>/.test(rowLine) || /^(#{1,6})\s+/.test(rowLine))
                        break;
                    const parsed = parseTableRow(rowLine);
                    const normalized = headers.map((_h, colIdx) => parsed[colIdx] ?? '');
                    rows.push(normalized);
                    j++;
                }
                blocks.push({type: 'table', headers, alignments, rows});
                i = j - 1;
                continue;
            }
        }

        const listMatch = /^(\s*)([*+-]|\d+\.)\s+(.+)$/.exec(line);
        if (listMatch) {
            flushPara();
            const rawMarker = listMatch[2];
            const indent = Math.min(4, Math.floor(listMatch[1].length / 2));
            blocks.push({
                type: 'list_item',
                indent,
                marker: /^\d+\.$/.test(rawMarker) ? rawMarker : '•',
                text: listMatch[3],
            });
            continue;
        }

        paraLines.push(line);
    }

    flushQuote();
    flushPara();
    return blocks;
}

function createFormattedLabel(text, styleClass = 'bytey-msg-text') {
    const label = new St.Label({
        style_class: styleClass,
        x_expand: true,
    });
    label.clutter_text.line_wrap = true;
    label.clutter_text.line_wrap_mode = Pango.WrapMode.WORD_CHAR;
    label.clutter_text.ellipsize = Pango.EllipsizeMode.NONE;
    label.clutter_text.set_markup(formatInlineMarkup(text));
    return label;
}

const TABLE_ALIGN_MAP = {
    left: {actor: Clutter.ActorAlign.START, pango: Pango.Alignment.LEFT},
    center: {actor: Clutter.ActorAlign.CENTER, pango: Pango.Alignment.CENTER},
    right: {actor: Clutter.ActorAlign.END, pango: Pango.Alignment.RIGHT},
};

function renderMarkdownTable(block) {
    const wrap = new St.BoxLayout({
        style_class: 'bytey-md-table-wrap',
        orientation: Clutter.Orientation.VERTICAL,
        x_expand: true,
    });

    const buildRow = (cells, isHeader, altRow) => {
        let rowClass = 'bytey-md-table-row';
        if (isHeader)
            rowClass += ' bytey-md-table-header';
        else if (altRow)
            rowClass += ' bytey-md-table-row-alt';

        const rowBox = new St.BoxLayout({
            style_class: rowClass,
            x_expand: true,
        });
        rowBox.layout_manager.homogeneous = true;

        for (let c = 0; c < block.headers.length; c++) {
            const alignCfg = TABLE_ALIGN_MAP[block.alignments[c]] ?? TABLE_ALIGN_MAP.left;
            const labelClass = isHeader
                ? 'bytey-msg-text bytey-md-table-header-text'
                : 'bytey-msg-text bytey-md-table-cell-text';
            const label = createFormattedLabel(cells[c] ?? '', labelClass);
            label.x_align = alignCfg.actor;
            label.clutter_text.line_alignment = alignCfg.pango;

            const cell = new St.Bin({
                style_class: 'bytey-md-table-cell',
                x_expand: true,
                x_align: Clutter.ActorAlign.FILL,
                y_align: Clutter.ActorAlign.FILL,
                child: label,
            });
            rowBox.add_child(cell);
        }
        return rowBox;
    };

    wrap.add_child(buildRow(block.headers, true, false));
    for (let r = 0; r < block.rows.length; r++)
        wrap.add_child(buildRow(block.rows[r], false, r % 2 === 1));

    return wrap;
}

export function renderMarkdownBlocks(container, text) {
    const blocks = parseMarkdownBlocks(text);
    for (const block of blocks) {
        if (block.type === 'heading') {
            container.add_child(
                createFormattedLabel(
                    block.text,
                    `bytey-msg-text bytey-md-heading bytey-md-h${block.level}`
                )
            );
        } else if (block.type === 'hr') {
            container.add_child(new St.Widget({
                style_class: 'bytey-md-hr',
                x_expand: true,
            }));
        } else if (block.type === 'table') {
            container.add_child(renderMarkdownTable(block));
        } else if (block.type === 'blockquote') {
            const quoteBox = new St.BoxLayout({
                style_class: 'bytey-md-blockquote',
                orientation: Clutter.Orientation.VERTICAL,
                x_expand: true,
            });
            renderMarkdownBlocks(quoteBox, block.text);
            container.add_child(quoteBox);
        } else if (block.type === 'list_item') {
            const row = new St.BoxLayout({
                style_class: block.indent > 0
                    ? `bytey-md-list-row bytey-md-list-indent-${block.indent}`
                    : 'bytey-md-list-row',
                x_expand: true,
            });
            const marker = new St.Label({
                style_class: 'bytey-md-list-marker',
                text: block.marker,
                y_align: Clutter.ActorAlign.START,
            });
            const itemLabel = createFormattedLabel(
                block.text,
                'bytey-msg-text bytey-md-list-text'
            );
            row.add_child(marker);
            row.add_child(itemLabel);
            container.add_child(row);
        } else {
            container.add_child(createFormattedLabel(block.text));
        }
    }
}

export function renderMessageBody(bodyBox, content, {replayIconSize, onCopyCode}) {
    if (!bodyBox)
        return;
    bodyBox.destroy_all_children();
    const segments = parseMessageSegments(content);

    for (const seg of segments) {
        if (seg.type === 'code') {
            const codeCard = new St.BoxLayout({
                style_class: 'bytey-code-block',
                orientation: Clutter.Orientation.VERTICAL,
                x_expand: true,
            });

            const header = new St.BoxLayout({
                style_class: 'bytey-code-header',
                x_expand: true,
            });
            const langLabel = new St.Label({
                style_class: 'bytey-code-lang',
                text: seg.lang || 'code',
                x_expand: true,
                y_align: Clutter.ActorAlign.CENTER,
            });
            const copyIcon = new St.Icon({
                icon_name: 'edit-copy-symbolic',
                icon_size: replayIconSize,
            });
            const copyBtn = new St.Button({
                style_class: 'bytey-code-copy-btn',
                can_focus: true,
                accessible_name: 'Copy code',
                y_align: Clutter.ActorAlign.CENTER,
                child: copyIcon,
            });
            copyBtn.connect('clicked', () => onCopyCode(seg.code, copyIcon));
            header.add_child(langLabel);
            header.add_child(copyBtn);
            codeCard.add_child(header);

            const codeLabel = new St.Label({
                style_class: 'bytey-code-text',
                text: seg.code,
                x_expand: true,
            });
            codeLabel.clutter_text.line_wrap = true;
            codeLabel.clutter_text.line_wrap_mode = Pango.WrapMode.WORD_CHAR;
            codeLabel.clutter_text.ellipsize = Pango.EllipsizeMode.NONE;
            codeCard.add_child(codeLabel);

            bodyBox.add_child(codeCard);
        } else {
            const trimmed = seg.text.replace(/^\n+|\n+$/g, '');
            if (!trimmed)
                continue;
            renderMarkdownBlocks(bodyBox, trimmed);
        }
    }

    bodyBox.visible = bodyBox.get_n_children() > 0;
}
