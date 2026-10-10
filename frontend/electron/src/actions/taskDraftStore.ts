import type { TaskDraft, TaskDraftWork } from '../ipc/schemas/taskDrafts';

/** What a composer hears: the task's draft as another window left it, or null once cleared. */
export type TaskDraftChange = { work: TaskDraftWork; draft: TaskDraft | null };

/** A window whose composer shows a task; `id` is its webContents id. */
export type TaskDraftSubscriber = {
  readonly id: number;
  send: (channel: 'action:draftChanged', change: TaskDraftChange) => void;
};

export type TaskDraftStore = ReturnType<typeof createTaskDraftStore>;

const fileIds = (draft: TaskDraft): string[] =>
  draft.attachments.flatMap((attachment) =>
    attachment.kind === 'file' ? [attachment.attachmentId] : []
  );

/**
 * The unsent draft of each task, shared by every window whose composer shows it: the main
 * window's task pane and the small windows. The writer wins, and each change reaches the other
 * windows of that task, never its sender. Private content: held in memory only, never logged.
 *
 * A staged file is discarded only when a composer removes it and no draft lists it any more, so
 * a file another window still shows, or that a send is about to take, is never deleted. A draft
 * cleared by a send drops its files without discarding them: the backend moves them.
 */
export function createTaskDraftStore(params: {
  discardStagedFile: (ownerId: string, attachmentId: string) => void;
}) {
  let ownerId: string | null = null;
  const drafts = new Map<TaskDraftWork, TaskDraft>();
  const subscribers = new Map<TaskDraftWork, Map<number, TaskDraftSubscriber>>();
  // Files a composer removed: kept out of every draft from then on, and discarded once unlisted.
  const removed = new Set<string>();
  const awaitingDiscard = new Set<string>();

  const isReferenced = (attachmentId: string) =>
    [...drafts.values()].some((draft) => fileIds(draft).includes(attachmentId));

  // Drafts belong to one owner: switching owners discards the previous owner's unsent files.
  const useOwner = (next: string) => {
    if (ownerId === next) return;
    if (ownerId !== null) {
      for (const draft of drafts.values()) {
        for (const attachmentId of fileIds(draft)) params.discardStagedFile(ownerId, attachmentId);
      }
    }
    ownerId = next;
    drafts.clear();
    subscribers.clear();
    removed.clear();
    awaitingDiscard.clear();
  };

  return {
    /** Subscribes a window's composer to the task and returns its current draft. */
    open(owner: string, work: TaskDraftWork, subscriber: TaskDraftSubscriber): TaskDraft | null {
      useOwner(owner);
      const forWork = subscribers.get(work) ?? new Map<number, TaskDraftSubscriber>();
      forWork.set(subscriber.id, subscriber);
      subscribers.set(work, forWork);
      return drafts.get(work) ?? null;
    },

    close(owner: string, work: TaskDraftWork, subscriberId: number): void {
      if (owner === ownerId) subscribers.get(work)?.delete(subscriberId);
    },

    /** The window is gone: none of its composers hears anything more. */
    closeAll(subscriberId: number): void {
      for (const forWork of subscribers.values()) forWork.delete(subscriberId);
    },

    /** The composer's draft as it is now; an empty one clears the task's draft. */
    update(owner: string, work: TaskDraftWork, draft: TaskDraft, senderId: number): void {
      useOwner(owner);
      // A removed file stays removed, even in a draft from a window that had not heard yet.
      const attachments = draft.attachments.filter(
        (attachment) => attachment.kind !== 'file' || !removed.has(attachment.attachmentId)
      );
      const corrected = attachments.length !== draft.attachments.length;
      if (draft.text === '' && attachments.length === 0) drafts.delete(work);
      else drafts.set(work, { ...draft, attachments });
      for (const attachmentId of [...awaitingDiscard]) {
        if (isReferenced(attachmentId)) continue;
        awaitingDiscard.delete(attachmentId);
        params.discardStagedFile(owner, attachmentId);
      }
      const change = { work, draft: drafts.get(work) ?? null };
      for (const subscriber of subscribers.get(work)?.values() ?? []) {
        if (subscriber.id !== senderId || corrected) subscriber.send('action:draftChanged', change);
      }
    },

    /** A composer removed a staged file: it goes once no draft lists it. */
    discard(owner: string, attachmentId: string): void {
      useOwner(owner);
      removed.add(attachmentId);
      if (isReferenced(attachmentId)) awaitingDiscard.add(attachmentId);
      else params.discardStagedFile(owner, attachmentId);
    },
  };
}
