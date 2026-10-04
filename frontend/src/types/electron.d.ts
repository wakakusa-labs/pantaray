import type { CaptureEditingRequest } from '../../electron/src/screenshot/captureEditing';
import type {
  CapturePrivacySettings,
  IdeFileRules,
} from '../../electron/src/privacy/capturePrivacy';
import type {
  CapturePrivacySettingsInput,
  IdeFileRulesInput,
} from '../../electron/src/ipc/schemas/privacy';
import type { AuthState, UpdateReadyNotice } from '../../electron/src/ipc/context';
import type {
  ConnectionCommand,
  ConnectionStateResult,
  ConnectionUpdateResult,
} from '../../electron/src/ipc/schemas/aiConnection';
import type {
  ActionTerminalStatus,
  AcceptActionRequest,
  RendererOrchestrationClientEvent,
  OrchestrationServerEvent,
  OrchestrationStatus,
} from './websocket';
import type { UiLanguage } from '../i18n/types';
import type {
  ActionMessageRequest,
  ActionMessageSubmitResult,
  ActionResumeRequest,
  ActionToolOutputDetail,
} from '../../electron/src/actions/actionContracts';
import type {
  ActionConversationPageReadResult,
  ActionConversationPageRequest,
  ActionToolOutputRequest,
} from '../../electron/src/actions/actionFetch';
import type { ActionLiveUpdate } from '../../electron/src/actions/actionLiveCore';
import type { ActionImageAttachResult } from '../../electron/src/ipc/schemas/actionImages';
import type { ActionAttachFileResult } from '../../electron/src/ipc/schemas/actionAttachments';
import type { ActionImageMimeType } from '../../electron/src/protocol/imageStoragePath';
import type { RecordingStartResult } from '../../electron/src/screenshot/screenshotSync';
import type { HistoryFetchResult } from '../../electron/src/history/historyFetch';
import type { ActionCompletionViewedRequest } from '../../electron/src/history/actionReadState';
import type {
  ConversationHistoryRequest,
  HistoryItemDeleteRequest,
} from '../../electron/src/history/historyContracts';
import type { HistoryItemDeleteResult } from '../../electron/src/history/historyItemDelete';

import type { CommandNetworkSettings } from '../../electron/src/settings/workspaceSettingsFetch';

export {};

type ElectronCaptureGateState = {
  /** 記録に必要な macOS の権限が付与されているか（毎回 OS に問い合わせる） */
  osPermissionsGranted: boolean;
  /** 権限がないまま要求された会話が main で待っているか */
  introRequested: boolean;
  /** 記録画面を「あとで」で閉じたか（main がアプリ起動中だけ保持する） */
  introDismissed: boolean;
};

type ElectronInstalledApp = {
  name: string;
  bundleId: string | null;
  iconDataUrl: string | null;
};

type ElectronWorkspaceOrganization = {
  organization_id: string;
  display_name: string;
};

type ElectronWorkspaceProject = {
  project_id: string;
  display_name: string;
  sort_order: number;
  organization_ids: string[];
};

type ElectronWorkspaceFolder = {
  folder_id: string;
  display_name: string;
  real_path: string;
  canonical_real_path: string;
  organization_ids: string[];
  project_ids: string[];
};

type ElectronWorkspaceSettings = {
  read_access_scope: 'workspace' | 'full_access';
  organizations: ElectronWorkspaceOrganization[];
  projects: ElectronWorkspaceProject[];
  folders: ElectronWorkspaceFolder[];
};

type ElectronGlobalShortcutFailure =
  | 'settings_unreadable'
  | 'registration_unavailable'
  | 'persistence_failed';

type ElectronGlobalShortcutState = {
  accelerator: string | null;
  failure: ElectronGlobalShortcutFailure | null;
};

type ElectronGlobalShortcutChangeResult =
  | { ok: true; state: ElectronGlobalShortcutState }
  | {
      ok: false;
      reason: 'registration_unavailable' | 'persistence_failed';
      state: ElectronGlobalShortcutState;
    };

type ElectronOverlaySnapshot = {
  suggestionId: string;
  commandId: string | null;
  interactionContract: 'action_offer' | 'message_only' | null;
  suggestionText: string;
  reactionState: 'accepted' | 'rejected' | null;
  reactionTimestamp: string | null;
  actionPhase: 'idle' | 'requesting' | 'accepted_pending_start' | 'processing' | 'terminal';
  actionStatus: 'idle' | 'processing' | ActionTerminalStatus | null;
  actionErrorCode: string | null;
  actionFailureStage: string | null;
  actionFailureMessagePublic: string | null;
  processId: string | null;
  actionId: string | null;
  updatedAt: string | null;
  lastSequence: number;
  isLive: boolean;
};

type ElectronOverlaySnapshotPayload = {
  snapshot: ElectronOverlaySnapshot;
  initialUiState?: {
    expand?: boolean;
  } | null;
};

type ElectronOverlayDragPoint = {
  screenX: number;
  screenY: number;
};

type ElectronActionApprovalMode = 'prompt_each_time' | 'always_allow';

type ElectronActionApprovalModeResponse = {
  action_id: string;
  approval_mode: ElectronActionApprovalMode;
  source: 'action' | 'user_default';
};

declare global {
  interface Window {
    electron?: {
      ipcRenderer: {
        invoke: <T = unknown>(channel: string, ...args: unknown[]) => Promise<T>;
        send: (channel: string, ...args: unknown[]) => void;
        on: (channel: string, func: (...args: unknown[]) => void) => () => void;
      };
      screenshot?: {
        /** OS 権限の付与状況と、それを待っている会話の有無 */
        getGateState: () => Promise<ElectronCaptureGateState>;
        /** 記録画面を「あとで」で閉じる。main は表示されていた owner の答えだけを受ける */
        dismissIntro: (ownerId: string) => Promise<ElectronCaptureGateState>;
        /** システム設定のプライバシーとセキュリティ→アクセシビリティを開く */
        openPermissionSettings: () => Promise<void>;
        start: () => Promise<RecordingStartResult>;
        stop: () => Promise<boolean>;
        getStatus: () => Promise<boolean>;
        onStatusChanged?: (callback: (isEnabled: boolean) => void) => () => void;
        /** OS 権限を今すぐ読み直す合図（main がこのウィンドウを前面に出したとき） */
        onGateStateChanged?: (callback: () => void) => () => void;
      };
      window: {
        getPosition: () => Promise<{ x: number; y: number }>;
        move: (position: { x: number; y: number }) => void;
        toggleBubble: () => void;
        close: () => void;
        closeBubble: () => void;
        adjustSize?: (route: string) => void;
      };
      process: {
        platform: string;
        env: {
          NODE_ENV: string;
        };
      };
      auth: {
        saveLoginState: (isLoggedIn: boolean) => Promise<{ success: boolean }>;
        confirmationComplete?: () => Promise<boolean>;
        /** main の認証状態を取得（トークンは返さない） */
        getState: () => Promise<AuthState>;
        /** main に保持されたセッションを破棄し、永続化ファイルも削除する */
        signOut: () => Promise<{ ok: boolean; error?: string }>;
        /** 外部ブラウザログイン開始用（PKCE attempt を作る。verifier は main が保持） */
        startBrowserLogin: (route: string) => Promise<{
          ok: boolean;
          attempt_id?: string;
          code_challenge?: string;
          loopback_redirect_url?: string;
          route?: string;
          error?: string;
        }>;
        /** 認証状態の変更通知 */
        onStateChanged?: (cb: (state: AuthState) => void) => () => void;
      };
      history?: {
        fetch: (params: ConversationHistoryRequest) => Promise<HistoryFetchResult>;
        markCompletionViewed: (request: ActionCompletionViewedRequest) => Promise<void>;
        deleteItem: (request: HistoryItemDeleteRequest) => Promise<HistoryItemDeleteResult>;
        openNewConversation: () => Promise<void>;
        /** Resolves to how the Overlay was shown, or why it was not (sign-in or recording). */
        openConversation: (request: {
          actionId: string;
        }) => Promise<
          'created' | 'loading' | 'focused' | 'initializing' | 'login' | 'recording_intro'
        >;
        onChanged?: (cb: (payload: unknown) => void) => () => void;
      };
      actionFiles?: {
        open: (params: { path: string }) => Promise<void>;
      };
      actions?: {
        submitMessage: (request: ActionMessageRequest) => Promise<ActionMessageSubmitResult>;
        resumeAction: (request: ActionResumeRequest) => Promise<ActionMessageSubmitResult>;
        attachImage: (request: {
          bytes: ArrayBuffer;
          declaredMimeType: ActionImageMimeType;
        }) => Promise<ActionImageAttachResult>;
        revealImage: (request: { storagePath: string }) => Promise<{ revealed: boolean }>;
        /** Stages a document; the returned id goes into the next message's `files`. */
        attachFile: (request: {
          bytes: ArrayBuffer;
          name: string;
        }) => Promise<ActionAttachFileResult>;
        discardAttachment: (request: { attachmentId: string }) => Promise<void>;
        readConversationPage: (
          request: ActionConversationPageRequest
        ) => Promise<ActionConversationPageReadResult>;
        readToolOutputPage: (request: ActionToolOutputRequest) => Promise<ActionToolOutputDetail>;
        onConversationUpdated: (callback: (update: ActionLiveUpdate) => void) => () => void;
      };
      ui?: {
        initialLanguage: UiLanguage | null;
        getLanguage: () => Promise<UiLanguage>;
        setLanguage: (lang: UiLanguage) => Promise<UiLanguage>;
        onLanguageChanged?: (cb: (lang: UiLanguage) => void) => () => void;
      };
      update?: {
        /** A downloaded update waiting for a restart; `null` when none, or after "later". */
        getReadyNotice: () => Promise<UpdateReadyNotice | null>;
        restartToUpdate: () => Promise<void>;
        onReadyNoticeChanged: (callback: () => void) => () => void;
      };
      approval?: {
        getWorkspaceEditCommandPreference: () => Promise<{
          scope_type: 'global';
          scope_ref: null;
          approval_mode: 'prompt_each_time' | 'always_allow';
          applies_to: ['workspace_edit_and_command'];
        }>;
        setWorkspaceEditCommandPreference: (
          approvalMode: 'prompt_each_time' | 'always_allow'
        ) => Promise<{
          scope_type: 'global';
          scope_ref: null;
          approval_mode: 'prompt_each_time' | 'always_allow';
          applies_to: ['workspace_edit_and_command'];
        }>;
      };
      aiConnection?: {
        getState: () => Promise<ConnectionStateResult>;
        update: (command: ConnectionCommand) => Promise<ConnectionUpdateResult>;
        onChanged: (callback: () => void) => () => void;
      };
      shortcut?: {
        getState: () => Promise<ElectronGlobalShortcutState>;
        setAccelerator: (accelerator: string) => Promise<ElectronGlobalShortcutChangeResult>;
      };
      workspaceSettings?: {
        get: () => Promise<ElectronWorkspaceSettings>;
        getCommandNetwork: () => Promise<CommandNetworkSettings>;
        updateCommandNetwork: (enabled: boolean) => Promise<CommandNetworkSettings>;
        getReadAccessScope: () => Promise<{
          read_access_scope: 'workspace' | 'full_access';
        }>;
        createOrganization: (input: {
          displayName: string;
        }) => Promise<ElectronWorkspaceOrganization>;
        createProject: (input: {
          displayName: string;
          organizationIds: string[];
        }) => Promise<ElectronWorkspaceProject>;
        createFolder: (input: {
          displayName: string;
          realPath: string;
          organizationIds: string[];
          projectIds: string[];
        }) => Promise<ElectronWorkspaceFolder>;
        reorderProjects: (input: { projectIds: string[] }) => Promise<{ project_ids: string[] }>;
        deleteOrganization: (organizationId: string) => Promise<void>;
        deleteProject: (projectId: string) => Promise<void>;
        deleteFolder: (folderId: string) => Promise<void>;
        updateProjectLinks: (
          projectId: string,
          input: { organizationIds: string[] }
        ) => Promise<ElectronWorkspaceProject>;
        updateFolderLinks: (
          folderId: string,
          input: { organizationIds: string[]; projectIds: string[] }
        ) => Promise<ElectronWorkspaceFolder>;
        updateReadAccessScope: (
          readAccessScope: 'workspace' | 'full_access'
        ) => Promise<{ read_access_scope: 'workspace' | 'full_access' }>;
        selectFolder: () => Promise<{ canceled: boolean; path: string | null }>;
      };
      privacy?: {
        getCaptureSettings: () => Promise<CapturePrivacySettings>;
        updateCaptureSettings: (
          settings: CapturePrivacySettingsInput
        ) => Promise<CapturePrivacySettings>;
        /** アクティブウィンドウの完全情報を取得（appName + title） */
        /** 記録フィルタのアプリ候補（インストール済みアプリ + アイコン） */
        listInstalledApps: () => Promise<ElectronInstalledApp[]>;
        /** IDE file rules（VSCode/Cursor） */
        getIdeFileRules: () => Promise<IdeFileRules>;
        updateIdeFileRules: (rules: IdeFileRulesInput) => Promise<IdeFileRules>;
        /** Chrome/Safari のアクティブタブURL（取得できない場合は url=null + error） */
        /** 編集モード（フィルタ編集中は全アプリのキャプチャを一時停止） */
        setCaptureEditing: (request: CaptureEditingRequest) => Promise<boolean>;
        onCaptureSettingsUpdated?: (cb: (settings: CapturePrivacySettings) => void) => () => void;
      };
      agentOverlay?: {
        /** 履歴オーバーレイ表示（main 側で通知ウィンドウを開く） */
        showHistory?: (payload: {
          suggestionId: string;
          initialUiState?: { expand?: boolean } | null;
          fromStart?: boolean;
        }) => void;
        close: () => Promise<unknown>;
        resize: (height: number) => Promise<unknown>;
        onSnapshot?: (
          func: (payload: ElectronOverlaySnapshotPayload) => void
        ) => (() => void) | undefined;
        submitApprovalDecision?: (payload: {
          actionId: string;
          processId: string;
          approvalSessionId: string;
          toolRequestId: string;
          decision: 'approved_once' | 'approved_for_conversation' | 'denied';
        }) => Promise<void>;
        getActionApprovalMode?: (actionId: string) => Promise<ElectronActionApprovalModeResponse>;
        setActionApprovalMode?: (payload: {
          actionId: string;
          approvalMode: ElectronActionApprovalMode;
        }) => Promise<ElectronActionApprovalModeResponse>;
        dragStart?: (payload: ElectronOverlayDragPoint) => void;
        dragMove?: (payload: ElectronOverlayDragPoint) => void;
        dragEnd?: () => void;
        /** Brings the main window forward on the workspace settings page. */
        openWorkspaceSettings?: () => void;
        acceptAction: (data: unknown) => void;
        rejectAction: (data: unknown) => void;
        hide: () => void;
        stopAction: () => void;
      };
      orchestration?: {
        send: (message: RendererOrchestrationClientEvent) => void;
        acceptAction: (request: AcceptActionRequest) => Promise<ElectronOverlaySnapshot | null>;
        getStatus?: () => Promise<{ status: 'connected' | 'disconnected' }>;
        onEvent?: (cb: (ev: OrchestrationServerEvent) => void) => () => void;
        onStatus?: (cb: (st: OrchestrationStatus) => void) => () => void;
      };
    };
  }
}
