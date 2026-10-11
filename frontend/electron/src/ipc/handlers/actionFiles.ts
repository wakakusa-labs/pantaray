import fs from 'fs';

import { documentHtmlFormat, readDocumentAsHtml } from '../../actions/actionDocumentHtml';
import {
  openActionFileInApp,
  openActionFileWithApp,
  readActionFile,
  resolveActionFile,
  type ActionFileRequest,
} from '../../actions/actionFileAccess';
import type { MainContext } from '../context';
import type { IpcRegistrar } from '../registrar';
import { parseInput } from '../schemas/error';
import { ActionFileOpenInputSchema, ActionFileRequestInputSchema } from '../schemas/actionFiles';

const NOT_FOUND = { kind: 'unavailable', reason: 'not_found' } as const;

function isRegularFile(realPath: string): boolean {
  try {
    return fs.statSync(realPath).isFile();
  } catch {
    return false;
  }
}

export function registerActionFileHandlers(ctx: MainContext, registrar: IpcRegistrar): void {
  // Every use goes through the real path this returns, never the requested one.
  const resolve = (request: ActionFileRequest) =>
    resolveActionFile(ctx.actions.readConversationPage, request);

  registrar.handle('actionFile:open', async (_event, params) => {
    const parsed = parseInput(ActionFileOpenInputSchema, 'actionFile:open', params);
    ctx.actionFiles.open(parsed);
  });
  registrar.handle('actionFile:read', async (_event, params) => {
    const parsed = parseInput(ActionFileRequestInputSchema, 'actionFile:read', params);
    const realPath = await resolve(parsed);
    if (realPath === null) return NOT_FOUND;
    const format = documentHtmlFormat(realPath);
    return format === null
      ? readActionFile(realPath)
      : await readDocumentAsHtml(realPath, format, ctx.actionFiles.runFile);
  });
  registrar.handle('actionFile:openInApp', async (_event, params) => {
    const parsed = parseInput(ActionFileRequestInputSchema, 'actionFile:openInApp', params);
    const realPath = await resolve(parsed);
    if (realPath === null) return NOT_FOUND;
    return await openActionFileInApp(realPath, ctx.actionFiles.openInApp);
  });
  registrar.handle('actionFile:openWithApp', async (_event, params) => {
    const parsed = parseInput(ActionFileRequestInputSchema, 'actionFile:openWithApp', params);
    const realPath = await resolve(parsed);
    if (realPath === null) return NOT_FOUND;
    return await openActionFileWithApp(
      realPath,
      ctx.actionFiles.chooseApp,
      ctx.actionFiles.runFile
    );
  });
  registrar.handle('actionFile:reveal', async (_event, params) => {
    const parsed = parseInput(ActionFileRequestInputSchema, 'actionFile:reveal', params);
    const realPath = await resolve(parsed);
    if (realPath === null) return NOT_FOUND;
    // Finder selects the file; it never opens or runs it.
    ctx.actionFiles.open({ path: realPath });
    return { kind: 'revealed' } as const;
  });
  registrar.handle('actionFile:quickLook', async (_event, params) => {
    const parsed = parseInput(ActionFileRequestInputSchema, 'actionFile:quickLook', params);
    const realPath = await resolve(parsed);
    const mainWindow = ctx.windows.getMainWindow();
    // A regular file only: Quick Look of a FIFO or a device could stall the main process.
    if (realPath === null || !isRegularFile(realPath) || !mainWindow || mainWindow.isDestroyed()) {
      return NOT_FOUND;
    }
    // Quick Look draws the file itself and never runs it.
    mainWindow.previewFile(realPath);
    return { kind: 'shown' } as const;
  });
}
