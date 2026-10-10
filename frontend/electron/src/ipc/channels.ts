/**
 * IPC チャンネルの許可リスト（Single Source of Truth）
 *
 * 目的:
 * - preload / main の双方で「許可するIPCチャンネル」を単一定義し、ズレを防ぐ。
 * - 変更時は必ずテストで差分検知できるようにする。
 */

export const validSendChannels = [
  'resize-notification-window',
  'notification-action-accept',
  'notification-hide',
  'notification-stop-action',
  'overlay:recordInteraction',
  'overlay:dragStart',
  'overlay:dragMove',
  'overlay:dragEnd',
  'overlay:openWorkspaceSettings',
  'history:openOverlay',
  'open-external-url',
  // WebSocket bridge (connect/disconnect は main に統一のため未公開)
  'ws:send',
] as const;

export const validReceiveChannels = [
  'aiConnection:changed',
  // Auth state (main SSOT)
  'auth:stateChanged',
  // History realtime hint (main SSOT)
  'history:changed',
  // WebSocket bridge
  'ws:event',
  'ws:status',
  'overlay:snapshot',
  'overlay:focusComposer',
  'action:conversationUpdated',
  // Main window only: a suggestion record main holds changed, or is about to be cleared
  'suggestion:snapshot',
  // Main window only: an item appended to the user's chat, and the overlay's "show in chat"
  'chat:itemAppended',
  'chat:turnState',
  'history:showChat',
  // Main window only: select this work in History (a task a panel started)
  'history:showItem',
  // A task's shared draft as another window changed it
  'action:draftChanged',
  // Screenshot status change notification
  'screenshot:statusChanged',
  // Capture allowlist settings update
  'privacy:captureSettingsUpdated',
  // Capture gate: re-read it now (main brought this window forward)
  'recording:gateStateChanged',
  // UI language change
  'ui:languageChanged',
  // A downloaded update became ready: re-read the notice
  'update:readyNoticeChanged',
] as const;

export const validInvokeChannels = [
  'aiConnection:getState',
  'aiConnection:update',
  'window:getPosition',
  'window:move',
  'window:close',
  'auth:confirmationComplete',
  'auth:saveLoginState',
  // Auth state (main SSOT)
  'auth:getState',
  'auth:signOut',
  // Browser login start (PKCE attempt)
  'auth:startBrowserLogin',
  'approval:getWorkspaceEditCommandPreference',
  'approval:setWorkspaceEditCommandPreference',
  'shortcut:getState',
  'shortcut:setAccelerator',
  'overlayPlacement:get',
  'overlayPlacement:set',
  'workspaceSettings:get',
  'workspaceSettings:getReadAccessScope',
  'workspaceSettings:getCommandNetwork',
  'workspaceSettings:updateCommandNetwork',
  'workspaceSettings:createOrganization',
  'workspaceSettings:createProject',
  'workspaceSettings:renameProject',
  'workspaceSettings:createFolder',
  'workspaceSettings:reorderProjects',
  'workspaceSettings:deleteOrganization',
  'workspaceSettings:deleteProject',
  'workspaceSettings:deleteFolder',
  'workspaceSettings:updateProjectLinks',
  'workspaceSettings:updateFolderLinks',
  'workspaceSettings:updateReadAccessScope',
  'workspaceSettings:selectFolder',
  'workspaceSettings:openFolder',
  'actionFile:open',
  // Main window: preview a file an Action names, or open it in its default app or another
  'actionFile:read',
  'actionFile:openInApp',
  'actionFile:openWithApp',
  'actionFile:reveal',
  'actionFile:quickLook',
  // Overlay copy buttons: written by main, so a window without focus still copies.
  'clipboard:writeText',
  'action:submitMessage',
  'action:resume',
  // Main window: show an Action in place (refresh and live resume, no window)
  'action:openConversation',
  // Composer image attachments (write) and "reveal in Finder" for a stored image
  'action:attachImage',
  'actionImage:reveal',
  // Composer document attachments: stage a file, or discard one the user removed
  'action:attachFile',
  'action:discardAttachment',
  'action:readConversationPage',
  'action:readToolOutputPage',
  // The single chat (main window only)
  'chat:sendMessage',
  'chat:listItems',
  'chat:retryTurn',
  'chat:getTurnState',
  // Main window: read a suggestion into main's record without opening a panel
  'suggestion:read',
  // Suggestion history (main SSOT)
  'history:fetch',
  'history:markCompletionViewed',
  'history:deleteItem',
  'history:openNewConversation',
  'history:openConversation',
  'screenshot:getStatus',
  'screenshot:start',
  'screenshot:stop',
  // Capture gate: does macOS grant the recorder its permissions, and is a
  // conversation waiting on them.
  'recording:getGateState',
  'recording:dismissIntro',
  'recording:openPermissionSettings',
  'ws:getStatus',
  // UI language (SSOT)
  'ui:getLanguage',
  'ui:setLanguage',
  // Update ready notice (main window)
  'update:getReadyNotice',
  'update:restartToUpdate',
  // Recording filter (apps / websites, exclude or include-only)
  'privacy:getCaptureSettings',
  'privacy:updateCaptureSettings',
  'privacy:listInstalledApps',
  // IDE file rules
  'privacy:getIdeFileRules',
  'privacy:updateIdeFileRules',
  // 編集モード（全アプリのキャプチャを一時停止）
  'privacy:setCaptureEditing',
  'ws:acceptAction',
  'overlay:submitApprovalDecision',
  'overlay:getActionApprovalMode',
  'overlay:setActionApprovalMode',
  // Overlay → main window: show this Action's latest card in the chat
  'overlay:showChat',
  // Overlay → main window: the panel closes and the main window opens the task it started
  'overlay:openTask',
  // Shared task drafts, for every composer that shows a task
  'action:openDraft',
  'action:updateDraft',
  'action:closeDraft',
] as const;

export type ValidSendChannel = (typeof validSendChannels)[number];
export type ValidReceiveChannel = (typeof validReceiveChannels)[number];
export type ValidInvokeChannel = (typeof validInvokeChannels)[number];
