import { useEffect, useState } from 'react';

import { useI18n } from '@/context/useI18n';
import type { OverlaySnapshot } from '../../../electron/src/orchestration/contracts';
import { isErrorEvent } from '../../../electron/src/orchestration/eventContracts';
import { isActionErrorMeta } from '../agent-overlay/model/overlayTypes';
import { useActionApprovalMode } from '../agent-overlay/useActionApprovalMode';
import { useOverlayComposerController } from '../agent-overlay/useOverlayComposerController';

/**
 * Why the last answer did not go through. `accept_failed` is the accept call itself failing (main
 * sent nothing, or put its record back); `preflight_rejected` and `start_failed` are the backend's
 * answer to `execute_action`.
 */
export type SuggestionStartFailure =
  | { stage: 'accept_failed' }
  | { stage: 'preflight_rejected' | 'start_failed'; message: string | null };

export type SuggestionTaskPhase =
  | 'loading'
  | 'load_failed'
  /** The text is still arriving; whether it offers an Action is not known yet. */
  | 'streaming'
  | 'actionable'
  | 'starting'
  | 'start_failed'
  /** An Action exists for it; the Action pane takes over. */
  | 'started'
  /** Dismissed before replies replaced 見送る, with its reason if any. A reply may still start an Action. */
  | 'dismissed';

type Scoped<T> = { suggestionId: string; value: T };

export function deriveSuggestionTaskPhase({
  snapshot,
  loadFailed,
  accepting,
  failure,
  replyActionId,
  replying,
}: {
  snapshot: OverlaySnapshot | null;
  loadFailed: boolean;
  accepting: boolean;
  failure: SuggestionStartFailure | null;
  replyActionId: string | null;
  replying: boolean;
}): SuggestionTaskPhase {
  if (snapshot === null) return loadFailed ? 'load_failed' : 'loading';
  if (snapshot.actionId !== null || replyActionId !== null) return 'started';
  if (snapshot.reactionState === 'rejected') return replying ? 'starting' : 'dismissed';
  if (failure?.stage === 'start_failed') return 'start_failed';
  if (
    accepting ||
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
 * One unanswered suggestion in the main window: main's record of it, and the reply that answers it.
 *
 * A reply goes the way the Overlay sends it. To an offer it is the acceptance: `ws:acceptAction`
 * with no command id, so main reuses the command it already holds for this suggestion, and the
 * reply as its supplement. To a message-only suggestion, or to one dismissed earlier, it opens a
 * new Action with `reply_to_suggestion_id` through the Overlay's composer controller.
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
  const [acceptingFor, setAcceptingFor] = useState<string | null>(null);
  const [failureState, setFailure] = useState<Scoped<SuggestionStartFailure> | null>(null);
  const snapshot = scoped(record, suggestionId);
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

  // main's record does not keep why a start failed; the backend's error events say it.
  useEffect(() => {
    if (!orchestration?.onEvent) return;
    return orchestration.onEvent((event) => {
      if (!isErrorEvent(event) || !isActionErrorMeta(event.meta)) return;
      const { stage, suggestion_id } = event.meta;
      if (suggestion_id !== suggestionId) return;
      if (stage !== 'preflight_rejected' && stage !== 'start_failed') return;
      setAcceptingFor(null);
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
  // Only a reply, sent or found already open, gives this composer an Action.
  const replyActionId = composer.composer.initialActionId;
  const phase = deriveSuggestionTaskPhase({
    snapshot,
    loadFailed: loadFailedFor === suggestionId,
    accepting: acceptingFor === suggestionId,
    failure,
    replyActionId,
    replying: submission !== null && submission.state !== 'failed',
  });

  const hasWords = composer.hasMessage;
  // A reply to an unanswered offer accepts it, with the reply as what to do next.
  const repliesToOffer = phase === 'actionable' && contract === 'action_offer';
  const canSend =
    (repliesToOffer ||
      (phase === 'actionable' && contract === 'message_only') ||
      phase === 'dismissed') &&
    submission === null &&
    hasWords &&
    composer.composer.attachmentsInFlight === 0 &&
    !(repliesToOffer && composer.supplementInvalid);
  const send = () => {
    if (!canSend || snapshot === null) return;
    if (!repliesToOffer) {
      composer.submitDraft(null, true, null, snapshot.lastSequence, suggestionId);
      return;
    }
    if (!orchestration) return;
    setAcceptingFor(suggestionId);
    setFailure(null);
    orchestration
      .acceptAction({
        suggestionId,
        commandId: null,
        supplement: composer.supplement,
        supplementProjectRefs: composer.refs.project_refs,
        supplementWorkRefs: composer.refs.work_refs,
        images: composer.images,
        files: composer.files,
      })
      .catch(() => setFailure({ suggestionId, value: { stage: 'accept_failed' } }))
      // The record main stored for the accept now says it is starting.
      .finally(() => setAcceptingFor((current) => (current === suggestionId ? null : current)));
  };

  return {
    snapshot,
    phase,
    failure,
    actionId: snapshot?.actionId ?? replyActionId,
    composer,
    approvalMode,
    canSend,
    send,
  };
}
