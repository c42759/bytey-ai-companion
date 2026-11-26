// SPDX-License-Identifier: GPL-3.0-or-later

import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';

import {IslandContextMenu} from './contextMenu.js';
import {DaemonClient} from './daemonClient.js';
import {IslandWidget} from './islandWidget.js';
import {PositionManager} from './positionManager.js';
import {TranscriptDrawer} from './transcriptDrawer.js';

/**
 * GNOME Shell 48-51 Extension entrypoint for Bytey AI Companion Dynamic Island.
 */
export default class ByteyExtension extends Extension {
    enable() {
        this._settings = this.getSettings();
        this._daemon = new DaemonClient(this._settings, {
            onStatus: status => this._drawer?.setConnectionStatus(status),
            onConnected: () => {
                this._drawer?.setConnectionStatus('connected');
                if (!Main.overview.visible)
                    this._islandWidget?.show();
                this._loadInitialData().catch(() => {});
            },
            onEvent: event => this._handleWsEvent(event),
        });

        this._settingsChangedId = this._settings.connect('changed::daemon-url', () => {
            if (this._daemon?.onDaemonUrlChanged())
                this._loadInitialData().catch(() => {});
        });

        this._config = {
            general: {pet_name: 'Bytey'},
            provider: {active_provider: 'ollama', model: ''},
            voice_in: {mic_muted: false},
            voice_out: {tts_muted: false},
            visuals: {
                animation_mode: 'full',
                cursor_tracking: true,
                position_mode: 'fixed',
                monitor_index: 0,
                fixed_alignment: 'top-center',
                offset_x: 0,
                offset_y: 12,
                float_x: 500,
                float_y: 40,
                chat_width: 380,
                chat_height: 420,
                chat_density: 'comfortable',
                eye_color: '#F7FAFF',
                background_color: '#0A0A0C',
                border_color: '#2A2C34',
            },
        };

        this._activeSessionId = null;
        this._sessions = [];
        this._daemonState = 'IDLE';

        this._positionManager = new PositionManager();

        this._drawer = new TranscriptDrawer(this._positionManager, {
            onSendText: (text, attachments) => this._handleSendText(text, attachments),
            onClearChat: () => this._handleNewChat(),
            onReplayTts: (messageId, text) => this._daemon.replayTts(this._activeSessionId, messageId, text),
            onDeleteMessage: messageId => this._handleDeleteMessage(messageId),
            onTogglePauseTts: () => this._daemon.pauseTts(),
            onManualMic: () => this._daemon.triggerVoice((this._islandWidget?.state ?? 'IDLE') === 'SPEAKING'),
            onConfirmTool: (callId, approved) => this._daemon.confirmTool(callId, approved),
            onResizeCommit: (width, height) => {
                this._patchConfig({
                    visuals: {
                        chat_width: Math.round(width),
                        chat_height: Math.round(height),
                    },
                }).catch(() => {});
            },
            onAutoAcceptChange: enabled => {
                this._patchConfig({
                    workspace: {
                        require_write_approval: !enabled,
                        require_command_approval: !enabled,
                    },
                }).catch(() => {});
            },
            onGodModeChange: enabled => {
                this._patchConfig({
                    agents: {
                        god_mode_enabled: Boolean(enabled),
                    },
                }).catch(() => {});
            },
            onOpenChanged: isOpen => {
                if (isOpen && !this._drawer?.hasPendingConfirmation() && !this._isDaemonBusy())
                    this._refreshActiveSession().catch(() => {});
            },
            onSelectSession: sessionId => this._handleSelectSession(sessionId),
            onDeleteSession: sessionId => this._handleDeleteSession(sessionId),
            onRenameSession: (sessionId, title) => this._handleRenameSession(sessionId, title),
            onCreateSession: () => this._handleNewChat(),
            onToggleSidebar: isOpen => {
                this._patchConfig({
                    visuals: {chat_sidebar_open: Boolean(isOpen)},
                }).catch(() => {});
                if (isOpen)
                    this._refreshSessionsList().catch(() => {});
            },
            onSelectWorkspace: path => this._handleSelectWorkspace(path),
            onOpenWorkspaceSettings: () => this.openPreferences(),
            onListModels: provider => this._handleListModels(provider),
            onSelectModel: model => this._handleSelectModel(model),
        });

        this._islandWidget = new IslandWidget(this._positionManager, {
            onLeftClick: () => this._handleLeftClick(),
            onRightClick: () => this._handleRightClick(),
            onMoved: bounds => {
                if (this._drawer?.isOpen) {
                    const monIdx = this._config.visuals?.monitor_index ?? 0;
                    this._drawer.updatePosition(
                        bounds.x,
                        bounds.y,
                        bounds.width,
                        bounds.height,
                        monIdx
                    );
                }
            },
            onDragCommit: (x, y) => this._persistFloatingPosition(x, y),
        });

        this._contextMenu = new IslandContextMenu(this._islandWidget.actor, {
            onOpenPreferences: () => this.openPreferences(),
            onToggleMicMute: muted => this._patchConfig({voice_in: {mic_muted: muted}}),
            onToggleTtsMute: muted => this._patchConfig({voice_out: {tts_muted: muted}}),
            onNewChat: () => this._handleNewChat(),
            onExit: () => this._handleExit(),
        });

        this._monitorsChangedId = Main.layoutManager.connect('monitors-changed', () => {
            if (!this._daemon?.active)
                return;
            this._islandWidget?.reposition();
            this._syncDrawerPosition();
        });
        this._workareasChangedId = global.display.connect('workareas-changed', () => {
            if (!this._daemon?.active)
                return;
            this._islandWidget?.reposition();
            this._syncDrawerPosition();
        });
        this._overviewShowingId = Main.overview.connect('showing', () => {
            if (!this._daemon?.active)
                return;
            this._drawer?.close();
            this._contextMenu?.close();
            this._islandWidget?.fadeOut();
        });
        this._overviewHiddenId = Main.overview.connect('hidden', () => {
            if (!this._daemon?.active || this._daemon.exitedByUser)
                return;
            this._islandWidget?.fadeIn();
        });
        if (Main.overview.visible)
            this._islandWidget?.hide();

        this._applyConfig(this._config);
        this._daemon.connectWebSocket();
        this._loadInitialData().catch(() => {});
    }

    disable() {
        if (this._settings && this._settingsChangedId) {
            this._settings.disconnect(this._settingsChangedId);
            this._settingsChangedId = 0;
        }
        this._settings = null;

        if (this._daemon) {
            this._daemon.destroy();
            this._daemon = null;
        }

        if (this._monitorsChangedId) {
            Main.layoutManager.disconnect(this._monitorsChangedId);
            this._monitorsChangedId = 0;
        }
        if (this._workareasChangedId) {
            global.display.disconnect(this._workareasChangedId);
            this._workareasChangedId = 0;
        }
        if (this._overviewShowingId) {
            Main.overview.disconnect(this._overviewShowingId);
            this._overviewShowingId = 0;
        }
        if (this._overviewHiddenId) {
            Main.overview.disconnect(this._overviewHiddenId);
            this._overviewHiddenId = 0;
        }

        if (this._contextMenu) {
            this._contextMenu.destroy();
            this._contextMenu = null;
        }

        if (this._drawer) {
            this._drawer.destroy();
            this._drawer = null;
        }

        if (this._islandWidget) {
            this._islandWidget.destroy();
            this._islandWidget = null;
        }

        this._positionManager = null;
        this._config = null;
        this._activeSessionId = null;
        this._sessions = [];
        this._daemonState = 'IDLE';
    }

    _openDrawerAtIsland(action = 'open') {
        if (!this._daemon?.active || !this._islandWidget || !this._drawer)
            return;
        const b = this._islandWidget.getBounds();
        const monIdx = this._config?.visuals?.monitor_index ?? 0;
        this._drawer[action](b.x, b.y, b.width, b.height, monIdx);
    }

    _handleLeftClick() {
        if (!this._daemon?.active || !this._islandWidget || !this._drawer)
            return;
        this._contextMenu?.close();
        this._openDrawerAtIsland('toggle');
    }

    _handleRightClick() {
        if (!this._daemon?.active || !this._islandWidget || !this._contextMenu)
            return;
        this._drawer?.close();
        this._contextMenu.toggle(this._islandWidget.isInLowerHalf());
    }

    _syncDrawerPosition() {
        if (!this._drawer?.isOpen)
            return;
        this._openDrawerAtIsland('updatePosition');
    }

    _applyConfig(config) {
        if (!this._daemon?.active || !config)
            return;

        this._config = {
            ...this._config,
            ...config,
            general: {...(this._config?.general ?? {}), ...(config.general ?? {})},
            provider: {...(this._config?.provider ?? {}), ...(config.provider ?? {})},
            voice_in: {...(this._config?.voice_in ?? {}), ...(config.voice_in ?? {})},
            voice_out: {...(this._config?.voice_out ?? {}), ...(config.voice_out ?? {})},
            visuals: {...(this._config?.visuals ?? {}), ...(config.visuals ?? {})},
            workspace: {...(this._config?.workspace ?? {}), ...(config.workspace ?? {})},
            agents: {...(this._config?.agents ?? {}), ...(config.agents ?? {})},
        };

        this._islandWidget?.applyVisualsConfig(this._config.visuals);
        this._islandWidget?.setGodMode(Boolean(this._config.agents?.god_mode_enabled));
        this._contextMenu?.syncFromConfig(this._config);
        this._drawer?.setConfig(this._config);
        this._syncDrawerPosition();
    }

    async _loadInitialData() {
        if (!this._daemon?.active)
            return;
        const cfgResp = await this._daemon.getConfig();
        if (cfgResp.ok && cfgResp.data)
            this._applyConfig(cfgResp.data);
        await this._refreshActiveSession();
    }

    async _refreshActiveSession() {
        if (!this._daemon?.active)
            return;
        const sessionResp = await this._daemon.getActiveSessionOrList();
        if (!this._daemon?.active || !sessionResp.ok || !sessionResp.data)
            return;

        const payload = sessionResp.data;
        let sessionId = null;
        let messages = null;

        if (Array.isArray(payload)) {
            sessionId = payload[0]?.id ?? null;
            this._sessions = payload;
        } else if (typeof payload === 'object') {
            sessionId = payload.active_session_id ??
                payload.session?.id ??
                payload.id ??
                payload.sessions?.[0]?.id ??
                null;
            if (Array.isArray(payload.messages))
                messages = payload.messages;
            if (Array.isArray(payload.sessions))
                this._sessions = payload.sessions;
        }

        this._activeSessionId = sessionId;

        if (messages === null && sessionId) {
            const msgResp = await this._daemon.getSessionMessages(sessionId);
            if (msgResp.ok && msgResp.data)
                messages = this._extractMessages(msgResp.data);
        }

        if (this._daemon?.active && Array.isArray(messages))
            this._drawer?.setMessages(messages);

        await this._refreshSessionsList();
        await this._refreshSkillsList();
        await this._refreshWorkspace();
    }

    async _refreshWorkspace() {
        if (!this._daemon?.active)
            return;
        const resp = await this._daemon.getWorkspace();
        if (!this._daemon?.active || !resp.ok || !resp.data)
            return;
        this._drawer?.setWorkspace(resp.data);
    }

    async _handleSelectWorkspace(path) {
        if (!this._daemon?.active)
            return;
        const resp = await this._daemon.setWorkspace(path);
        if (!this._daemon?.active || !resp.ok)
            return;
        if (resp.data)
            this._drawer?.setWorkspace(resp.data);
        this._refreshSkillsList().catch(() => {});
    }

    async _handleListModels(provider) {
        if (!this._daemon?.active || !provider)
            return [];
        const resp = await this._daemon.listModels(provider);
        if (!this._daemon?.active || !resp.ok || !resp.data)
            return [];
        return Array.isArray(resp.data.models) ? resp.data.models : [];
    }

    async _handleSelectModel(model) {
        if (!this._daemon?.active || !model)
            return;
        await this._patchConfig({provider: {model: String(model)}});
    }

    async _refreshSkillsList() {
        if (!this._daemon?.active)
            return;
        const resp = await this._daemon.getSkills();
        if (!this._daemon?.active || !resp.ok || !resp.data)
            return;

        const payload = resp.data;
        const skills = Array.isArray(payload)
            ? payload
            : (Array.isArray(payload.skills) ? payload.skills : []);
        const enabled = typeof payload.enabled === 'boolean' ? payload.enabled : true;
        this._drawer?.setSkills(skills, enabled);
    }

    async _refreshSessionsList() {
        if (!this._daemon?.active)
            return;
        const resp = await this._daemon.getSessions();
        if (!this._daemon?.active || !resp.ok || !resp.data)
            return;

        const payload = resp.data;
        const sessions = Array.isArray(payload)
            ? payload
            : (Array.isArray(payload.sessions) ? payload.sessions : []);
        this._sessions = sessions;

        if (!this._activeSessionId && payload && typeof payload === 'object') {
            this._activeSessionId = payload.active_session_id ??
                sessions[0]?.id ??
                null;
        }

        this._drawer?.setSessions(this._sessions, this._activeSessionId);
    }

    _isDaemonBusy() {
        const state = String(this._daemonState ?? this._islandWidget?.state ?? 'IDLE').toUpperCase();
        return state === 'THINKING' || state === 'TOOL_CALLING' || state === 'SPEAKING';
    }

    async _handleSelectSession(sessionId) {
        if (!this._daemon?.active || !sessionId || this._isDaemonBusy())
            return;

        if (sessionId === this._activeSessionId) {
            this._drawer?.focusInput();
            return;
        }

        const resp = await this._daemon.activateSession(sessionId);
        if (!this._daemon?.active || !resp.ok || !resp.data)
            return;

        this._activeSessionId = resp.data.active_session_id ?? sessionId;
        const messages = Array.isArray(resp.data.messages) ? resp.data.messages : [];
        this._drawer?.setSessions(this._sessions, this._activeSessionId);
        this._drawer?.loadMessages(messages);
        this._drawer?.focusInput();
    }

    async _handleDeleteSession(sessionId) {
        if (!this._daemon?.active || !sessionId)
            return;

        const wasActive = sessionId === this._activeSessionId;
        if (wasActive && this._isDaemonBusy())
            return;

        const resp = await this._daemon.deleteSession(sessionId);
        if (!this._daemon?.active || !resp.ok)
            return;

        const nextActiveId = resp.data?.active_session_id ?? null;
        if (wasActive || (nextActiveId && nextActiveId !== this._activeSessionId)) {
            this._activeSessionId = nextActiveId;
            if (this._activeSessionId) {
                const msgResp = await this._daemon.getSessionMessages(this._activeSessionId);
                if (this._daemon?.active && msgResp.ok && msgResp.data)
                    this._drawer?.loadMessages(this._extractMessages(msgResp.data));
            } else {
                this._drawer?.clearMessages();
            }
        }

        await this._refreshSessionsList();
    }

    async _handleRenameSession(sessionId, title) {
        if (!this._daemon?.active || !sessionId)
            return;

        const cleanTitle = typeof title === 'string' ? title.trim() : '';
        if (!cleanTitle)
            return;

        const resp = await this._daemon.renameSession(sessionId, cleanTitle);
        if (!this._daemon?.active || !resp.ok)
            return;

        await this._refreshSessionsList();
    }

    async _patchConfig(patch) {
        if (!this._daemon?.active)
            return;
        this._applyConfig(patch);
        const resp = await this._daemon.patchConfig(patch);
        if (resp.ok && resp.data && typeof resp.data === 'object')
            this._applyConfig(resp.data);
    }

    _persistFloatingPosition(x, y) {
        this._patchConfig({
            visuals: {
                float_x: Math.round(x),
                float_y: Math.round(y),
            },
        }).catch(() => {});
    }

    async _handleSendText(text, attachments = []) {
        if (!this._daemon?.active)
            return;
        const body = {
            content: text,
            text,
            input_mode: 'text',
            source: 'text',
        };
        if (Array.isArray(attachments) && attachments.length > 0)
            body.attachments = attachments;

        const resp = await this._daemon.sendMessage(this._activeSessionId, body);
        if (this._daemon?.active && resp.ok && resp.data) {
            const msg = resp.data.user_message ?? resp.data.message ?? resp.data;
            if (msg && (msg.id || msg.message_id) && (msg.content || msg.text)) {
                this._drawer?.upsertMessageDelta({
                    message_id: msg.id ?? msg.message_id,
                    role: msg.role ?? 'user',
                    source: msg.input_mode ?? 'text',
                    text: msg.content ?? msg.text,
                    is_final: true,
                });
            }
            if (resp.data.session_id)
                this._activeSessionId = resp.data.session_id;
        }
    }

    async _handleNewChat() {
        if (!this._daemon?.active || this._isDaemonBusy())
            return;

        const resp = await this._daemon.createOrResetSession(this._activeSessionId);
        if (this._daemon?.active) {
            if (resp.ok && resp.data)
                this._activeSessionId = resp.data.id ?? resp.data.session?.id ?? null;
            this._drawer?.clearMessages();
            await this._refreshSessionsList();
            this._drawer?.focusInput();
        }
    }

    async _handleDeleteMessage(messageId) {
        if (!this._daemon?.active || !messageId)
            return;
        const resp = await this._daemon.deleteMessage(this._activeSessionId, messageId);
        if (!this._daemon?.active || !resp.ok)
            return;
        this._drawer?.removeMessage(messageId);
        await this._refreshSessionsList();
    }

    _extractMessages(data) {
        return Array.isArray(data) ? data : (data?.messages ?? []);
    }

    _beginShutdown() {
        this._daemon?.markExited();
        this._drawer?.setConnectionStatus('disconnected');
        this._drawer?.close();
        this._contextMenu?.close();
        this._islandWidget?.hide();
    }

    async _handleExit() {
        this._beginShutdown();
        await this._daemon?.exitSystem();
        this._daemon?.closeWebSocket();
    }

    _handleWsEvent(event) {
        if (!this._daemon?.active || !event || typeof event !== 'object')
            return;

        switch (event.type) {
        case 'state_changed': {
            const state = event.state ?? 'IDLE';
            this._daemonState = String(state).toUpperCase();
            this._islandWidget?.setState(state, event.emotion, event.label);
            this._drawer?.setListening(this._daemonState === 'LISTENING');
            if (this._daemonState !== 'SPEAKING')
                this._drawer?.setSpeakingMessage(null, 'idle');
            else if (event.speaking_message_id)
                this._drawer?.setSpeakingMessage(event.speaking_message_id, 'playing');
            break;
        }
        case 'tts_playback': {
            this._drawer?.setSpeakingMessage(
                event.message_id ?? null,
                event.status ?? 'idle'
            );
            break;
        }
        case 'message_deleted': {
            this._drawer?.removeMessage(event.message_id);
            break;
        }
        case 'audio_level': {
            this._islandWidget?.setAudioLevel(event.source, event.rms);
            break;
        }
        case 'transcript_delta': {
            if (event.session_id && event.session_id !== this._activeSessionId) {
                if (this._activeSessionId !== null)
                    this._drawer?.clearMessages();
                this._activeSessionId = event.session_id;
            }
            this._drawer?.upsertMessageDelta(event);
            break;
        }
        case 'tool_call_status': {
            const status = String(event.status ?? '').toLowerCase();
            if (status === 'running' && event.tool)
                this._islandWidget?.setState('TOOL_CALLING', undefined, String(event.tool));
            if (status === 'awaiting_confirm' && !this._drawer?.isOpen)
                this._openDrawerAtIsland('open');
            this._drawer?.updateToolCall(event);
            break;
        }
        case 'tool_confirmation_required': {
            if (!this._drawer?.isOpen)
                this._openDrawerAtIsland('open');
            this._drawer?.showToolConfirmation(event);
            break;
        }
        case 'workspace_updated': {
            this._drawer?.setWorkspace(event.workspace ?? event);
            this._refreshSkillsList().catch(() => {});
            break;
        }
        case 'config_updated': {
            if (event.config && typeof event.config === 'object') {
                this._applyConfig(event.config);
                if (event.config.skills !== undefined)
                    this._refreshSkillsList().catch(() => {});
            }
            break;
        }
        case 'skills_updated': {
            if (Array.isArray(event.skills))
                this._drawer?.setSkills(event.skills, event.enabled ?? true);
            else
                this._refreshSkillsList().catch(() => {});
            break;
        }
        case 'position_updated': {
            const visuals = event.visuals ?? event;
            this._applyConfig({visuals});
            break;
        }
        case 'sessions_updated': {
            if (Array.isArray(event.sessions)) {
                this._sessions = event.sessions;
                if (event.active_session_id && event.active_session_id !== this._activeSessionId) {
                    this._activeSessionId = event.active_session_id;
                    if (Array.isArray(event.messages))
                        this._drawer?.loadMessages(event.messages);
                }
                this._drawer?.setSessions(this._sessions, this._activeSessionId);
            } else {
                this._refreshSessionsList().catch(() => {});
            }
            break;
        }
        case 'session_activated': {
            const nextId = event.session_id ?? event.active_session_id ?? null;
            if (nextId)
                this._activeSessionId = nextId;
            if (Array.isArray(event.messages)) {
                this._drawer?.loadMessages(event.messages);
            } else if (this._activeSessionId) {
                this._daemon.getSessionMessages(this._activeSessionId).then(msgResp => {
                    if (this._daemon?.active && msgResp.ok && msgResp.data)
                        this._drawer?.loadMessages(this._extractMessages(msgResp.data));
                }).catch(() => {});
            }
            this._drawer?.setSessions(this._sessions, this._activeSessionId);
            break;
        }
        case 'shutdown': {
            this._beginShutdown();
            this._daemon?.closeWebSocket();
            break;
        }
        default:
            break;
        }
    }
}
