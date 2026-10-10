import { useEffect, useRef, useState, type Dispatch, type SetStateAction } from 'react';

import type { TaskDraft, TaskDraftWork } from '../../../electron/src/ipc/schemas/taskDrafts';
import type { ComposerState } from './useOverlayComposerController';

type DraftActions = Pick<
  NonNullable<NonNullable<typeof window.electron>['actions']>,
  'openDraft' | 'updateDraft' | 'closeDraft' | 'onDraftChanged'
>;

// Typing is shared once it pauses this long; a composer that goes sends what is pending first.
const SHARE_DELAY_MS = 150;

const EMPTY_DRAFT: TaskDraft = { text: '', mentions: [], attachments: [], retry: null };

/**
 * The composer's draft as the task shares it. A send whose outcome this composer does not know,
 * because it is still out or failed without an answer, is kept as the exact request to retry:
 * the backend dedupes on its message_id, so a new message could run the same request twice.
 */
function draftOf(composer: ComposerState): TaskDraft {
  const { submission } = composer;
  const unresolved =
    submission !== null &&
    (submission.state === 'submitting' ||
      (submission.state === 'failed' && composer.failureKind === 'transport'));
  return {
    text: composer.draft,
    mentions: composer.mentions.map((mention) => ({ ...mention, paths: [...mention.paths] })),
    attachments: [...composer.attachments],
    retry: unresolved ? submission.request : null,
  };
}

// Equal drafts compare equal however their objects were built or carried over IPC.
function draftKey(draft: TaskDraft): string {
  return JSON.stringify([
    draft.text,
    draft.mentions.map((m) => [m.projectId, m.displayName, m.paths, m.start, m.end]),
    draft.attachments.map((a) =>
      a.kind === 'image' ? [a.storagePath] : [a.attachmentId, a.name, a.byteSize]
    ),
    draft.retry?.message.message_id ?? null,
  ]);
}

const EMPTY_KEY = draftKey(EMPTY_DRAFT);

// An unresolved send heard from another window holds this composer to its retry, as it would
// hold the window that sent it; once another window settled it, the retry goes.
function withDraft(composer: ComposerState, draft: TaskDraft): ComposerState {
  const next = {
    ...composer,
    draft: draft.text,
    mentions: draft.mentions,
    attachments: draft.attachments,
  };
  if (draft.retry) {
    return {
      ...next,
      submission: { request: draft.retry, state: 'failed' },
      submissionStartFence: {
        messageId: draft.retry.message.message_id,
        sequence: 0,
        processId: null,
      },
      failureKind: 'transport',
      refreshState: 'idle',
    };
  }
  const offeredRetry =
    composer.submission?.state === 'failed' && composer.failureKind === 'transport';
  return offeredRetry ? { ...next, submission: null, failureKind: null } : next;
}

/**
 * Keeps this composer's unsent words, mentions and attachments in the task's one draft in main,
 * which every window showing the task shares: what is typed here appears there, and the reverse.
 * A send in flight shares nothing, so no other window offers it as unsent; once it has gone
 * through, the composer reads what another window wrote meanwhile. A composer that goes while
 * its send is out leaves that send as the task's retry, which main settles when it ends. A
 * change heard from another window is not sent back, and a read answered after the composer
 * moved to another task or was typed in again is dropped.
 */
export function useSharedTaskDraft(
  actions: DraftActions | undefined,
  work: TaskDraftWork | null,
  composer: ComposerState,
  setComposer: Dispatch<SetStateAction<ComposerState>>
): void {
  const sending = composer.submission !== null && composer.submission.state !== 'failed';
  const shared = sending ? EMPTY_DRAFT : draftOf(composer);
  const sharedKey = draftKey(shared);
  // The work whose draft has been read: until then nothing is shared, so it is not overwritten.
  const [readWork, setReadWork] = useState<TaskDraftWork | null>(null);
  const sync = useRef({
    work: null as TaskDraftWork | null,
    // The draft main holds as far as this composer knows: what it last sent or heard.
    known: EMPTY_KEY,
    // Bumped by every switch of task and every change to the draft; a read started before a
    // bump answers for a draft that is no longer the one shown.
    reads: 0,
    shownKey: sharedKey,
    sending,
    composer,
    unmounted: false,
    pending: null as { work: TaskDraftWork; draft: TaskDraft; key: string; timer: number } | null,
  });

  // Declared first, so it runs before the cleanup below when the composer goes.
  useEffect(() => {
    const state = sync.current;
    return () => {
      state.unmounted = true;
    };
  }, []);

  useEffect(() => {
    if (!actions || work === null) return;
    const state = sync.current;
    state.work = work;
    const read = ++state.reads;
    const hear = (draft: TaskDraft) => {
      // The latest writer wins: a change heard here replaces what was about to be shared.
      if (state.pending) window.clearTimeout(state.pending.timer);
      state.pending = null;
      state.known = draftKey(draft);
      setComposer((current) => withDraft(current, draft));
    };
    const off = actions.onDraftChanged((change) => {
      if (change.work === work && !state.sending) hear(change.draft ?? EMPTY_DRAFT);
    });
    actions
      .openDraft({ work })
      .then((draft) => {
        if (state.work !== work) return;
        state.known = draftKey(draft ?? EMPTY_DRAFT);
        // Words typed before the read arrived are newer, and are shared instead.
        if (draft && state.reads === read && !state.sending) hear(draft);
        setReadWork(work);
      })
      .catch((error: unknown) => console.error('Failed to open the shared task draft:', error));
    return () => {
      off();
      state.work = null;
      state.reads += 1;
      const pending = state.pending;
      if (pending) window.clearTimeout(pending.timer);
      state.pending = null;
      // Gone with a send out: it stays the task's retry until main hears how it ended. A switch of
      // task is not this: a send that went through moves its composer to the Action it opened.
      const leaving =
        state.unmounted && state.composer.submission?.state === 'submitting'
          ? draftOf(state.composer)
          : pending?.draft;
      if (leaving) void actions.updateDraft({ work, draft: leaving }).catch(() => {});
      state.known = EMPTY_KEY;
      void actions.closeDraft({ work }).catch(() => {});
      setReadWork(null);
    };
  }, [actions, work, setComposer]);

  useEffect(() => {
    const state = sync.current;
    if (sharedKey !== state.shownKey) {
      state.shownKey = sharedKey;
      state.reads += 1;
    }
    const sent = state.sending && !sending && composer.submission === null;
    state.sending = sending;
    state.composer = composer;
    if (!actions || work === null || readWork !== work) return;
    if (sent) {
      // The send went through: show the draft as another window may have written it meanwhile.
      const read = state.reads;
      void actions
        .openDraft({ work })
        .then((draft) => {
          if (state.work !== work || state.reads !== read) return;
          state.known = draftKey(draft ?? EMPTY_DRAFT);
          if (draft) setComposer((current) => withDraft(current, draft));
        })
        .catch((error: unknown) => console.error('Failed to read the shared task draft:', error));
      return;
    }
    if (sharedKey === state.known) {
      // Back to what main already holds, as an undo does: nothing is left to share.
      if (state.pending) window.clearTimeout(state.pending.timer);
      state.pending = null;
      return;
    }
    if (state.pending?.key === sharedKey) return;
    if (state.pending) window.clearTimeout(state.pending.timer);
    const draft = shared;
    const timer = window.setTimeout(
      () => {
        state.pending = null;
        state.known = sharedKey;
        void actions.updateDraft({ work, draft }).catch((error: unknown) => {
          console.error('Failed to share the task draft:', error);
        });
      },
      // A send's emptying is shared at once, so no other window offers its words meanwhile.
      sending ? 0 : SHARE_DELAY_MS
    );
    state.pending = { work, draft, key: sharedKey, timer };
  });
}
