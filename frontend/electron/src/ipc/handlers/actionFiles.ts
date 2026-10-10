import {
  isActionFile,
  openActionFileInApp,
  readActionFile,
  type ActionFileRequest,
} from '../../actions/actionFileAccess';
import type { MainContext } from '../context';
import type { IpcRegistrar } from '../registrar';
import { parseInput } from '../schemas/error';
import { ActionFileOpenInputSchema, ActionFileRequestInputSchema } from '../schemas/actionFiles';

const NOT_FOUND = { kind: 'unavailable', reason: 'not_found' } as const;

export function registerActionFileHandlers(ctx: MainContext, registrar: IpcRegistrar): void {
  const named = (request: ActionFileRequest) =>
    isActionFile(ctx.actions.readConversationPage, request);

  registrar.handle('actionFile:open', async (_event, params) => {
    const parsed = parseInput(ActionFileOpenInputSchema, 'actionFile:open', params);
    ctx.actionFiles.open(parsed);
  });
  registrar.handle('actionFile:read', async (_event, params) => {
    const parsed = parseInput(ActionFileRequestInputSchema, 'actionFile:read', params);
    return (await named(parsed)) ? readActionFile(parsed.path) : NOT_FOUND;
  });
  registrar.handle('actionFile:openInApp', async (_event, params) => {
    const parsed = parseInput(ActionFileRequestInputSchema, 'actionFile:openInApp', params);
    if (!(await named(parsed))) return NOT_FOUND;
    return await openActionFileInApp(parsed.path, ctx.actionFiles.openInApp);
  });
}
