/**
 * Shared task drafts (`action:openDraft`, `action:updateDraft`, `action:closeDraft`): every
 * composer that shows a task, in the main window or a small window, reads and writes one draft
 * in main, and hears the others' changes on `action:draftChanged`.
 */

import type { WebContents } from 'electron';

import type { MainContext } from '../context';
import type { IpcRegistrar } from '../registrar';
import { requireComposerUser } from './composerSender';
import { parseInput } from '../schemas/error';
import { TaskDraftOpenRequestSchema, TaskDraftUpdateRequestSchema } from '../schemas/taskDrafts';

export function registerTaskDraftHandlers(ctx: MainContext, registrar: IpcRegistrar): void {
  const watched = new Set<number>();
  const watch = (sender: WebContents) => {
    if (watched.has(sender.id)) return;
    watched.add(sender.id);
    sender.once('destroyed', () => {
      watched.delete(sender.id);
      ctx.taskDrafts.closeAll(sender.id);
    });
  };

  registrar.handle('action:openDraft', (event, request) => {
    const owner = requireComposerUser(ctx, event.sender);
    const { work } = parseInput(TaskDraftOpenRequestSchema, 'action:openDraft', request);
    const sender = event.sender;
    watch(sender);
    return ctx.taskDrafts.open(owner, work, {
      id: sender.id,
      send: (channel, change) => {
        if (!sender.isDestroyed()) sender.send(channel, change);
      },
    });
  });

  registrar.handle('action:updateDraft', (event, request) => {
    const owner = requireComposerUser(ctx, event.sender);
    const { work, draft } = parseInput(TaskDraftUpdateRequestSchema, 'action:updateDraft', request);
    ctx.taskDrafts.update(owner, work, draft, event.sender.id);
  });

  registrar.handle('action:closeDraft', (event, request) => {
    const owner = requireComposerUser(ctx, event.sender);
    const { work } = parseInput(TaskDraftOpenRequestSchema, 'action:closeDraft', request);
    ctx.taskDrafts.close(owner, work, event.sender.id);
  });
}
