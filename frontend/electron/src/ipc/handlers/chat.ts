/**
 * chat:* IPC handlers: the main window's single chat.
 *
 * Sender trust keeps every `chat:*` channel main-only: the Overlay talks to its Action, never
 * to the chat (design 8).
 */

import type { MainContext } from '../context';
import type { IpcRegistrar } from '../registrar';
import { ChatItemPageRequestSchema, ChatMessageRequestSchema } from '../../chat/chatContracts';
import { parseInput } from '../schemas/error';

export function registerChatHandlers(ctx: MainContext, registrar: IpcRegistrar): void {
  registrar.handle('chat:sendMessage', async (_event, request) =>
    ctx.chat.sendMessage(parseInput(ChatMessageRequestSchema, 'chat:sendMessage', request))
  );
  registrar.handle('chat:listItems', async (_event, request) =>
    ctx.chat.listItems(parseInput(ChatItemPageRequestSchema, 'chat:listItems', request))
  );
}
