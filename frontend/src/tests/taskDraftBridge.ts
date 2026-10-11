import {
  answeredSendOutcome,
  createTaskDraftStore,
  sendWork,
} from '../../electron/src/actions/taskDraftStore';
import type { TaskDraftChange } from '../../electron/src/actions/taskDraftStore';
import type {
  ActionMessageRequest,
  ActionMessageSubmitResult,
} from '../../electron/src/actions/actionContracts';

type Submit = (request: ActionMessageRequest) => Promise<ActionMessageSubmitResult>;

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
    /** `submit` as main's `action:submitMessage` runs it: it clears the draft, then settles it. */
    sending(submit: Submit): Submit {
      return async (request) => {
        const settle = (outcome: 'sent' | 'refused' | 'unknown') =>
          store.settleSend(owner, sendWork(request), structuredClone(request), outcome);
        store.clearForSend(owner, sendWork(request));
        try {
          const result = await submit(request);
          settle(answeredSendOutcome(result));
          return result;
        } catch (error) {
          settle('unknown');
          throw error;
        }
      };
    },
    window() {
      const id = nextWindowId++;
      const listeners = new Set<(change: TaskDraftChange) => void>();
      const subscriber = {
        id,
        send: (_channel: string, change: TaskDraftChange) =>
          listeners.forEach((listener) => listener(structuredClone(change))),
      };
      type Work = TaskDraftChange['work'];
      return {
        openDraft: async ({ work }: { work: Work }) =>
          structuredClone(store.open(owner, work, subscriber)),
        updateDraft: async (request: {
          work: Work;
          draft: NonNullable<TaskDraftChange['draft']>;
        }) => store.update(owner, request.work, structuredClone(request.draft), id),
        closeDraft: async ({ work }: { work: Work }) => store.close(owner, work, id),
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
