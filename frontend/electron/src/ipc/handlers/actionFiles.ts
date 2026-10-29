import fs from 'fs';
import path from 'path';

import { documentHtmlFormat, readDocumentAsHtml } from '../../actions/actionDocumentHtml';
import {
  openActionFileInApp,
  openActionFileWithApp,
  readActionFile,
  resolveTaskFile,
  type ActionFileReadResult,
  type TaskFileRequest,
} from '../../actions/actionFileAccess';
import { buildActionFileUrl } from '../../protocol/actionFileUrl';
import type { MainContext } from '../context';
import type { IpcRegistrar } from '../registrar';
import { parseInput } from '../schemas/error';
import { ActionFileOpenInputSchema, TaskFileRequestInputSchema } from '../schemas/actionFiles';

const NOT_FOUND = { kind: 'unavailable', reason: 'not_found' } as const;
const HTML_EXTENSIONS = new Set(['.html', '.htm']);

function isRegularFile(realPath: string): boolean {
  try {
    return fs.statSync(realPath).isFile();
  } catch {
    return false;
  }
}

/** An HTML file as the PDF printed from it, at the URL the preview's frame loads it from. */
async function readHtmlAsPdf(
  ctx: MainContext,
  request: TaskFileRequest,
  realPath: string
): Promise<ActionFileReadResult> {
  const rendered = await ctx.actionFiles.renderHtml(realPath);
  return rendered.kind === 'rendered'
    ? { kind: 'pdf', url: buildActionFileUrl(request.target, request.path, rendered.renderId) }
    : { kind: 'unavailable', reason: 'render_failed' };
}

export function registerActionFileHandlers(ctx: MainContext, registrar: IpcRegistrar): void {
  // Every use goes through the real path this returns, never the requested one.
  const resolve = (request: TaskFileRequest) =>
    resolveTaskFile(
      {
        readConversationPage: ctx.actions.readConversationPage,
        readSuggestionBootstrap: ctx.overlay.resolveOverlayBootstrap,
        getCurrentSubjectId: ctx.actions.getCurrentSubjectId,
      },
      request
    );

  registrar.handle('actionFile:open', async (_event, params) => {
    const parsed = parseInput(ActionFileOpenInputSchema, 'actionFile:open', params);
    ctx.actionFiles.open(parsed);
  });
  registrar.handle('actionFile:read', async (_event, params) => {
    const parsed = parseInput(TaskFileRequestInputSchema, 'actionFile:read', params);
    const realPath = await resolve(parsed);
    if (realPath === null) return NOT_FOUND;
    // By the real path, as the Action's HTML tools: a link named deck.html to a text file is text.
    if (HTML_EXTENSIONS.has(path.extname(realPath).toLowerCase())) {
      return await readHtmlAsPdf(ctx, parsed, realPath);
    }
    const format = documentHtmlFormat(realPath);
    return format === null
      ? readActionFile(realPath)
      : await readDocumentAsHtml(realPath, format, ctx.actionFiles.runFile);
  });
  registrar.handle('actionFile:openInApp', async (_event, params) => {
    const parsed = parseInput(TaskFileRequestInputSchema, 'actionFile:openInApp', params);
    const realPath = await resolve(parsed);
    if (realPath === null) return NOT_FOUND;
    return await openActionFileInApp(realPath, ctx.actionFiles.openInApp);
  });
  registrar.handle('actionFile:openWithApp', async (_event, params) => {
    const parsed = parseInput(TaskFileRequestInputSchema, 'actionFile:openWithApp', params);
    const subjectId = ctx.actions.getCurrentSubjectId();
    const realPath = await resolve(parsed);
    if (realPath === null) return NOT_FOUND;
    // The picker waits on the user; an owner who changed meanwhile opens nothing.
    const chooseApp = async () => {
      const appPath = await ctx.actionFiles.chooseApp();
      return ctx.actions.getCurrentSubjectId() === subjectId ? appPath : null;
    };
    return await openActionFileWithApp(realPath, chooseApp, ctx.actionFiles.runFile);
  });
  registrar.handle('actionFile:reveal', async (_event, params) => {
    const parsed = parseInput(TaskFileRequestInputSchema, 'actionFile:reveal', params);
    const realPath = await resolve(parsed);
    if (realPath === null) return NOT_FOUND;
    // Finder selects the file; it never opens or runs it.
    ctx.actionFiles.open({ path: realPath });
    return { kind: 'revealed' } as const;
  });
  registrar.handle('actionFile:quickLook', async (_event, params) => {
    const parsed = parseInput(TaskFileRequestInputSchema, 'actionFile:quickLook', params);
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
