// SPDX-License-Identifier: GPL-3.0-or-later

import Adw from 'gi://Adw';
import Gtk from 'gi://Gtk';

import {
    ANIMATION_MODES,
    CHAT_DENSITIES,
    DEFAULT_BG_COLOR,
    DEFAULT_BORDER_COLOR,
    DEFAULT_EYE_COLOR,
    FIXED_ALIGNMENTS,
    POSITION_MODE_OPTIONS,
    TEST_ANIMATION_EMOTIONS,
    TEST_ANIMATION_STATES,
    hexToRgba,
    positionModeIndexForValue,
    positionModeValueForIndex,
    rgbaToHex,
} from './constants.js';
import {comboText, makeComboRow, makeSpinRow, setComboText} from './widgets.js';

export function buildVisualsPage(ctx) {
    const page = new Adw.PreferencesPage({
        title: 'Visuals & Position',
        icon_name: 'preferences-desktop-display-symbolic',
    });

    const placementGroup = new Adw.PreferencesGroup({title: 'Island Placement'});
    const posModeRow = makeComboRow('Mode', POSITION_MODE_OPTIONS.map(o => o.label));
    const monitorRow = makeSpinRow('Monitor Index', 0, 15, 1);
    const fixedAlignRow = makeComboRow('Fixed Alignment', FIXED_ALIGNMENTS);
    const offsetXRow = makeSpinRow('Offset X', -4000, 4000, 4);
    const offsetYRow = makeSpinRow('Offset Y', -4000, 4000, 4);
    const floatXRow = makeSpinRow('Floating X', 0, 10000, 10);
    const floatYRow = makeSpinRow('Floating Y', 0, 10000, 10);
    placementGroup.add(posModeRow);
    placementGroup.add(monitorRow);
    placementGroup.add(fixedAlignRow);
    placementGroup.add(offsetXRow);
    placementGroup.add(offsetYRow);
    placementGroup.add(floatXRow);
    placementGroup.add(floatYRow);
    page.add(placementGroup);

    const eyesGroup = new Adw.PreferencesGroup({title: 'Eye Animations'});
    const animModeRow = makeComboRow('Animation Mode', ANIMATION_MODES);
    const cursorTrackRow = new Adw.SwitchRow({
        title: 'Cursor Tracking',
        active: true,
    });
    eyesGroup.add(animModeRow);
    eyesGroup.add(cursorTrackRow);
    page.add(eyesGroup);

    const colorControls = {};
    const makeColorRow = (title, fieldKey, defaultHex) => {
        const row = new Adw.ActionRow({
            title,
            subtitle: defaultHex,
        });
        const btn = new Gtk.ColorDialogButton({
            dialog: new Gtk.ColorDialog({with_alpha: true}),
            valign: Gtk.Align.CENTER,
        });
        btn.set_rgba(hexToRgba(defaultHex, defaultHex));
        btn.connect('notify::rgba', () => ctx.onUserChange(() => {
            const hexVal = rgbaToHex(btn.get_rgba());
            row.set_subtitle(hexVal);
            ctx.patchConfig({visuals: {[fieldKey]: hexVal}}, `Saved ${title.toLowerCase()}`);
        }));
        row.add_suffix(btn);
        colorControls[fieldKey] = {row, btn, defaultHex};
        return row;
    };

    const colorsGroup = new Adw.PreferencesGroup({
        title: 'Colors',
        description: 'Customize eye, island background, and border colors.',
    });
    const resetColorsBtn = new Gtk.Button({
        label: 'Reset Defaults',
        valign: Gtk.Align.CENTER,
        css_classes: ['flat'],
    });
    colorsGroup.set_header_suffix(resetColorsBtn);
    colorsGroup.add(makeColorRow('Eye Color', 'eye_color', DEFAULT_EYE_COLOR));
    colorsGroup.add(makeColorRow('Background Color', 'background_color', DEFAULT_BG_COLOR));
    colorsGroup.add(makeColorRow('Border Color', 'border_color', DEFAULT_BORDER_COLOR));
    page.add(colorsGroup);

    const drawerGroup = new Adw.PreferencesGroup({title: 'Chat Drawer'});
    const densityRow = makeComboRow('Chat Density', CHAT_DENSITIES);
    const islandWidthRow = makeSpinRow('Island Width (px)', 140, 450, 4);
    drawerGroup.add(islandWidthRow);

    const chatWidthRow = makeSpinRow('Chat Width (px)', 260, 3000, 10);
    const chatHeightRow = makeSpinRow('Chat Height (px)', 220, 900, 10);
    const chatOpacityRow = makeSpinRow('Chat Opacity (%)', 10, 100, 5);
    drawerGroup.add(densityRow);
    drawerGroup.add(chatWidthRow);
    drawerGroup.add(chatHeightRow);
    drawerGroup.add(chatOpacityRow);
    page.add(drawerGroup);

    const testAnimGroup = new Adw.PreferencesGroup({
        title: 'Test Animations',
        description: 'Preview eye emotions and island states live on the companion.',
    });
    const testStateRow = makeComboRow('Animation State', TEST_ANIMATION_STATES);
    const testEmotionRow = makeComboRow('Emotion', TEST_ANIMATION_EMOTIONS);
    const testAnimActionRow = new Adw.ActionRow({
        title: 'Live Preview',
        subtitle: 'Trigger the selected state and emotion on the island.',
    });
    const testAnimBtnBox = new Gtk.Box({
        orientation: Gtk.Orientation.HORIZONTAL,
        spacing: 8,
        valign: Gtk.Align.CENTER,
    });
    const resetAnimBtn = new Gtk.Button({
        label: 'Reset',
        valign: Gtk.Align.CENTER,
    });
    const previewAnimBtn = new Gtk.Button({
        label: 'Preview',
        valign: Gtk.Align.CENTER,
        css_classes: ['suggested-action'],
    });
    testAnimBtnBox.append(resetAnimBtn);
    testAnimBtnBox.append(previewAnimBtn);
    testAnimActionRow.add_suffix(testAnimBtnBox);
    testAnimGroup.add(testStateRow);
    testAnimGroup.add(testEmotionRow);
    testAnimGroup.add(testAnimActionRow);
    page.add(testAnimGroup);

    const syncPlacementSensitivity = () => {
        const mode = positionModeValueForIndex(posModeRow.get_selected());
        const isFixed = mode === 'fixed';
        const isFloating = mode === 'floating';
        fixedAlignRow.set_sensitive(isFixed);
        offsetXRow.set_sensitive(isFixed);
        offsetYRow.set_sensitive(isFixed);
        floatXRow.set_sensitive(isFloating);
        floatYRow.set_sensitive(isFloating);
    };

    animModeRow.connect('notify::selected', () => ctx.onUserChange(() => {
        ctx.patchConfig({visuals: {animation_mode: comboText(animModeRow)}}, 'Saved animation mode');
    }));

    cursorTrackRow.connect('notify::active', () => ctx.onUserChange(() => {
        ctx.patchConfig({visuals: {cursor_tracking: cursorTrackRow.get_active()}}, 'Saved cursor tracking');
    }));

    resetColorsBtn.connect('clicked', () => ctx.onUserChange(() => {
        ctx.withLoading(() => {
            for (const {row, btn, defaultHex} of Object.values(colorControls)) {
                btn.set_rgba(hexToRgba(defaultHex, defaultHex));
                row.set_subtitle(defaultHex);
            }
        });
        ctx.patchConfig(
            {
                visuals: {
                    eye_color: DEFAULT_EYE_COLOR,
                    background_color: DEFAULT_BG_COLOR,
                    border_color: DEFAULT_BORDER_COLOR,
                },
            },
            'Reset colors to defaults'
        );
    }));

    densityRow.connect('notify::selected', () => ctx.onUserChange(() => {
        ctx.patchConfig({visuals: {chat_density: comboText(densityRow)}}, 'Saved chat density');
    }));

    islandWidthRow.connect('notify::value', () => ctx.onUserChange(() => {
        ctx.patchConfig({visuals: {island_width: Math.round(islandWidthRow.get_value())}}, 'Saved island width');
    }));

    chatWidthRow.connect('notify::value', () => ctx.onUserChange(() => {
        ctx.patchConfig({visuals: {chat_width: Math.round(chatWidthRow.get_value())}}, 'Saved chat width');
    }));

    chatHeightRow.connect('notify::value', () => ctx.onUserChange(() => {
        ctx.patchConfig({visuals: {chat_height: Math.round(chatHeightRow.get_value())}}, 'Saved chat height');
    }));

    chatOpacityRow.connect('notify::value', () => ctx.onUserChange(() => {
        ctx.patchConfig({visuals: {chat_opacity: Math.round(chatOpacityRow.get_value())}}, 'Saved chat opacity');
    }));

    previewAnimBtn.connect('clicked', async () => {
        const state = comboText(testStateRow) || 'IDLE';
        const emotion = comboText(testEmotionRow) || 'neutral';
        const resp = await ctx.httpJson('POST', '/api/v1/visuals/preview', {state, emotion});
        if (ctx.cancellable.is_cancelled())
            return;
        if (resp.ok)
            ctx.showToast(`Previewing ${state} (${emotion})`);
        else
            ctx.showToast(`Preview failed: ${resp.error || `HTTP ${resp.status || 'offline'}`}`);
    });

    resetAnimBtn.connect('clicked', async () => {
        const resp = await ctx.httpJson('POST', '/api/v1/visuals/preview', {reset: true});
        if (ctx.cancellable.is_cancelled())
            return;
        if (resp.ok)
            ctx.showToast('Reset animation to idle');
        else
            ctx.showToast(`Reset failed: ${resp.error || `HTTP ${resp.status || 'offline'}`}`);
    });

    posModeRow.connect('notify::selected', () => ctx.onUserChange(() => {
        syncPlacementSensitivity();
        const mode = positionModeValueForIndex(posModeRow.get_selected());
        ctx.patchConfig({visuals: {position_mode: mode}}, 'Saved mode');
    }));

    monitorRow.connect('notify::value', () => ctx.onUserChange(() => {
        ctx.patchConfig({visuals: {monitor_index: Math.round(monitorRow.get_value())}}, 'Saved monitor index');
    }));

    fixedAlignRow.connect('notify::selected', () => ctx.onUserChange(() => {
        ctx.patchConfig({visuals: {fixed_alignment: comboText(fixedAlignRow)}}, 'Saved fixed alignment');
    }));

    offsetXRow.connect('notify::value', () => ctx.onUserChange(() => {
        ctx.patchConfig({visuals: {offset_x: Math.round(offsetXRow.get_value())}}, 'Saved offset X');
    }));

    offsetYRow.connect('notify::value', () => ctx.onUserChange(() => {
        ctx.patchConfig({visuals: {offset_y: Math.round(offsetYRow.get_value())}}, 'Saved offset Y');
    }));

    floatXRow.connect('notify::value', () => ctx.onUserChange(() => {
        ctx.patchConfig({visuals: {float_x: Math.round(floatXRow.get_value())}}, 'Saved floating X');
    }));

    floatYRow.connect('notify::value', () => ctx.onUserChange(() => {
        ctx.patchConfig({visuals: {float_y: Math.round(floatYRow.get_value())}}, 'Saved floating Y');
    }));

    const applyConfig = visuals => {
        setComboText(animModeRow, visuals.animation_mode ?? 'full');
        cursorTrackRow.set_active(Boolean(visuals.cursor_tracking ?? true));
        for (const [key, {row, btn, defaultHex}] of Object.entries(colorControls)) {
            const rawVal = visuals[key] ?? defaultHex;
            const rgba = hexToRgba(String(rawVal), defaultHex);
            btn.set_rgba(rgba);
            row.set_subtitle(rgbaToHex(rgba));
        }

        setComboText(densityRow, visuals.chat_density ?? 'comfortable');
        islandWidthRow.set_value(Number(visuals.island_width ?? 140));
        chatWidthRow.set_value(Number(visuals.chat_width ?? 380));
        chatHeightRow.set_value(Number(visuals.chat_height ?? 420));
        chatOpacityRow.set_value(Number(visuals.chat_opacity ?? 96));

        posModeRow.set_selected(positionModeIndexForValue(visuals.position_mode ?? 'fixed'));
        monitorRow.set_value(Number(visuals.monitor_index ?? 0));
        setComboText(fixedAlignRow, visuals.fixed_alignment ?? 'top-center');
        offsetXRow.set_value(Number(visuals.offset_x ?? 0));
        offsetYRow.set_value(Number(visuals.offset_y ?? 12));
        floatXRow.set_value(Number(visuals.float_x ?? 500));
        floatYRow.set_value(Number(visuals.float_y ?? 40));
    };

    const syncVisibility = () => syncPlacementSensitivity();

    return {page, applyConfig, syncVisibility};
}
