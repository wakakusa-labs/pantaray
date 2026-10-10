import { createTaskDraftStore } from '../../electron/src/actions/taskDraftStore';
import type { TaskDraftChange } from '../../electron/src/actions/taskDraftStore';

/**
 * Main's real task draft store behind fake IPC, for renderer tests: each `window()` is one
 * window's `actions` draft calls, and what crosses is cloned as IPC would.
 */
export function createTaskDraftBridge(owner = 'user-1') {
  const discarded: string[] = [];
  const store = createTaskDraftStore({
    discardStagedFile: (_owner, attachmentId) => discarded.push(attachmentId),
  });
  let nextWindowId = 1;
  return {
    store,
    discarded,
    window() {
      const id = nextWindowId++;
      const listeners = new Set<(change: TaskDraftChange) => void>();
      const subscriber = {
        id,
        send: (_channel: string, change: TaskDraftChange) =>
          listeners.forEach((listener) => listener(structuredClone(change))),
      };
      return {
        openDraft: async ({ work }: { work: TaskDraftChange['work'] }) =>
          structuredClone(store.open(owner, work, subscriber)),
        updateDraft: async ({
          work,
          draft,
        }: {
          work: TaskDraftChange['work'];
          draft: NonNullable<TaskDraftChange['draft']>;
        }) => store.update(owner, work, structuredClone(draft), id),
        closeDraft: async ({ work }: { work: TaskDraftChange['work'] }) =>
          store.close(owner, work, id),
        onDraftChanged: (listener: (change: TaskDraftChange) => void) => {
          listeners.add(listener);
          return () => {
            listeners.delete(listener);
          };
        },
        discardAttachment: async ({ attachmentId }: { attachmentId: string }) =>
          store.discard(owner, attachmentId),
      };
    },
  };
}
