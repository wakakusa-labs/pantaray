import type { CaptureEditingRequest } from '../screenshot/captureEditing';
/**
 * MainContext for IPC handlers.
 *
 * Every payload-bearing entry point is typed against the canonical shape
 * the handler will pass to the SSOT after schema validation. Renderer
 * input is parsed in the handler layer (see `ipc/schemas/`), and what
 * crosses this contract is already trusted by the time the SSOT sees it.
 */

import type { LocalConnectionRuntime } from '../aiConnection/localConnectionRuntime';
import type { BrowserWindow } from 'electron';

import type { GlobalShortcutChangeResult, GlobalShortcutState } from './schemas/shortcut';

import type { LocalRuntimeState } from '../auth/localRuntimeState';
import type { CapturePrivacySettings, IdeFileRules } from '../privacy/capturePrivacy';
import type { InstalledAppOption } from '../privacy/installedApps';
import type { RecordingStartResult } from '../screenshot/screenshotSync';
import type { CapturePrivacySettingsInput, IdeFileRulesInput } from './schemas/privacy';
import type { SubmitApprovalDecisionPayload } from '../actions/actionApprovalDecisionFetch';
import type {
  ActionApprovalMode,
  ActionApprovalModeResponse,
} from '../actions/actionApprovalModeFetch';
import type { HistoryFetchResult } from '../history/historyFetch';
import type { HistoryItemDeleteRequest } from '../history/historyContracts';
import type { HistoryItemDeleteResult } from '../history/historyItemDelete';
import type { ActionCompletionViewedRequest } from '../history/actionReadState';
import type {
  ReadAccessScope,
  ReadAccessScopeSettings,
  CommandNetworkSettings,
  WorkspaceFolder,
  WorkspaceOrganization,
  WorkspaceProject,
  WorkspaceSettings,
} from '../settings/workspaceSettingsFetch';
import type { IpcMainLike } from './registrar';
import type {
  AuxiliaryIpcWindowRole,
  IpcChannel,
  IpcSenderEvent,
  IpcSenderIdentity,
  IpcWindowRole,
} from './senderTrust';
import type {
  CreateFolderInput,
  CreateOrganizationInput,
  CreateProjectInput,
  ReorderProjectsInput,
  UpdateFolderLinksInput,
  UpdateProjectLinksInput,
} from './schemas/workspaceSettings';
import type { ActionFileOpenInput } from './schemas/actionFiles';
import type { createActionFetcher } from '../actions/actionFetch';
import type {
  CreateNotificationIpcHandlers,
  AcceptActionRequest,
  OrchestrationClientEvent,
  OverlayBootstrapResponse,
  OverlaySnapshot,
  ResumeProcessRequest,
  UiLanguage,
} from '../orchestration/contracts';

export type { UiLanguage } from '../orchestration/contracts';

export type AuthStatus = 'initializing' | 'authenticated' | 'unauthenticated' | 'expired';

export type AuthState = {
  authStatus: AuthStatus;
  isLoggedIn: boolean;
  user: { id: string; email?: string | null } | null;
  runtimeState: LocalRuntimeState;
};

/**
 * What the recording screen reads: whether macOS lets the recorder run at all,
 * whether a conversation is waiting on that, and whether the screen was already
 * answered with "later". Nothing here is stored — the gate is the live OS
 * permission, and the answer is held in main for this app run only.
 */
export type CaptureGateState = {
  osPermissionsGranted: boolean;
  introRequested: boolean;
  introDismissed: boolean;
};

/**
 * The main window's "update ready" notice: a downloaded update waiting for a restart.
 * `version` is what electron-updater reported for it. Main holds the "later" answer
 * for this app run, so a dismissed notice reads as `null` in a reopened window too.
 */
export type UpdateReadyNotice = { version: string | null };

// Main-window pages that main opens on its own; each has a caller.
export type MainWindowRoute = '/login' | '/workspace';

export type MainContext = {
  aiConnection: Pick<
    LocalConnectionRuntime,
    | 'getSettings'
    | 'getStatus'
    | 'savePreferences'
    | 'saveApiKey'
    | 'removeApiKey'
    | 'saveWebSearchKey'
    | 'removeWebSearchKey'
    | 'signInToChatgpt'
    | 'cancelChatgptSignIn'
    | 'disconnectChatgpt'
  >;
  ipcMain: IpcMainLike;

  security: {
    authorize: (channel: IpcChannel, event: IpcSenderEvent) => IpcWindowRole;
    auditMutation: (operation: string, outcome: 'succeeded' | 'failed') => void;
    registerWindow: (role: AuxiliaryIpcWindowRole, sender: IpcSenderIdentity) => void;
    unregisterWindow: (sender: IpcSenderIdentity) => void;
  };

  windows: {
    getMainWindow: () => BrowserWindow | null;
    showMainRoute: (route: MainWindowRoute) => void;
    openNewConversationOverlay: () => void;
    openActionConversationOverlay: (actionId: string) => void;
  };

  auth: {
    saveLoginState: (isLoggedIn: boolean) => void;
    getState: () => AuthState;
    signOut: () => Promise<{ ok: boolean; error?: string }>;
    startBrowserLogin: (route: unknown) => Promise<
      | {
          ok: true;
          attempt_id: string;
          code_challenge: string;
          route: string;
          loopback_redirect_url: string;
        }
      | { ok: false; error: string }
    >;
  };

  history: {
    fetch: (params: unknown) => Promise<HistoryFetchResult>;
    markCompletionViewed: (request: ActionCompletionViewedRequest) => void;
    deleteItem: (request: HistoryItemDeleteRequest) => Promise<HistoryItemDeleteResult>;
  };

  actionFiles: {
    /** Selects the path in Finder; never opens or executes it. */
    open: (params: ActionFileOpenInput) => void;
  };

  actionImages: {
    /** `LOCAL_ARTIFACT_ROOT`; images live under `{root}/generated/images`. */
    localArtifactRoot: string;
    /** `nativeImage` decode, injected so the handler stays testable outside Electron. */
    decodeImageDimensions: (bytes: Buffer) => { widthPx: number; heightPx: number } | null;
    revealInFolder: (absolutePath: string) => void;
  };

  actions: ReturnType<typeof createActionFetcher> & {
    getCurrentSubjectId: () => string | null;
    resolveOverlayIdForSender: (sender: IpcSenderIdentity) => string | null;
    registerActionAssociation: (actionId: string, overlayId: string) => void;
    refreshActionConversation: (actionId: string) => void;
  };

  ui: {
    getLanguage: () => UiLanguage;
    setLanguage: (lang: unknown) => UiLanguage;
  };

  update: {
    getReadyNotice: () => UpdateReadyNotice | null;
    /** The same restart the app and tray menus' "Restart to update" makes. */
    restartToUpdate: () => void;
  };

  approval: {
    getWorkspaceEditCommandPreference: () => Promise<unknown>;
    setWorkspaceEditCommandPreference: (approvalMode: unknown) => Promise<unknown>;
  };

  shortcut: {
    getState: () => GlobalShortcutState;
    setAccelerator: (accelerator: string) => GlobalShortcutChangeResult;
  };

  workspaceSettings: {
    get: () => Promise<WorkspaceSettings>;
    getReadAccessScope: () => Promise<ReadAccessScopeSettings>;
    getCommandNetwork: () => Promise<CommandNetworkSettings>;
    updateCommandNetwork: (enabled: boolean) => Promise<CommandNetworkSettings>;
    createOrganization: (input: CreateOrganizationInput) => Promise<WorkspaceOrganization>;
    createProject: (input: CreateProjectInput) => Promise<WorkspaceProject>;
    createFolder: (input: CreateFolderInput) => Promise<WorkspaceFolder>;
    reorderProjects: (input: ReorderProjectsInput) => Promise<{ project_ids: string[] }>;
    deleteOrganization: (organizationId: string) => Promise<void>;
    deleteProject: (projectId: string) => Promise<void>;
    deleteFolder: (folderId: string) => Promise<void>;
    updateProjectLinks: (
      projectId: string,
      input: UpdateProjectLinksInput
    ) => Promise<WorkspaceProject>;
    updateFolderLinks: (
      folderId: string,
      input: UpdateFolderLinksInput
    ) => Promise<WorkspaceFolder>;
    updateReadAccessScope: (
      readAccessScope: ReadAccessScope
    ) => Promise<{ read_access_scope: ReadAccessScope }>;
    selectFolder: () => Promise<{ canceled: boolean; path: string | null }>;
  };

  screenshot: {
    getStatus: () => boolean;
    start: () => Promise<RecordingStartResult>;
    stop: () => Promise<boolean>;
    getGateState: () => CaptureGateState;
    /**
     * Answers the recording screen with "later" for the rest of this app run, in the
     * name of the owner it was shown to. One gate is held for the whole app run, so an
     * answer that no longer names the current owner is not taken: it only reads the gate,
     * the same read the current owner's own screen makes.
     */
    dismissIntro: (ownerId: string) => CaptureGateState;
    openPermissionSettings: () => Promise<void>;
  };

  privacy: {
    getCaptureSettings: () => CapturePrivacySettings;
    // Input types are derived from the IPC schemas (permissive — partial
    // sections, omitted timestamps). Return types are the fully normalized
    // SSOT shape.
    updateCaptureSettings: (next: CapturePrivacySettingsInput) => Promise<CapturePrivacySettings>;
    listInstalledApps: () => Promise<InstalledAppOption[]>;
    getIdeFileRules: () => IdeFileRules;
    setCaptureEditing: (request: CaptureEditingRequest) => Promise<boolean>;
    updateIdeFileRules: (nextRules: IdeFileRulesInput) => Promise<IdeFileRules>;
  };

  externalUrl: {
    open: (rawUrl: unknown) => Promise<void>;
  };

  ws: {
    send: (message: OrchestrationClientEvent) => void | Promise<void>;
    acceptAction: (request: AcceptActionRequest) => Promise<OverlaySnapshot | null>;
    getStatus: () => { status: 'connected' | 'disconnected' };
  };

  overlay: {
    resumeLiveProcess: (payload: ResumeProcessRequest) => void;
    resolveOverlayBootstrap: (suggestionId: string) => Promise<OverlayBootstrapResponse | null>;
    submitApprovalDecision: (payload: SubmitApprovalDecisionPayload) => Promise<void>;
    getActionApprovalMode: (actionId: string) => Promise<ActionApprovalModeResponse>;
    setActionApprovalMode: (
      actionId: string,
      approvalMode: ActionApprovalMode
    ) => Promise<ActionApprovalModeResponse>;
    createNotificationIpcHandlers?: CreateNotificationIpcHandlers;
  };
};
