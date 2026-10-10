import { useEffect, useRef, useState, type Dispatch, type SetStateAction } from 'react';

import type { TaskDraft, TaskDraftWork } from '../../../electron/src/ipc/schemas/taskDrafts';
import type { ComposerState } from './useOverlayComposerController';

type DraftActions = Pick<
  NonNullable<NonNullable<typeof window.electron>['actions']>,
  'openDraft' | 'updateDraft' | 'closeDraft' | 'onDraftChanged'
>;

// Typing is shared once it pauses this long; a composer that goes sends what is pending first.
const SHARE_DELAY_MS = 150;

const EMPTY_DRAFT: TaskDraft = { text: '', mentions: [], attachments: [] };

function draftOf(composer: ComposerState): TaskDraft {
  return {
    text: composer.draft,
    mentions: composer.mentions.map((mention) => ({ ...mention, paths: [...mention.paths] })),
    attachments: [...composer.attachments],
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
  ]);
}

const EMPTY_KEY = draftKey(EMPTY_DRAFT);

const withDraft = (composer: ComposerState, draft: TaskDraft): ComposerState => ({
  ...composer,
  draft: draft.text,
  mentions: draft.mentions,
  attachments: draft.attachments,
});

/**
 * Keeps this composer's unsent words, mentions and attachments in the task's one draft in main,
 * which every window showing the task shares: what is typed here appears there, and the reverse.
 * A send in flight shares nothing, so no other window offers it as unsent; once it has gone
 * through, the composer reads what another window wrote meanwhile, and if it failed, its words
 * are the draft again. A change heard from another window is not sent back.
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
    // The draft main holds as far as this composer knows: what it last sent or heard.
    known: EMPTY_KEY,
    sending,
    composer,
    pending: null as { work: TaskDraftWork; draft: TaskDraft; key: string; timer: number } | null,
  });

  useEffect(() => {
    if (!actions || work === null) return;
    const state = sync.current;
    let open = true;
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
        if (!open) return;
        state.known = draftKey(draft ?? EMPTY_DRAFT);
        // Words typed before the read arrived are newer, and are shared instead.
        if (draft && !state.sending && draftKey(draftOf(state.composer)) === EMPTY_KEY) {
          hear(draft);
        }
        setReadWork(work);
      })
      .catch((error: unknown) => console.error('Failed to open the shared task draft:', error));
    return () => {
      open = false;
      off();
      const pending = state.pending;
      if (pending) {
        window.clearTimeout(pending.timer);
        void actions.updateDraft({ work: pending.work, draft: pending.draft }).catch(() => {});
      }
      state.pending = null;
      state.known = EMPTY_KEY;
      void actions.closeDraft({ work }).catch(() => {});
      setReadWork(null);
    };
  }, [actions, work, setComposer]);

  useEffect(() => {
    const state = sync.current;
    const sent = state.sending && !sending && composer.submission === null;
    state.sending = sending;
    state.composer = composer;
    if (!actions || work === null || readWork !== work) return;
    if (sent) {
      // The send went through: show the draft as another window may have written it meanwhile.
      void actions
        .openDraft({ work })
        .then((draft) => {
          state.known = draftKey(draft ?? EMPTY_DRAFT);
          if (draft) setComposer((current) => withDraft(current, draft));
        })
        .catch((error: unknown) => console.error('Failed to read the shared task draft:', error));
      return;
    }
    if (sharedKey === state.known || state.pending?.key === sharedKey) return;
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
