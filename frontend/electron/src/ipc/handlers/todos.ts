/**
 * todos:* IPC handlers: the main window's TODO list and its progress.
 *
 * Sender trust keeps every `todos:*` channel main-only, as for the chat: an Overlay never
 * reads or writes the TODOs.
 */

import type { MainContext } from '../context';
import type { IpcRegistrar } from '../registrar';
import {
  ProgressRecordCreateRequestSchema,
  ProgressRecordRefRequestSchema,
  TodoCheckRequestSchema,
  TodoCreateRequestSchema,
  TodoDeletionAnswerRequestSchema,
  TodoDueRequestSchema,
  TodoGoalRequestSchema,
  TodoListQuerySchema,
  TodoProgressQuerySchema,
  TodoProjectRefRequestSchema,
  TodoRefRequestSchema,
  TodoScopeQuerySchema,
  TodoStartRequestSchema,
} from '../../todos/todoContracts';
import { parseInput } from '../schemas/error';

export function registerTodoHandlers(ctx: MainContext, registrar: IpcRegistrar): void {
  registrar.handle('todos:list', async (_event, query) =>
    ctx.todos.list(parseInput(TodoListQuerySchema, 'todos:list', query))
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
  registrar.handle('todos:setStart', async (_event, request) =>
    ctx.todos.setStart(parseInput(TodoStartRequestSchema, 'todos:setStart', request))
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
  registrar.handle('todos:progress', async (_event, query) =>
    ctx.todos.progress(parseInput(TodoProgressQuerySchema, 'todos:progress', query))
  );
  registrar.handle('todos:setGoal', async (_event, request) =>
    ctx.todos.setGoal(parseInput(TodoGoalRequestSchema, 'todos:setGoal', request))
  );
  registrar.handle('todos:clearGoal', async (_event, request) =>
    ctx.todos.clearGoal(parseInput(TodoProjectRefRequestSchema, 'todos:clearGoal', request))
  );
  // A user record: the renderer names it with `client_record_id`, so a resend adds nothing.
  registrar.handle('todos:addProgressRecord', async (_event, request) =>
    ctx.todos.addProgressRecord(
      parseInput(ProgressRecordCreateRequestSchema, 'todos:addProgressRecord', request)
    )
  );
  registrar.handle('todos:deleteProgressRecord', async (_event, request) =>
    ctx.todos.deleteProgressRecord(
      parseInput(ProgressRecordRefRequestSchema, 'todos:deleteProgressRecord', request)
    )
  );
}
