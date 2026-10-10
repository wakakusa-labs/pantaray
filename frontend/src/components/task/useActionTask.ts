import { useEffect, useMemo, useRef, useState } from 'react';

import { useI18n } from '@/context/useI18n';
import { projectActionConversationView } from '../../../electron/src/actions/actionConversationModel';
import type {
  ActionApprovalBlocker,
  ActionLiveSnapshot,
  ActionTransientToolStep,
} from '../../../electron/src/actions/actionLiveCore';
import { createActionToolOutputLoader } from '../../../electron/src/actions/actionToolOutputLoader';
import type { ApprovalDecision } from '../agent-overlay/approvalDisplayModel';
import {
  createConversationPaging,
  EMPTY_CONVERSATION_PAGING,
} from '../agent-overlay/conversationPaging';
import { useActionApprovalDecisionController } from '../agent-overlay/useActionApprovalDecisionController';
import { useActionApprovalMode } from '../agent-overlay/useActionApprovalMode';
import {
  initialComposerState,
  reconcileCanonicalSubmission,
  useOverlayComposerController,
} from '../agent-overlay/useOverlayComposerController';
import { deriveActionTaskStatus } from './actionTaskStatus';

type Live = { lifecycle: ActionLiveSnapshot['lifecycle'] };

// The submission start fence orders the Overlay's suggestion stream, which the main window does
// not read; every main-window send shares this one position.
const MAIN_WINDOW_STREAM_SEQUENCE = 0;

export type ActionTask = ReturnType<typeof useActionTask>;

/**
 * One existing Action for the main window's task pane: its conversation, status, approvals and
 * composer. Its state belongs to one Action, so the pane is keyed by `actionId`. Main broadcasts every Action's updates to this
 * window, so only the ones for `actionId` are read.
 */
export function useActionTask(actionId: string) {
  const { language, t } = useI18n();
  const actions = window.electron?.actions;
  const orchestration = window.electron?.orchestration;
  if (!actions || !orchestration) throw new Error('Missing Actions bridge.');

  const [paging, setPaging] = useState(EMPTY_CONVERSATION_PAGING);
  const conversation = useMemo(() => createConversationPaging(actions, setPaging), [actions]);
  useEffect(() => () => conversation.dispose(), [conversation]);
  const toolOutputLoader = useMemo(
    () => createActionToolOutputLoader(actions.readToolOutputPage),
    [actions]
  );
  const [live, setLive] = useState<Live | null>(null);
  const [transientToolSteps, setTransientToolSteps] = useState<readonly ActionTransientToolStep[]>(
    []
  );
  const appliedPageVersionRef = useRef(0);
  const [openFailed, setOpenFailed] = useState(false);
  // A canonical read made here supersedes the lifecycle notification it was read after.
  const settleLive = (readAfter: Live | null) =>
    setLive((current) => (current === readAfter ? null : current));

  const composerControl = useOverlayComposerController({
    actions,
    initialActionId: actionId,
    suggestionId: null,
    suggestionAccepted: false,
    language,
    onRefreshedPage: (page) => {
      conversation.update(page);
      settleLive(live);
    },
  });
  const { composer, setComposer, composerGenerationRef, submissionRefreshScopeRef } =
    composerControl;
  const approvalMode = useActionApprovalMode(actionId, null);
  const { reset: resetApprovalMode } = approvalMode;
  const approval = useActionApprovalDecisionController({ t });
  const { setApprovalBlockers } = approval;

  useEffect(() => {
    return actions.onConversationUpdated((update) => {
      if (update.kind === 'reset') {
        // The signed-in owner changed: nothing read for the previous one may remain.
        resetApprovalMode();
        appliedPageVersionRef.current = 0;
        submissionRefreshScopeRef.current += 1;
        toolOutputLoader.clear();
        conversation.reset();
        setLive(null);
        setTransientToolSteps([]);
        setApprovalBlockers([]);
        setOpenFailed(false);
        composerGenerationRef.current += 1;
        setComposer(initialComposerState(actionId));
        return;
      }
      const { snapshot } = update;
      if (snapshot.actionId !== actionId) return;
      setLive({ lifecycle: snapshot.lifecycle });
      // A snapshot rebuilt after its Action finished carries version 0 and no page; it must not
      // replace a page this pane already read.
      const { page } = snapshot;
      if (page && snapshot.pageVersion > appliedPageVersionRef.current) {
        appliedPageVersionRef.current = snapshot.pageVersion;
        conversation.update(page);
        setComposer((current) => reconcileCanonicalSubmission(current, page));
      }
      setTransientToolSteps(snapshot.transientToolSteps);
      setApprovalBlockers(snapshot.approvalBlockers);
    });
  }, [
    actions,
    actionId,
    conversation,
    toolOutputLoader,
    composerGenerationRef,
    submissionRefreshScopeRef,
    setComposer,
    resetApprovalMode,
    setApprovalBlockers,
  ]);

  // Subscribed first, so the refresh this starts reaches the listener above.
  useEffect(() => {
    let current = true;
    actions.openConversation({ actionId }).catch(() => {
      if (current) setOpenFailed(true);
    });
    return () => {
      current = false;
    };
  }, [actions, actionId]);

  const visiblePages = paging.pages?.[0].action.action_id === actionId ? paging.pages : null;
  const canonicalPage =
    paging.currentPage?.action.action_id === actionId ? paging.currentPage : null;
  useEffect(() => {
    if (visiblePages !== null)
      setComposer((current) => visiblePages.reduce(reconcileCanonicalSubmission, current));
  }, [visiblePages, setComposer]);

  const projected = projectActionConversationView(
    visiblePages,
    composer.submission ? [composer.submission] : [],
    visiblePages ? transientToolSteps : []
  );
  const view = projected.action !== null || composer.submission !== null ? projected : null;
  const lifecycle = live?.lifecycle ?? null;
  const status = deriveActionTaskStatus({
    lifecycle,
    page: canonicalPage,
    approvalPending: approval.approvalUiState === 'approval_pending',
    view,
  });

  const permissionsReady = approvalMode.mode !== null && !approvalMode.isSaving;
  const canCompose =
    composer.submission === null && visiblePages !== null && canonicalPage !== null;
  const canSend =
    canCompose &&
    composer.draft.trim() !== '' &&
    composer.attachmentsInFlight === 0 &&
    permissionsReady;
  const canStop = status.kind === 'running' && status.stopTarget !== null;
  const canResume =
    status.resumable &&
    permissionsReady &&
    (composer.resume === null || composer.resume.state === 'failed');
  // The composer's primary button: send a draft, else stop the running run, else resume a stop.
  const composerAction: 'send' | 'stop' | 'resume' =
    composer.draft.trim() !== '' ? 'send' : canStop ? 'stop' : status.resumable ? 'resume' : 'send';

  const stop = () => {
    const processId = status.stopTarget;
    if (canStop && processId !== null)
      orchestration.send({ event: 'stop_process', data: { process_id: processId } });
  };
  // A follow-up joins the running run: the backend rejects it if that run is no longer current.
  const send = () => {
    if (canSend)
      composerControl.submitDraft(
        visiblePages,
        false,
        status.stopTarget,
        MAIN_WINDOW_STREAM_SEQUENCE,
        approvalMode.mode,
        null
      );
  };
  const resume = () => {
    if (canResume) composerControl.requestResume(visiblePages);
  };
  const decide = (decision: ApprovalDecision, blocker: ActionApprovalBlocker) =>
    approval.submitApprovalDecision(decision, blocker);
  const loadOlder = async () => {
    const page = await conversation.loadOlder();
    if (page) setComposer((current) => reconcileCanonicalSubmission(current, page));
    return page;
  };
  // The recovery when the open's refresh never arrives or the open itself failed.
  const reload = async () => {
    const readAfter = live;
    const page = await conversation.loadBound(actionId);
    if (!page) return;
    settleLive(readAfter);
    setOpenFailed(false);
  };

  return {
    view,
    lifecycle,
    toolOutputLoader,
    status,
    conversation: {
      olderPageState: paging.olderPageState,
      boundPageState: paging.boundPageState,
      /** No conversation page has been read for this Action yet. */
      awaitingPage: canonicalPage === null && composer.submission === null,
      openFailed,
      loadOlder,
      reload,
    },
    approval: {
      blockers: approval.approvalBlockers,
      isSubmitting: approval.isSubmittingApproval,
      errorMessage: approval.approvalErrorMessage,
      decide,
    },
    approvalMode,
    composer: {
      state: composer,
      canAttach: composerControl.canAttach,
      canSend,
      canResume,
      action: composerAction,
      changeDraft: composerControl.changeDraft,
      attachFiles: composerControl.attachFiles,
      removeAttachment: composerControl.removeAttachment,
      retrySubmission: composerControl.retrySubmission,
      refreshSubmission: composerControl.refreshSubmission,
    },
    send,
    stop,
    resume,
  };
}
