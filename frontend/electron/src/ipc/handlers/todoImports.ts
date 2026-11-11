/**
 * todoImports:* IPC handlers: the main window's TODO import and its draft.
 *
 * Sender trust keeps every `todoImports:*` channel main-only, as for the TODO list.
 */

import type { MainContext } from '../context';
import type { IpcRegistrar } from '../registrar';
import {
  TodoImportRefRequestSchema,
  TodoImportRegisterRequestSchema,
  TodoImportStartRequestSchema,
  TodoImportWriteRequestSchema,
} from '../../todos/todoImportContracts';
import { parseInput } from '../schemas/error';

export function registerTodoImportHandlers(ctx: MainContext, registrar: IpcRegistrar): void {
  // The renderer names the import with `import_id`, so a resend starts nothing new.
  registrar.handle('todoImports:start', async (_event, request) =>
    ctx.todoImports.start(parseInput(TodoImportStartRequestSchema, 'todoImports:start', request))
  );
  registrar.handle('todoImports:read', async (_event, request) =>
    ctx.todoImports.read(parseInput(TodoImportRefRequestSchema, 'todoImports:read', request))
  );
  registrar.handle('todoImports:write', async (_event, request) =>
    ctx.todoImports.write(parseInput(TodoImportWriteRequestSchema, 'todoImports:write', request))
  );
  registrar.handle('todoImports:register', async (_event, request) =>
    ctx.todoImports.register(
      parseInput(TodoImportRegisterRequestSchema, 'todoImports:register', request)
    )
  );
  registrar.handle('todoImports:cancel', async (_event, request) =>
    ctx.todoImports.cancel(parseInput(TodoImportRefRequestSchema, 'todoImports:cancel', request))
  );
}
