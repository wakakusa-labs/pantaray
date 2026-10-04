import path from 'path';
import { fileURLToPath } from 'url';

import type { BrowserWindow, IpcMainEvent, IpcMainInvokeEvent, WebContents } from 'electron';

import type { ValidInvokeChannel, ValidSendChannel } from './channels';

export type IpcWindowRole = 'main' | 'overlay';
export type AuxiliaryIpcWindowRole = Exclude<IpcWindowRole, 'main'>;
export type IpcChannel = ValidInvokeChannel | ValidSendChannel;
export type IpcSenderEvent = IpcMainEvent | IpcMainInvokeEvent;
export type IpcSenderIdentity = Readonly<{ id: number }>;

type SecurityEventRecorder = (
  name: string,
  metadata: Readonly<Record<string, string | number | boolean | null>>,
  level: 'info' | 'warn'
) => void;

const MAIN_AND_OVERLAY_CHANNELS = new Set<IpcChannel>([
  'actionFile:open',
  // The Overlay binds read receipts to the same subject already broadcast to all windows.
  'auth:getState',
  // Read-only: the overlay's permission control opens on the Settings default
  // before a conversation exists. The matching setter stays main-only.
  'approval:getWorkspaceEditCommandPreference',
  // Read-only: the overlay composer lists workspace projects to reference.
  // Every workspaceSettings write stays main-only.
  'workspaceSettings:get',
  'action:readConversationPage',
  'action:readToolOutputPage',
  'ui:getLanguage',
  'ws:send',
  'ws:acceptAction',
  'ws:getStatus',
]);

const OVERLAY_ONLY_CHANNELS = new Set<IpcChannel>([
  'history:markCompletionViewed',
  'action:submitMessage',
  'action:resume',
  'action:attachImage',
  'action:attachFile',
  'action:discardAttachment',
  'actionImage:reveal',
  'overlay:submitApprovalDecision',
  'overlay:getActionApprovalMode',
  'overlay:setActionApprovalMode',
  'resize-notification-window',
  'notification-action-accept',
  'notification-action-reject',
  'notification-hide',
  'notification-stop-action',
  'overlay:recordInteraction',
  'overlay:dragStart',
  'overlay:dragMove',
  'overlay:dragEnd',
  'overlay:openWorkspaceSettings',
]);

export class IpcSenderRejectedError extends Error {
  readonly code: string;

  constructor(code: string) {
    super('IPC sender is not authorized.');
    this.name = 'IpcSenderRejectedError';
    this.code = code;
  }
}

function allowedRoles(channel: IpcChannel): ReadonlySet<IpcWindowRole> {
  if (OVERLAY_ONLY_CHANNELS.has(channel)) return new Set(['overlay']);
  if (MAIN_AND_OVERLAY_CHANNELS.has(channel)) return new Set(['main', 'overlay']);
  return new Set(['main']);
}

function classifyDocument(params: {
  frameUrl: string;
  isDev: boolean;
  frontendDevOrigin: string | null;
  frontendDistIndex: string;
}): IpcWindowRole | null {
  let url: URL;
  try {
    url = new URL(params.frameUrl);
  } catch {
    return null;
  }

  const notificationFilename = 'notification.html';
  if (params.isDev) {
    if (!params.frontendDevOrigin) return null;
    if (url.origin !== params.frontendDevOrigin) return null;
    if (url.pathname === '/' || url.pathname === '/index.html') return 'main';
    if (url.pathname !== `/${notificationFilename}`) return null;
  } else {
    if (url.protocol !== 'file:') return null;
    let documentPath: string;
    try {
      documentPath = path.resolve(fileURLToPath(url));
    } catch {
      return null;
    }
    if (documentPath === path.resolve(params.frontendDistIndex)) return 'main';
    if (documentPath !== path.join(path.dirname(params.frontendDistIndex), notificationFilename)) {
      return null;
    }
  }

  return 'overlay';
}

export function createIpcSenderSecurity(params: {
  getMainWindow: () => BrowserWindow | null;
  isDevRuntime: () => boolean;
  frontendDevOrigin: string | null;
  frontendDistIndex: string;
  recordSecurityEvent: SecurityEventRecorder;
}): {
  authorize: (channel: IpcChannel, event: IpcSenderEvent) => IpcWindowRole;
  auditMutation: (operation: string, outcome: 'succeeded' | 'failed') => void;
  registerWindow: (role: AuxiliaryIpcWindowRole, sender: IpcSenderIdentity) => void;
  unregisterWindow: (sender: IpcSenderIdentity) => void;
} {
  const auxiliaryRoleBySender = new Map<IpcSenderIdentity, AuxiliaryIpcWindowRole>();
  const reject = (channel: IpcChannel, event: IpcSenderEvent, code: string): never => {
    params.recordSecurityEvent(
      'IPC_SENDER_REJECTED',
      {
        channel,
        reason: code,
        sender_web_contents_id: typeof event.sender?.id === 'number' ? event.sender.id : null,
      },
      'warn'
    );
    throw new IpcSenderRejectedError(code);
  };

  const authorize = (channel: IpcChannel, event: IpcSenderEvent): IpcWindowRole => {
    const sender = event.sender as WebContents | undefined;
    const senderFrame = event.senderFrame;
    if (!sender || !senderFrame || senderFrame !== sender.mainFrame) {
      return reject(channel, event, 'top_frame_required');
    }

    const documentRole = classifyDocument({
      frameUrl: senderFrame.url,
      isDev: params.isDevRuntime(),
      frontendDevOrigin: params.frontendDevOrigin,
      frontendDistIndex: params.frontendDistIndex,
    });
    if (!documentRole) return reject(channel, event, 'untrusted_document');

    const mainWindow = params.getMainWindow();
    const isMainSender = Boolean(
      mainWindow && !mainWindow.isDestroyed() && mainWindow.webContents === sender
    );
    const registeredRole = isMainSender ? 'main' : auxiliaryRoleBySender.get(sender);
    if (!registeredRole || registeredRole !== documentRole) {
      return reject(channel, event, 'window_role_mismatch');
    }
    if (!allowedRoles(channel).has(registeredRole)) {
      return reject(channel, event, 'channel_not_allowed_for_window');
    }
    return registeredRole;
  };

  return {
    authorize,
    auditMutation: (operation, outcome) => {
      params.recordSecurityEvent('IPC_MUTATION', { operation, outcome }, 'info');
    },
    registerWindow: (role, sender) => {
      if (!Number.isInteger(sender.id)) {
        throw new TypeError('IPC window sender must have an integer WebContents id.');
      }
      auxiliaryRoleBySender.set(sender, role);
    },
    unregisterWindow: (sender) => {
      auxiliaryRoleBySender.delete(sender);
    },
  };
}
