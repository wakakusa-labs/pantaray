/**
 * `browserView:watch` and `browserView:unwatch`, main window only: the window names an Action,
 * and main streams the app's page that Action works on to that window alone, on
 * `browserView:update`. The window sends nothing to the page.
 */

import type { MainContext } from '../context';
import type { IpcRegistrar } from '../registrar';
import { parseInput } from '../schemas/error';
import { BrowserViewUnwatchRequestSchema, BrowserViewWatchRequestSchema } from '../schemas/actions';

export function registerBrowserViewHandlers(ctx: MainContext, registrar: IpcRegistrar): void {
  registrar.handle('browserView:watch', (event, request) => {
    const { actionId } = parseInput(BrowserViewWatchRequestSchema, 'browserView:watch', request);
    return ctx.browserView.watch(actionId, event.sender);
  });
  registrar.handle('browserView:unwatch', (event, request) => {
    const { watchId } = parseInput(BrowserViewUnwatchRequestSchema, 'browserView:unwatch', request);
    ctx.browserView.unwatch(watchId, event.sender.id);
  });
}
