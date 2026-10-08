import type { MainContext } from '../context';
import { IpcSenderRejectedError, type IpcSenderIdentity } from '../senderTrust';

/**
 * The signed-in user an attachment is written for, once the sender is a composer: the main
 * window (its chat) or an Overlay with a conversation registered. Sender trust has already
 * admitted the window's role; an Overlay must also still be registered as a conversation.
 */
export function requireComposerUser(
  ctx: Pick<MainContext, 'actions' | 'windows'>,
  sender: IpcSenderIdentity
): string {
  const mainWindow = ctx.windows.getMainWindow();
  const isMainWindow =
    mainWindow !== null && !mainWindow.isDestroyed() && mainWindow.webContents === sender;
  if (!isMainWindow && ctx.actions.resolveOverlayIdForSender(sender) === null) {
    throw new IpcSenderRejectedError('overlay_window_not_registered');
  }
  const userId = ctx.actions.getCurrentSubjectId();
  if (userId === null) throw new Error('Missing authenticated user id.');
  return userId;
}
