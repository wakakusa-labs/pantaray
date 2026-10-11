import { z } from 'zod';

import type { MainContext } from '../context';
import type { IpcRegistrar } from '../registrar';
import { parseInput } from '../schemas/error';
import { IdSchema } from '../schemas/workspaceSettings';

const SuggestionReadRequestSchema = z.object({ suggestionId: IdSchema }).strict();

/**
 * The main window reads a suggestion into main's record, the one Overlay panels read too, and
 * from then on hears each change to it on `suggestion:snapshot`. No panel opens.
 */
export function registerSuggestionHandlers(ctx: MainContext, registrar: IpcRegistrar): void {
  registrar.handle('suggestion:read', async (_event, request) => {
    const { suggestionId } = parseInput(SuggestionReadRequestSchema, 'suggestion:read', request);
    const subjectId = ctx.actions.getCurrentSubjectId();
    if (subjectId === null) throw new Error('Missing authenticated user id.');
    const bootstrap = await ctx.overlay.resolveOverlayBootstrap(suggestionId);
    // An owner change cleared main's records; the previous owner's suggestion must not return.
    if (ctx.actions.getCurrentSubjectId() !== subjectId) throw new Error('Local owner changed.');
    if (bootstrap === null) throw new Error('Suggestion bootstrap is unavailable.');
    return ctx.overlay.adoptSuggestionSnapshot(bootstrap.snapshot);
  });
}
