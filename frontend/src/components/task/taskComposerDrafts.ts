import { useEffect, useRef, type MutableRefObject } from 'react';

import type { WorkKey } from '@/components/chat/chatTimeline';
import { discardDocuments, type AttachmentActions } from '../agent-overlay/attachmentStaging';
import type { ComposerState } from '../agent-overlay/useOverlayComposerController';

/**
 * The composers of the task panes the user left, by work, so a draft, its staged attachments and
 * a send waiting for a retry are there again on return. One store belongs to one owner.
 */
export type TaskComposerDrafts = {
  read: (work: WorkKey) => ComposerState | undefined;
  forget: (work: WorkKey) => void;
  keep: (work: WorkKey, state: ComposerState) => void;
  /** The owner's pages are gone: drafts never sent are discarded, now and from late keeps. */
  close: () => void;
  open: () => void;
};

/**
 * What a left composer keeps: nothing when it holds nothing, and no round trip it can no longer
 * hear. A send still in flight is kept as failed, so its retry repeats the same message id.
 */
export function keptComposerState(state: ComposerState): ComposerState | null {
  const inFlight = state.submission?.state === 'submitting';
  const submission =
    state.submission && inFlight
      ? { ...state.submission, state: 'failed' as const }
      : state.submission;
  if (state.draft === '' && state.attachments.length === 0 && submission === null) return null;
  return {
    ...state,
    submission,
    failureKind: inFlight ? 'transport' : state.failureKind,
    attachmentsInFlight: 0,
    submissionStartFence: null,
    refreshState: 'idle',
    resume:
      state.resume?.state === 'requesting' ? { ...state.resume, state: 'failed' } : state.resume,
  };
}

// Attachments of a kept send went out with it; moving them is the backend's job.
const unsentAttachments = (state: ComposerState) =>
  state.submission === null ? state.attachments : [];

export function createTaskComposerDrafts(
  actions: AttachmentActions | undefined
): TaskComposerDrafts {
  const drafts = new Map<WorkKey, ComposerState>();
  let closed = false;
  return {
    read: (work) => drafts.get(work),
    forget: (work) => drafts.delete(work),
    keep: (work, state) => {
      const kept = keptComposerState(state);
      if (closed) {
        if (kept) discardDocuments(actions, unsentAttachments(kept));
        return;
      }
      if (kept) drafts.set(work, kept);
      else drafts.delete(work);
    },
    close: () => {
      closed = true;
      for (const state of drafts.values()) discardDocuments(actions, unsentAttachments(state));
      drafts.clear();
    },
    open: () => {
      closed = false;
    },
  };
}

/**
 * Hands a pane's composer to the store when the pane goes, and takes it back while the pane is
 * shown. `keepable` is false once the draft has gone out another way, such as with an approval.
 */
export function useKeptTaskComposer({
  drafts,
  work,
  composer,
  keepable,
  composerGenerationRef,
}: {
  drafts: TaskComposerDrafts;
  work: WorkKey;
  composer: ComposerState;
  keepable: boolean;
  composerGenerationRef: MutableRefObject<number>;
}): void {
  const latest = useRef({ composer, keepable });
  useEffect(() => {
    latest.current = { composer, keepable };
  });
  useEffect(() => {
    drafts.forget(work);
    return () => {
      // A write still running would land in a composer that is gone; it discards itself instead.
      composerGenerationRef.current += 1;
      if (latest.current.keepable) drafts.keep(work, latest.current.composer);
    };
  }, [drafts, work, composerGenerationRef]);
}
