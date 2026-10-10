import { useEffect, useRef, useState } from 'react';

import { useI18n } from '@/context/useI18n';
import type { OverlaySnapshot } from '../../../electron/src/orchestration/contracts';
import { isErrorEvent } from '../../../electron/src/orchestration/eventContracts';
import { isActionErrorMeta } from '../agent-overlay/model/overlayTypes';
import { useActionApprovalMode } from '../agent-overlay/useActionApprovalMode';
import { useOverlayComposerController } from '../agent-overlay/useOverlayComposerController';

/**
 * Why the last answer did not go through. `accept_failed` is the accept call itself failing (main
 * sent nothing, or put its record back); `preflight_rejected` and `start_failed` are the backend's
 * answer to `execute_action`; `dismiss_failed` is its error answer to `dismiss_suggestion`.
 */
export type SuggestionStartFailure =
  | { stage: 'accept_failed' }
  | { stage: 'dismiss_failed' }
  | { stage: 'preflight_rejected' | 'start_failed'; message: string | null };

export type SuggestionTaskPhase =
  | 'loading'
  | 'load_failed'
  /** The text is still arriving; whether it offers an Action is not known yet. */
  | 'streaming'
  | 'actionable'
  | 'starting'
  | 'dismissing'
  | 'start_failed'
  /** An Action exists for it; the Action pane takes over. */
  | 'started'
  | 'dismissed';

type Scoped<T> = { suggestionId: string; value: T };

export function deriveSuggestionTaskPhase({
  snapshot,
  loadFailed,
  pending,
  failure,
  replyActionId,
  replying,
}: {
  snapshot: OverlaySnapshot | null;
  loadFailed: boolean;
  pending: 'accept' | 'dismiss' | null;
  failure: SuggestionStartFailure | null;
  replyActionId: string | null;
  replying: boolean;
}): SuggestionTaskPhase {
  if (snapshot === null) return loadFailed ? 'load_failed' : 'loading';
  if (snapshot.reactionState === 'rejected') return 'dismissed';
  if (snapshot.actionId !== null || replyActionId !== null) return 'started';
  if (failure?.stage === 'start_failed') return 'start_failed';
  if (pending === 'dismiss') return 'dismissing';
  if (
    pending === 'accept' ||
    replying ||
    snapshot.reactionState === 'accepted' ||
    snapshot.actionPhase !== 'idle'
  ) {
    return 'starting';
  }
  return snapshot.interactionContract === null ? 'streaming' : 'actionable';
}

function scoped<T>(state: Scoped<T> | null, suggestionId: string): T | null {
  return state?.suggestionId === suggestionId ? state.value : null;
}

/**
 * One unanswered suggestion in the main window: main's record of it, and the ways to answer it.
 *
 * Every answer goes the way the Overlay sends it. Accepting is `ws:acceptAction` with no command id,
 * so main reuses the command it already holds for this suggestion; dismissing is
 * `dismiss_suggestion` on `ws:send`; replying to a message-only suggestion opens a new Action
 * with `reply_to_suggestion_id` through the Overlay's composer controller.
 */
export function useSuggestionTask(suggestionId: string) {
  const { language } = useI18n();
  const electron = window.electron;
  const suggestions = electron?.suggestions;
  const orchestration = electron?.orchestration;
  const actions = electron?.actions;
  if (electron && (!suggestions || !orchestration || !actions)) {
    throw new Error('Missing suggestion bridge.');
  }

  const [record, setRecord] = useState<Scoped<OverlaySnapshot> | null>(null);
  const [loadFailedFor, setLoadFailedFor] = useState<string | null>(null);
  const [pendingState, setPending] = useState<Scoped<'accept' | 'dismiss'> | null>(null);
  const [failureState, setFailure] = useState<Scoped<SuggestionStartFailure> | null>(null);
  const snapshot = scoped(record, suggestionId);
  const pending = scoped(pendingState, suggestionId);
  const failure = scoped(failureState, suggestionId);

  useEffect(() => {
    if (!suggestions) return;
    let active = true;
    // main sends each record change after the read's reply or before it, in order, so the latest
    // arrival is the newest record.
    const off = suggestions.onSnapshot(({ snapshot: next }) => {
      if (next.suggestionId === suggestionId) setRecord({ suggestionId, value: next });
    });
    suggestions
      .read({ suggestionId })
      .then((value) => {
        if (active) setRecord({ suggestionId, value });
      })
      .catch(() => {
        if (active) setLoadFailedFor(suggestionId);
      });
    return () => {
      active = false;
      off();
    };
  }, [suggestions, suggestionId]);

  // The suggestion this pane sent dismiss_suggestion for and has no answer to yet.
  const dismissingRef = useRef<string | null>(null);

  // main's record keeps neither why a start failed nor that a dismissal did; the backend's error
  // events say it.
  useEffect(() => {
    if (!orchestration?.onEvent) return;
    return orchestration.onEvent((event) => {
      if (!isErrorEvent(event)) return;
      // The backend answers a dismissal it could not read or save with a suggestion error.
      if (
        event.meta?.kind === 'suggestion' &&
        event.meta.suggestion_id === suggestionId &&
        dismissingRef.current === suggestionId
      ) {
        dismissingRef.current = null;
        setPending(null);
        setFailure({ suggestionId, value: { stage: 'dismiss_failed' } });
        return;
      }
      if (!isActionErrorMeta(event.meta)) return;
      const { stage, suggestion_id } = event.meta;
      if (suggestion_id !== suggestionId) return;
      if (stage !== 'preflight_rejected' && stage !== 'start_failed') return;
      setPending(null);
      setFailure({
        suggestionId,
        value: { stage, message: event.data.error_message || null },
      });
    });
  }, [orchestration, suggestionId]);

  const contract = snapshot?.interactionContract ?? null;
  const composer = useOverlayComposerController({
    actions,
    initialActionId: null,
    suggestionId,
    suggestionAccepted: contract === 'action_offer' && snapshot?.reactionState === 'accepted',
    language,
    // Refreshing belongs to a conversation; once a reply starts one, the Action pane shows it.
    onRefreshedPage: () => undefined,
  });
  const approvalMode = useActionApprovalMode(null, suggestionId);
  const { submission } = composer.composer;
  const replyActionId =
    submission?.request.target.kind === 'new' ? composer.composer.initialActionId : null;
  const phase = deriveSuggestionTaskPhase({
    snapshot,
    loadFailed: loadFailedFor === suggestionId,
    pending,
    failure,
    replyActionId,
    replying: submission !== null && submission.state !== 'failed',
  });
  const permissionsReady = approvalMode.mode !== null && !approvalMode.isSaving;

  const canAccept =
    phase === 'actionable' &&
    contract === 'action_offer' &&
    permissionsReady &&
    composer.composer.attachmentsInFlight === 0 &&
    !composer.supplementInvalid;
  const accept = async () => {
    if (!canAccept || !orchestration || approvalMode.mode === null) return;
    setPending({ suggestionId, value: 'accept' });
    setFailure(null);
    try {
      await orchestration.acceptAction({
        suggestionId,
        commandId: null,
        supplement: composer.supplement,
        supplementProjectRefs: composer.projectRefs,
        approvalMode: approvalMode.mode,
        images: composer.images,
        files: composer.files,
      });
    } catch {
      setFailure({ suggestionId, value: { stage: 'accept_failed' } });
    } finally {
      // The record main stored for the accept now says it is starting.
      setPending((current) => (current?.suggestionId === suggestionId ? null : current));
    }
  };

  const canDismiss = phase === 'actionable' && contract === 'action_offer';
  const dismiss = () => {
    if (!canDismiss || !orchestration) return;
    // ws:send is fire-and-forget: a transport failure in main never reaches this window.
    dismissingRef.current = suggestionId;
    setPending({ suggestionId, value: 'dismiss' });
    setFailure(null);
    orchestration.send({ event: 'dismiss_suggestion', data: { suggestion_id: suggestionId } });
  };

  const canReply =
    phase === 'actionable' &&
    contract === 'message_only' &&
    permissionsReady &&
    submission === null &&
    composer.composer.draft.trim() !== '' &&
    composer.composer.attachmentsInFlight === 0;
  const reply = () => {
    if (!canReply || snapshot === null) return;
    composer.submitDraft(null, true, null, snapshot.lastSequence, approvalMode.mode, suggestionId);
  };

  return {
    snapshot,
    phase,
    failure,
    actionId: snapshot?.actionId ?? replyActionId,
    composer,
    approvalMode,
    canAccept,
    accept,
    canDismiss,
    dismiss,
    canReply,
    reply,
  };
}
