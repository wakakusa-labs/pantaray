/**
 * history:* IPC handlers
 *
 * 目的:
 * - main の local backend 経由で suggestion history を取得する。
 * - renderer へは「表示に必要なデータ」だけを返す（トークン等は返さない）。
 */

import type { MainContext } from '../context';
import type { IpcRegistrar } from '../registrar';
import { ActionCompletionViewedRequestSchema } from '../../history/actionReadState';
import { HistoryItemKeySchema } from '../../history/historyContracts';
import { ActionConversationOverlayRequestSchema } from '../schemas/actions';
import { parseInput } from '../schemas/error';

export function registerHistoryHandlers(ctx: MainContext, registrar: IpcRegistrar): void {
  registrar.handle('history:fetch', async (_event, params) => {
    try {
      return await ctx.history.fetch(params);
    } catch (e) {
      return { data: [], error: e instanceof Error ? e.message : String(e) };
    }
  });
  registrar.handle('history:markCompletionViewed', (_event, request) =>
    ctx.history.markCompletionViewed(
      parseInput(ActionCompletionViewedRequestSchema, 'history:markCompletionViewed', request)
    )
  );
  registrar.handle('history:deleteItem', (_event, request) =>
    ctx.history.deleteItem(parseInput(HistoryItemKeySchema, 'history:deleteItem', request))
  );
  registrar.handle('history:readItem', (_event, request) =>
    ctx.history.readItem(parseInput(HistoryItemKeySchema, 'history:readItem', request))
  );
  registrar.handle('history:openNewConversation', () => {
    ctx.windows.openNewConversationOverlay();
  });
  registrar.handle('history:openConversation', (_event, request) =>
    ctx.windows.openActionConversationOverlay(
      parseInput(ActionConversationOverlayRequestSchema, 'history:openConversation', request)
        .actionId
    )
  );
}
