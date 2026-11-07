/**
 * todos:* IPC handlers: the main window's TODO list.
 *
 * Sender trust keeps every `todos:*` channel main-only, as for the chat: an Overlay never
 * reads or writes the TODOs.
 */

import type { MainContext } from '../context';
import type { IpcRegistrar } from '../registrar';
import {
  TodoCheckRequestSchema,
  TodoCreateRequestSchema,
  TodoDeletionAnswerRequestSchema,
  TodoDueRequestSchema,
  TodoRefRequestSchema,
  TodoScopeQuerySchema,
  TodoWeekQuerySchema,
} from '../../todos/todoContracts';
import { parseInput } from '../schemas/error';

export function registerTodoHandlers(ctx: MainContext, registrar: IpcRegistrar): void {
  registrar.handle('todos:listWeek', async (_event, query) =>
    ctx.todos.listWeek(parseInput(TodoWeekQuerySchema, 'todos:listWeek', query))
  );
  registrar.handle('todos:create', async (_event, request) =>
    ctx.todos.create(parseInput(TodoCreateRequestSchema, 'todos:create', request))
  );
  registrar.handle('todos:setCheck', async (_event, request) =>
    ctx.todos.setCheck(parseInput(TodoCheckRequestSchema, 'todos:setCheck', request))
  );
  registrar.handle('todos:setDue', async (_event, request) =>
    ctx.todos.setDue(parseInput(TodoDueRequestSchema, 'todos:setDue', request))
  );
  registrar.handle('todos:delete', async (_event, request) =>
    ctx.todos.delete(parseInput(TodoRefRequestSchema, 'todos:delete', request))
  );
  registrar.handle('todos:answerDeletion', async (_event, request) =>
    ctx.todos.answerDeletion(
      parseInput(TodoDeletionAnswerRequestSchema, 'todos:answerDeletion', request)
    )
  );
  registrar.handle('todos:restore', async (_event, request) =>
    ctx.todos.restore(parseInput(TodoRefRequestSchema, 'todos:restore', request))
  );
  registrar.handle('todos:listDeleted', async (_event, query) =>
    ctx.todos.listDeleted(parseInput(TodoScopeQuerySchema, 'todos:listDeleted', query))
  );
}
