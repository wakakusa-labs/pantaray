/**
 * Overlay / Notification IPC handlers
 *
 * Validates the approval-decision payload at the IPC boundary. The
 * notification window factory is wired up via DI as before.
 */

import type { MainContext } from '../context';
import type { CreateNotificationIpcHandlers } from '../../orchestration/contracts';
import type { IpcRegistrar } from '../registrar';
import { parseInput } from '../schemas/error';
import {
  ActionApprovalModeUpdateSchema,
  SubmitApprovalDecisionPayloadSchema,
} from '../schemas/overlay';
import { IdSchema } from '../schemas/workspaceSettings';

function loadNotificationModule(): {
  createNotificationIpcHandlers?: CreateNotificationIpcHandlers;
} {
  // notification_window is CommonJS; load via require.
  // eslint-disable-next-line @typescript-eslint/no-require-imports
  return require('../../../notification_window.js');
}

export function registerOverlayHandlers(ctx: MainContext, registrar: IpcRegistrar): void {
  registrar.handle('overlay:submitApprovalDecision', async (_event, payload) => {
    const parsed = parseInput(
      SubmitApprovalDecisionPayloadSchema,
      'overlay:submitApprovalDecision',
      payload
    );
    return await ctx.overlay.submitApprovalDecision(parsed);
  });

  registrar.handle('overlay:getActionApprovalMode', async (_event, actionId) => {
    const parsed = parseInput(IdSchema, 'overlay:getActionApprovalMode', actionId);
    return await ctx.overlay.getActionApprovalMode(parsed);
  });

  registrar.handle('overlay:setActionApprovalMode', async (_event, payload) => {
    const parsed = parseInput(
      ActionApprovalModeUpdateSchema,
      'overlay:setActionApprovalMode',
      payload
    );
    return await ctx.overlay.setActionApprovalMode(parsed.actionId, parsed.approvalMode);
  });

  registrar.on('overlay:openWorkspaceSettings', () => {
    ctx.windows.showMainRoute('/workspace');
  });

  const injectedFactory = ctx.overlay.createNotificationIpcHandlers;
  const factory =
    typeof injectedFactory === 'function'
      ? injectedFactory
      : loadNotificationModule().createNotificationIpcHandlers;
  if (typeof factory !== 'function') throw new Error('createNotificationIpcHandlers is missing');

  const handlers = factory({
    resumeLiveProcess: ctx.overlay.resumeLiveProcess,
    resolveOverlayBootstrap: ctx.overlay.resolveOverlayBootstrap,
    refreshActionConversation: ctx.actions.refreshActionConversation,
    getMainWindow: ctx.windows.getMainWindow,
  });

  registrar.on('resize-notification-window', handlers.onResizeNotificationWindow);
  registrar.on('notification-action-accept', handlers.onNotificationActionAccept);
  registrar.on('notification-action-reject', handlers.onNotificationActionReject);
  registrar.on('notification-hide', handlers.onNotificationHide);
  registrar.on('notification-stop-action', handlers.onNotificationStopAction);
  registrar.on('overlay:recordInteraction', handlers.onOverlayInteraction);
  registrar.on('overlay:dragStart', handlers.onOverlayDragStart);
  registrar.on('overlay:dragMove', handlers.onOverlayDragMove);
  registrar.on('overlay:dragEnd', handlers.onOverlayDragEnd);
  registrar.on('history:openOverlay', handlers.onHistoryOpenOverlay);
}
