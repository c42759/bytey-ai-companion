// SPDX-License-Identifier: GPL-3.0-or-later

import Clutter from 'gi://Clutter';
import Pango from 'gi://Pango';
import St from 'gi://St';

export function formatToolStatus(status) {
    switch (status) {
    case 'awaiting_confirm':
        return 'Confirm?';
    case 'ok':
        return 'OK';
    case 'error':
        return 'Error';
    case 'running':
    default:
        return 'Running';
    }
}

export function formatToolDetails(tool) {
    const parts = [];
    if (tool.argumentsText)
        parts.push(`Args: ${tool.argumentsText}`);
    if (tool.summary)
        parts.push(tool.summary);
    return parts.join('\n');
}

export function refreshToolWidget(record, onRefreshGroup) {
    const {
        tool,
        badge,
        statusLabel,
        detailsLabel,
        confirmCard,
        confirmPreviewLabel,
        confirmRow,
        allowBtn,
        denyBtn,
    } = record;
    for (const cls of ['running', 'ok', 'error', 'awaiting_confirm'])
        badge.remove_style_class_name(`bytey-tool-${cls}`);
    badge.add_style_class_name(`bytey-tool-${tool.status}`);

    statusLabel.text = formatToolStatus(tool.status);
    const detailText = formatToolDetails(tool);
    detailsLabel.text = detailText;

    const awaiting = tool.status === 'awaiting_confirm';
    const previewText = tool.diff || tool.command || '';
    if (previewText && awaiting) {
        confirmPreviewLabel.style_class = tool.diff ? 'bytey-confirm-diff' : 'bytey-confirm-cmd';
        confirmPreviewLabel.text = tool.command && !tool.diff ? `$ ${tool.command}` : previewText;
        confirmCard.visible = true;
    } else {
        confirmCard.visible = false;
    }

    if (awaiting) {
        record.expanded = true;
        allowBtn.reactive = true;
        denyBtn.reactive = true;
    }
    detailsLabel.visible = record.expanded && detailText.length > 0;
    confirmRow.visible = awaiting;
    if (record.group)
        onRefreshGroup(record.group);
}

export function createToolWidget(toolBox, tool, group, {toolWidgets, onConfirmTool, onRefreshGroup}) {
    toolBox.visible = true;

    const badge = new St.BoxLayout({
        style_class: `bytey-tool-badge bytey-tool-${tool.status}`,
        orientation: Clutter.Orientation.VERTICAL,
        x_expand: true,
    });

    const headerBtn = new St.Button({
        style_class: 'bytey-tool-header-btn',
        x_expand: true,
        can_focus: true,
    });

    const headerRow = new St.BoxLayout({
        style_class: 'bytey-tool-header',
        x_expand: true,
    });
    const titleLabel = new St.Label({
        style_class: 'bytey-tool-title',
        text: `${tool.server} · ${tool.tool}`,
        x_expand: true,
        x_align: Clutter.ActorAlign.START,
        y_align: Clutter.ActorAlign.CENTER,
    });
    const statusLabel = new St.Label({
        style_class: 'bytey-tool-status-chip',
        text: formatToolStatus(tool.status),
        y_align: Clutter.ActorAlign.CENTER,
    });
    headerRow.add_child(titleLabel);
    headerRow.add_child(statusLabel);
    headerBtn.set_child(headerRow);
    badge.add_child(headerBtn);

    const detailsLabel = new St.Label({
        style_class: 'bytey-tool-details',
        text: formatToolDetails(tool),
        visible: tool.status === 'awaiting_confirm',
        x_expand: true,
    });
    detailsLabel.clutter_text.line_wrap = true;
    detailsLabel.clutter_text.line_wrap_mode = Pango.WrapMode.WORD_CHAR;
    detailsLabel.clutter_text.ellipsize = Pango.EllipsizeMode.NONE;
    badge.add_child(detailsLabel);

    const confirmCard = new St.BoxLayout({
        style_class: 'bytey-confirm-card',
        orientation: Clutter.Orientation.VERTICAL,
        visible: false,
        x_expand: true,
    });
    const confirmScroll = new St.ScrollView({
        style_class: 'bytey-confirm-scroll',
        hscrollbar_policy: St.PolicyType.AUTOMATIC,
        vscrollbar_policy: St.PolicyType.AUTOMATIC,
        overlay_scrollbars: true,
        x_expand: true,
    });
    const confirmPreviewBox = new St.BoxLayout({
        orientation: Clutter.Orientation.VERTICAL,
        x_expand: true,
    });
    const confirmPreviewLabel = new St.Label({
        style_class: 'bytey-confirm-diff',
        text: '',
        x_expand: true,
    });
    confirmPreviewLabel.clutter_text.line_wrap = false;
    confirmPreviewLabel.clutter_text.ellipsize = Pango.EllipsizeMode.NONE;
    confirmPreviewBox.add_child(confirmPreviewLabel);
    confirmScroll.set_child(confirmPreviewBox);
    confirmCard.add_child(confirmScroll);
    badge.add_child(confirmCard);

    const confirmRow = new St.BoxLayout({
        style_class: 'bytey-confirm-row',
        visible: tool.status === 'awaiting_confirm',
        x_expand: true,
    });
    const allowBtn = new St.Button({
        style_class: 'bytey-btn-allow bytey-confirm-approve-btn',
        label: 'Approve',
        can_focus: true,
    });
    const denyBtn = new St.Button({
        style_class: 'bytey-btn-deny bytey-confirm-reject-btn',
        label: 'Reject',
        can_focus: true,
    });
    confirmRow.add_child(allowBtn);
    confirmRow.add_child(denyBtn);
    badge.add_child(confirmRow);

    const record = {
        tool,
        group,
        badge,
        titleLabel,
        statusLabel,
        detailsLabel,
        confirmCard,
        confirmPreviewLabel,
        confirmRow,
        allowBtn,
        denyBtn,
        expanded: tool.status === 'awaiting_confirm',
    };

    headerBtn.connect('clicked', () => {
        record.expanded = !record.expanded;
        detailsLabel.visible = record.expanded && detailsLabel.text.length > 0;
    });

    allowBtn.connect('clicked', () => {
        allowBtn.reactive = false;
        denyBtn.reactive = false;
        record.tool.status = 'running';
        refreshToolWidget(record, onRefreshGroup);
        onConfirmTool?.(record.tool.requestId || record.tool.id, true);
    });

    denyBtn.connect('clicked', () => {
        allowBtn.reactive = false;
        denyBtn.reactive = false;
        record.tool.status = 'error';
        record.tool.summary = 'Rejected by user';
        refreshToolWidget(record, onRefreshGroup);
        onConfirmTool?.(record.tool.requestId || record.tool.id, false);
    });

    toolWidgets.set(tool.id, record);
    if (group)
        group.tools.push(tool);
    refreshToolWidget(record, onRefreshGroup);
    toolBox.add_child(badge);
    return record;
}
