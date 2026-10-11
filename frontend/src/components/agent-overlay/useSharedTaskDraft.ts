import { useEffect, useRef, useState, type Dispatch, type SetStateAction } from 'react';

import type { TaskDraft, TaskDraftWork } from '../../../electron/src/ipc/schemas/taskDrafts';
import type { ComposerState } from './useOverlayComposerController';

type DraftActions = Pick<
  NonNullable<NonNullable<typeof window.electron>['actions']>,
  'openDraft' | 'updateDraft' | 'closeDraft' | 'onDraftChanged'
>;

// Typing is shared once it pauses this long; a composer that goes sends what is pending first.
const SHARE_DELAY_MS = 150;

const EMPTY_DRAFT: TaskDraft = {
  text: '',
  mentions: [],
  quotes: [],
  attachments: [],
  retry: null,
};

/**
 * The composer's draft as the task holds it. A send that failed without an answer is the task's
 * retry, the exact request: the backend dedupes on its message_id, so a new message could run
 * the same request twice. Only main sets a retry, as it settles the send; a composer that holds
 * one only compares with it.
 */
function draftOf(composer: ComposerState): TaskDraft {
  return {
    text: composer.draft,
    mentions: composer.mentions.map((mention) => ({ ...mention, paths: [...mention.paths] })),
    quotes: composer.quotes.map((quote) => ({ ...quote })),
    attachments: [...composer.attachments],
    retry: holdsRetry(composer) && composer.submission ? composer.submission.request : null,
  };
}

// Equal drafts compare equal however their objects were built or carried over IPC.
function draftKey(draft: TaskDraft): string {
  return JSON.stringify([
    draft.text,
    draft.mentions.map((m) => [m.projectId, m.displayName, m.paths, m.start, m.end]),
    draft.quotes.map((q) => [q.id, q.text, q.comment]),
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
    quotes: draft.quotes,
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
  if (!holdsRetry(composer)) return next;
  // Settled elsewhere, or given way to another window's newer draft, which wins: this send's
  // retry is dropped, and the composer says the message may not have gone out.
  const newer = draftKey(draft) !== EMPTY_KEY;
  return { ...next, submission: null, failureKind: newer ? 'superseded' : null };
}

const holdsRetry = (composer: ComposerState) =>
  composer.submission?.state === 'failed' && composer.failureKind === 'transport';

/**
 * Keeps this composer's unsent words, quotes, mentions and attachments in the task's one draft in main,
 * which every window showing the task shares: what is typed here appears there, and the reverse.
 * Main clears the task's draft as a send takes its words, and settles it as the send ends,
 * keeping a newer draft from another window over them; the composer writes nothing meanwhile,
 * and then reads the settled draft. A change heard from
 * another window is not sent back, and a read answered after the composer moved to another
 * task or was typed in again is dropped.
 */
export function useSharedTaskDraft(
  actions: DraftActions | undefined,
  work: TaskDraftWork | null,
  composer: ComposerState,
  setComposer: Dispatch<SetStateAction<ComposerState>>
): void {
  const sending = composer.submission !== null && composer.submission.state !== 'failed';
  const shared = draftOf(composer);
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
    // A send has ended and its settled draft is being read: nothing is heard or shared meanwhile.
    settling: false,
    composer,
    pending: null as { work: TaskDraftWork; draft: TaskDraft; key: string; timer: number } | null,
  });

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
      if (change.work === work && !state.sending && !state.settling) {
        hear(change.draft ?? EMPTY_DRAFT);
      }
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
      if (pending) {
        window.clearTimeout(pending.timer);
        void actions.updateDraft({ work, draft: pending.draft }).catch(() => {});
      }
      state.pending = null;
      state.settling = false;
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
    const ended = state.sending && !sending;
    state.sending = sending;
    state.composer = composer;
    if (!actions || work === null || readWork !== work) return;
    if (sending || ended) {
      // The send took the words, and main cleared the draft as it did: nothing waits to be shared.
      if (state.pending) window.clearTimeout(state.pending.timer);
      state.pending = null;
    }
    if (sending) return;
    if (ended) {
      // Main settled the draft as the send ended: show it, with what another window wrote.
      state.settling = true;
      const read = state.reads;
      void actions
        .openDraft({ work })
        .then((draft) => {
          if (state.work !== work || state.reads !== read) return;
          state.known = draftKey(draft ?? EMPTY_DRAFT);
          setComposer((current) => withDraft(current, draft ?? EMPTY_DRAFT));
        })
        .catch((error: unknown) => console.error('Failed to read the shared task draft:', error))
        .finally(() => {
          if (state.work === work) state.settling = false;
        });
      return;
    }
    // Only main writes a retry; a composer holding one waits for it to be settled or retried.
    if (state.settling || holdsRetry(composer)) return;
    if (sharedKey === state.known) {
      // Back to what main already holds, as an undo does: nothing is left to share.
      if (state.pending) window.clearTimeout(state.pending.timer);
      state.pending = null;
      return;
    }
    if (state.pending?.key === sharedKey) return;
    if (state.pending) window.clearTimeout(state.pending.timer);
    const draft = shared;
    const timer = window.setTimeout(() => {
      state.pending = null;
      state.known = sharedKey;
      void actions.updateDraft({ work, draft }).catch((error: unknown) => {
        console.error('Failed to share the task draft:', error);
      });
    }, SHARE_DELAY_MS);
    state.pending = { work, draft, key: sharedKey, timer };
  });
}
