import {
  initialComposerState,
  reconcileCanonicalSubmission,
  useOverlayComposerController,
} from './agent-overlay/useOverlayComposerController';
import {
  createConversationPaging,
  EMPTY_CONVERSATION_PAGING,
} from './agent-overlay/conversationPaging';
import { useConversationScroll } from './agent-overlay/useConversationScroll';
import { useConversationCopy } from './agent-overlay/conversationCopy';
import React, { useEffect, useMemo, useRef, useState } from 'react';
import AgentOverlayShell from './agent-overlay/AgentOverlayShell';
import { useAgentOverlayController } from './agent-overlay/useAgentOverlayController';
import { useI18n } from '@/context/useI18n';
import { useOverlayHeaderDrag } from './agent-overlay/useOverlayHeaderDrag';
import { ActionConversationView } from './action-conversation/ActionConversationView';
import { projectActionConversationView } from '../../electron/src/actions/actionConversationModel';
import type {
  ActionLiveSnapshot,
  ActionTransientToolStep,
} from '../../electron/src/actions/actionLiveCore';
import { createActionToolOutputLoader } from '../../electron/src/actions/actionToolOutputLoader';
import { useActionApprovalDecisionController } from './agent-overlay/useActionApprovalDecisionController';
import { useActionApprovalMode } from './agent-overlay/useActionApprovalMode';
import { useCompletionViewed } from './agent-overlay/useCompletionViewed';
import { SuggestionInputDisclosure } from './agent-overlay/SuggestionInputDisclosure';
import { useStandaloneComposerFocus } from './agent-overlay/useStandaloneComposerFocus';
import { SuggestionDecisionButtons } from './agent-overlay/SuggestionDecisionButtons';
import { useReplyAfterDismissal } from './agent-overlay/useReplyAfterDismissal';
import {
  ComposerStopButton,
  ComposerSubmissionRow,
  ComposerSubmissionStatus,
  OverlayComposer,
} from './agent-overlay/OverlayComposer';
type AgentOverlayProps = {
  entryMode?: 'overlay' | 'standalone';
  /** Existing Action this standalone Overlay continues, or null for a new conversation. */
  initialActionId?: string | null;
};
const AgentOverlay: React.FC<AgentOverlayProps> = ({
  entryMode = 'overlay',
  initialActionId = null,
}) => {
  const ctrl = useAgentOverlayController(entryMode === 'standalone');
  const headerDrag = useOverlayHeaderDrag();
  const { state } = ctrl;
  const { language, t } = useI18n();
  const actions = window.electron?.actions;
  if (window.electron && !actions) throw new Error('Missing Actions bridge.');
  const [liveState, setLiveState] = useState<Pick<
    ActionLiveSnapshot,
    'actionId' | 'lifecycle'
  > | null>(null);
  const [paging, setPaging] = useState(EMPTY_CONVERSATION_PAGING);
  const livePageRef = useRef<{ actionId: string; version: number } | null>(null);
  const { pages, olderPageState, boundPageState } = paging;
  const conversation = useMemo(
    () => (actions ? createConversationPaging(actions, setPaging) : null),
    [actions]
  );
  useEffect(() => () => conversation?.dispose(), [conversation]);
  const [transientToolSteps, setTransientToolSteps] = useState<readonly ActionTransientToolStep[]>(
    []
  );
  const {
    composer,
    images,
    files,
    canAttach,
    projectRefs: supplementProjectRefs,
    supplement,
    supplementInvalid,
    setComposer,
    composerGenerationRef,
    submissionRefreshScopeRef,
    restoreComposerFocusRef,
    submitDraft,
    requestResume,
    changeDraft,
    attachFiles,
    removeAttachment,
    refreshSubmission,
    retrySubmission,
  } = useOverlayComposerController({
    actions,
    initialActionId,
    suggestionId: entryMode === 'overlay' ? state.suggestionId : null,
    suggestionAccepted: entryMode === 'overlay' && state.reactionState === 'accepted',
    language,
    onRefreshedPage: (page) => {
      conversation?.update(page);
      setLiveState((current) => (current === liveState ? null : current));
    },
    onConversationStarted: ctrl.openTask,
  });
  const standaloneComposerRef = useRef<HTMLTextAreaElement>(null);
  const submissionControlRef = useRef<HTMLButtonElement>(null);
  const toolOutputLoader = useMemo(
    () => (actions ? createActionToolOutputLoader(actions.readToolOutputPage) : null),
    [actions]
  );
  const submittedActionId =
    composer.submission?.request.target.kind === 'existing'
      ? composer.submission.request.target.action_id
      : null;
  const conversationActionId =
    entryMode === 'standalone'
      ? composer.initialActionId
      : (submittedActionId ?? composer.initialActionId ?? state.currentActionId);
  const approvalMode = useActionApprovalMode(
    conversationActionId,
    entryMode === 'overlay' ? state.suggestionId : null
  );
  const { reset: resetApprovalMode } = approvalMode;
  const permissionsReady = approvalMode.mode !== null && !approvalMode.isSaving;
  const approval = useActionApprovalDecisionController({ t });
  const { approvalBlockers, approvalUiState, setApprovalBlockers } = approval;
  useStandaloneComposerFocus(entryMode === 'standalone', standaloneComposerRef);
  useEffect(() => {
    submissionRefreshScopeRef.current += 1;
    setApprovalBlockers([]);
    if (!actions || !toolOutputLoader) return;
    return actions.onConversationUpdated((update) => {
      if (update.kind === 'reset') {
        resetApprovalMode();
        livePageRef.current = null;
        submissionRefreshScopeRef.current += 1;
        restoreComposerFocusRef.current = false;
        toolOutputLoader.clear();
        conversation?.reset();
        setLiveState(null);
        setTransientToolSteps([]);
        setApprovalBlockers([]);
        composerGenerationRef.current += 1;
        setComposer(initialComposerState(initialActionId));
      } else if (
        conversationActionId !== null &&
        update.snapshot.actionId === conversationActionId
      ) {
        const { actionId, pageVersion, page, lifecycle } = update.snapshot;
        setLiveState({ actionId, lifecycle });
        if (
          livePageRef.current?.actionId !== actionId ||
          livePageRef.current.version !== pageVersion
        ) {
          livePageRef.current = { actionId, version: pageVersion };
          conversation?.update(page);
        }
        setTransientToolSteps(update.snapshot.transientToolSteps);
        setApprovalBlockers(update.snapshot.approvalBlockers);
        if (update.snapshot.page) {
          const page = update.snapshot.page;
          if (submissionControlRef.current?.contains(document.activeElement)) {
            restoreComposerFocusRef.current = true;
          }
          setComposer((current) => reconcileCanonicalSubmission(current, page));
        }
      }
    });
  }, [
    actions,
    conversationActionId,
    conversation,
    composerGenerationRef,
    restoreComposerFocusRef,
    setComposer,
    submissionRefreshScopeRef,
    initialActionId,
    resetApprovalMode,
    setApprovalBlockers,
    toolOutputLoader,
  ]);

  const visiblePages =
    conversationActionId !== null && pages?.[0].action.action_id === conversationActionId
      ? pages
      : null;
  const view = projectActionConversationView(
    visiblePages,
    composer.submission ? [composer.submission] : [],
    visiblePages ? transientToolSteps : []
  );
  const currentView = view.action !== null || composer.submission !== null ? view : null;
  // A message-only suggestion has no start of its own, and a dismissed offer has none left: a
  // reply to either starts a new conversation that opens with the suggestion.
  const repliesToSuggestion =
    state.interactionContract === 'message_only' || state.reactionState === 'rejected';
  const approvedSuggestion = currentView?.action?.approved_suggestion ?? null;
  const suggestionDisplay = {
    suggestionText:
      approvedSuggestion?.content ??
      (repliesToSuggestion && currentView?.action ? '' : state.suggestionText),
    isSuggestionStreamFinished: approvedSuggestion !== null || state.isSuggestionStreamFinished,
    isSuggestionAccepted:
      approvedSuggestion !== null ||
      (state.interactionContract === 'action_offer' && state.reactionState === 'accepted'),
  };
  const canonicalPage =
    paging.currentPage?.action.action_id === conversationActionId ? paging.currentPage : null;
  const lifecycle = liveState?.actionId === conversationActionId ? liveState.lifecycle : null;
  useConversationScroll({
    actionId: conversationActionId,
    userTurnId:
      composer.submission?.request.message.message_id ?? composer.resume?.messageId ?? null,
    liveUpdate: liveState,
    paging,
    scrollRef: ctrl.scrollableContentRef,
    contentRef: ctrl.contentInnerRef,
    answerRef: ctrl.answerAreaRef,
  });
  const canonicalStatus = canonicalPage?.action.status;
  const isTerminal = lifecycle
    ? lifecycle.status !== 'processing'
    : canonicalStatus && !['queued', 'processing'].includes(canonicalStatus);
  const isStartedSubmissionCurrent = Boolean(
    composer.submissionStartFence?.processId &&
    composer.submissionStartFence.sequence === state.lastSequence &&
    composer.submissionStartFence.processId === currentView?.action?.latest_run_id
  );
  const pageStatus =
    state.isActionStreamFinished && !isTerminal && !isStartedSubmissionCurrent
      ? null
      : canonicalStatus;
  const actionStatus =
    lifecycle?.status ??
    (pageStatus === 'queued' ? 'processing' : (pageStatus ?? state.actionStatusState));
  const operationalError =
    !lifecycle &&
    !isTerminal &&
    !isStartedSubmissionCurrent &&
    (state.actionFailureStage === 'start_failed' ||
      state.actionFailureStage === 'persist_final_state_failed')
      ? (state.actionFailureMessagePublic ?? '')
      : '';
  // 実行が失敗して終わったのに canonical な結果行がまだ会話に現れていない間（会話の更新前、
  // または更新に失敗したとき）は、オーバーレイに失敗の手掛かりが何も残らない。
  // 終了状態のラベルは持たない方針なので、結果行が届くまでの間だけ地の文で非成功を伝える。
  const currentRunId =
    lifecycle?.processId ?? canonicalPage?.action.latest_run_id ?? state.currentProcessId;
  const currentRunLines =
    currentView?.items.flatMap((item) =>
      item.kind === 'run' && item.runId === currentRunId ? item.lines : []
    ) ?? [];
  const latestRunOutcomeShown = currentRunLines.some(
    (line) => line.kind === 'final_output' || line.kind === 'terminal_outcome'
  );
  const completionEventId =
    latestRunOutcomeShown && !operationalError
      ? (canonicalPage?.runs.find((run) => run.run_id === canonicalPage.action.latest_run_id)
          ?.completion_event_id ?? null)
      : null;
  const { endRef: completionEndRef, failed: completionReadFailed } = useCompletionViewed(
    conversationActionId,
    completionEventId,
    (entryMode === 'standalone' || state.isOverlayVisible) &&
      (state.historyExpandOverride ?? state.isExpanded)
  );
  const failureFallbackText =
    !operationalError &&
    (actionStatus === 'error' || actionStatus === 'timeout') &&
    !latestRunOutcomeShown
      ? lifecycle
        ? t('overlay.executionStoppedWithError')
        : state.actionFailureMessagePublic || t('common.unexpectedError')
      : null;
  // Main pushes the bound conversation when the Overlay opens; this is the
  // recovery path for a failed push, so the window is never a dead blank.
  const loadBoundConversation = async (focusOwner: HTMLButtonElement) => {
    if (conversationActionId === null) return;
    const restoreFocus = focusOwner.contains(document.activeElement);
    const page = await conversation?.loadBound(conversationActionId);
    if (!page) return;
    setLiveState((current) => (current === liveState ? null : current));
    window.requestAnimationFrame(() => {
      if (restoreFocus && !focusOwner.isConnected && document.activeElement === document.body)
        standaloneComposerRef.current?.focus();
    });
  };
  const loadOlder = async (focusOwner: HTMLButtonElement) => {
    const page = await conversation?.loadOlder();
    if (!page) return;
    if (page.next_cursor === null && document.activeElement === focusOwner)
      ctrl.answerAreaRef.current?.focus();
    setComposer((current) => reconcileCanonicalSubmission(current, page));
  };
  useEffect(() => {
    if (visiblePages !== null)
      setComposer((current) => visiblePages.reduce(reconcileCanonicalSubmission, current));
  }, [visiblePages, setComposer]);
  const conversationCopy = useConversationCopy(actions, currentView?.action?.action_id ?? null);
  // An unanswered offer's composer holds 承認 / 見送る; while one is under way both stay, disabled.
  const offersDecision =
    state.interactionContract === 'action_offer' &&
    state.reactionState === null &&
    Boolean(state.suggestionId) &&
    Boolean(state.suggestionText);
  const canDecide = offersDecision && !state.decisionLocked;
  const showBusyIndicator =
    approvalUiState === 'hidden' &&
    (lifecycle
      ? lifecycle.status === 'processing'
      : state.requestState === 'requesting' ||
        state.requestState === 'accepted_pending_start' ||
        actionStatus === 'processing');
  const showThinking =
    showBusyIndicator &&
    !latestRunOutcomeShown &&
    !currentRunLines.some((line) => line.kind === 'tool');
  const stopProcessId = lifecycle
    ? lifecycle.status === 'processing'
      ? lifecycle.processId
      : null
    : canonicalPage === null
      ? state.currentProcessId
      : canonicalStatus === 'queued' || canonicalStatus === 'processing'
        ? canonicalPage.action.latest_run_id
        : null;
  const canStop =
    approvalUiState === 'hidden' && actionStatus === 'processing' && Boolean(stopProcessId);
  // 提案オーバーレイから新規会話を始められるのは、開始経路を持たないコメントのみの提案か
  // 見送った提案で、まだアクションも要求も存在しないときだけ。どちらも execute_action では
  // 始まらないため、提案を最初のアシスタント発言とする新規会話として開始する。
  // アクションが既にある場合（採用直後の accepted_pending_start / processing / 終了直後で
  // 会話ページがまだ届いていない間を含む）は入力欄を出さない。別アクションを作ってしまうため、
  // ページが届いてから既存会話への追記として送る。
  const canStartConversation =
    composer.initialActionId === null &&
    visiblePages === null &&
    (entryMode === 'standalone' ||
      (repliesToSuggestion &&
        Boolean(state.suggestionId) &&
        state.isSuggestionStreamFinished &&
        state.currentActionId === null &&
        state.requestState === 'idle'));
  // A reply to a message-only suggestion opens on request. After a dismissal the composer stays
  // open, to say why or to ask for something else.
  const startsReply = entryMode === 'overlay' && canStartConversation;
  const answersDismissal = startsReply && state.reactionState === 'rejected';
  const canCompose = Boolean(
    actions &&
    composer.submission === null &&
    ((visiblePages !== null && canonicalPage !== null) || canStartConversation)
  );
  // 入力欄の主ボタンが今どの操作なのか。書きかけがあれば送信、なければ動いている
  // ものを止めるか、直前に止めたものを再開する。
  const composerAction: 'send' | 'stop' | 'resume' =
    composer.draft.trim() !== ''
      ? 'send'
      : canStop
        ? 'stop'
        : !lifecycle && canonicalPage?.action.resumable === true
          ? 'resume'
          : 'send';
  const awaitsBoundConversation =
    conversationActionId !== null && canonicalPage === null && composer.submission === null;
  useEffect(() => {
    if (composer.submission?.state === 'submitting' && document.activeElement === document.body) {
      restoreComposerFocusRef.current = true;
    }
    if (!restoreComposerFocusRef.current) return;
    if (document.activeElement !== document.body) {
      if (submissionControlRef.current?.contains(document.activeElement)) return;
      restoreComposerFocusRef.current = false;
      return;
    }
    const target =
      composer.failureKind === 'action_conflict'
        ? standaloneComposerRef.current
        : composer.submission
          ? submissionControlRef.current
          : standaloneComposerRef.current;
    if (!target) return;
    target.focus();
    restoreComposerFocusRef.current = false;
  }, [
    canCompose,
    composer.failureKind,
    composer.refreshState,
    composer.submission,
    restoreComposerFocusRef,
    ctrl.answerAreaRef,
  ]);
  const conversationContent =
    toolOutputLoader &&
    !operationalError &&
    (currentView || awaitsBoundConversation || failureFallbackText) ? (
      <>
        {currentView?.action && (currentView.nextCursor !== null || olderPageState === 'failed') ? (
          <button
            className="action-conversation__disclosure"
            type="button"
            aria-disabled={olderPageState === 'loading'}
            aria-live={olderPageState === 'failed' ? 'assertive' : undefined}
            onClick={(event) => {
              if (olderPageState === 'loading') return;
              void loadOlder(event.currentTarget);
            }}
          >
            {t(`overlay.conversation.${olderPageState}`)}
          </button>
        ) : null}
        {currentView ? (
          <ActionConversationView
            view={currentView}
            lifecycle={lifecycle}
            toolOutputLoader={toolOutputLoader}
          />
        ) : null}
        {completionEventId ? (
          // Keep the 1px marker inside the result, including scrollIntoView's pixel rounding.
          <div
            ref={completionEndRef}
            aria-hidden="true"
            style={{ height: 1, position: 'relative', top: -2 }}
          />
        ) : null}
        {completionReadFailed ? (
          <p role="alert">{t('history.failedToMarkCompletionViewed')}</p>
        ) : null}
        {failureFallbackText ? (
          <section className="action-conversation__outcome" role="status">
            <p>{failureFallbackText}</p>
          </section>
        ) : null}
        {awaitsBoundConversation || (lifecycle && lifecycle.status !== 'processing') ? (
          <button
            className="action-conversation__retry"
            type="button"
            aria-disabled={boundPageState === 'loading'}
            aria-live={boundPageState === 'failed' ? 'assertive' : undefined}
            onClick={(event) => void loadBoundConversation(event.currentTarget)}
          >
            {t(`overlay.composer.refresh.${boundPageState}`)}
          </button>
        ) : null}
      </>
    ) : null;
  const submissionControls = composer.submission ? (
    <ComposerSubmissionStatus
      composer={composer}
      controlRef={submissionControlRef}
      onRetry={retrySubmission}
      onRefresh={(button) => void refreshSubmission(button)}
      onStop={canStop ? () => ctrl.onStop(stopProcessId ?? undefined) : undefined}
    />
  ) : null;
  const canAcceptSuggestion =
    permissionsReady && composer.attachmentsInFlight === 0 && !supplementInvalid;
  const sendDraft = () => {
    if (permissionsReady)
      submitDraft(
        visiblePages,
        canStartConversation,
        stopProcessId,
        state.lastSequence,
        approvalMode.mode,
        entryMode === 'overlay' && repliesToSuggestion ? state.suggestionId : null
      );
  };
  const replyAfterDismissal = useReplyAfterDismissal(
    state.suggestionId,
    answersDismissal,
    sendDraft
  );
  const hasWords = composer.draft.trim() !== '';
  const acceptSuggestion = () => {
    if (!canDecide || !canAcceptSuggestion || approvalMode.mode === null) return;
    ctrl.onAccept({
      supplement,
      supplementProjectRefs,
      approvalMode: approvalMode.mode,
      images,
      files,
    });
  };
  const composerContent =
    toolOutputLoader &&
    (!operationalError || composer.submission) &&
    (canCompose || offersDecision || composer.submission) ? (
      <OverlayComposer
        approvalMode={approvalMode}
        draft={composer.draft}
        mentions={composer.mentions}
        submissionControls={submissionControls}
        retryAcceptance={offersDecision && ctrl.acceptFailed}
        attachments={composer.attachments}
        attachmentFailure={composer.attachmentFailure}
        validationFailed={offersDecision ? supplementInvalid : composer.validationFailed}
        canAttach={canAttach}
        action={
          offersDecision
            ? {
                decision: (
                  <SuggestionDecisionButtons
                    canDismiss={
                      canDecide &&
                      (!hasWords || (permissionsReady && composer.attachmentsInFlight === 0))
                    }
                    canAccept={canDecide && canAcceptSuggestion}
                    onDismiss={() => {
                      replyAfterDismissal(hasWords);
                      ctrl.onReject(hasWords);
                    }}
                    onAccept={acceptSuggestion}
                  />
                ),
              }
            : composerAction
        }
        canSend={
          !composer.submission && hasWords && composer.attachmentsInFlight === 0 && permissionsReady
        }
        resumeFailed={composer.resume?.state === 'failed'}
        canResume={
          permissionsReady && (composer.resume === null || composer.resume.state === 'failed')
        }
        textareaRef={standaloneComposerRef}
        placeholder={answersDismissal ? t('overlay.composer.dismissedPlaceholder') : undefined}
        onAddProject={() => window.electron?.agentOverlay?.openWorkspaceSettings?.()}
        onDraftChange={changeDraft}
        onAttachFiles={(files) => void attachFiles(files)}
        onRemoveAttachment={removeAttachment}
        onSubmit={sendDraft}
        onStop={() => ctrl.onStop(stopProcessId ?? undefined)}
        onResume={() => {
          if (permissionsReady) requestResume(visiblePages);
        }}
      />
    ) : toolOutputLoader && !operationalError && canStop ? (
      <ComposerSubmissionRow>
        <ComposerStopButton onStop={() => ctrl.onStop(stopProcessId ?? undefined)} />
      </ComposerSubmissionRow>
    ) : null;

  return (
    <AgentOverlayShell
      isVisible={entryMode === 'standalone' || state.isOverlayVisible}
      isContentVisible={entryMode === 'standalone' || state.isOverlayVisible}
      isExpanded={
        state.historyExpandOverride !== null ? state.historyExpandOverride : state.isExpanded
      }
      content={state.content}
      {...suggestionDisplay}
      actionText={operationalError}
      conversationContent={conversationContent}
      composer={
        startsReply && !answersDismissal && composerContent ? (
          <SuggestionInputDisclosure
            key={state.suggestionId}
            label={t('overlay.composer.open')}
            inputRef={standaloneComposerRef}
          >
            {composerContent}
          </SuggestionInputDisclosure>
        ) : offersDecision && ctrl.acceptFailed ? (
          <>
            <p role="alert">{t('overlay.acceptFailed')}</p>
            {composerContent}
          </>
        ) : (
          composerContent
        )
      }
      isActionStreamFinished={state.isActionStreamFinished}
      approvalUiState={approvalUiState}
      approvalBlockers={approvalBlockers}
      isSubmittingApproval={approval.isSubmittingApproval}
      approvalErrorMessage={approval.approvalErrorMessage}
      showBusyIndicator={showBusyIndicator}
      showThinking={showThinking}
      fadeDurationMs={600}
      onToggleExpand={ctrl.onToggleExpand}
      onClose={ctrl.onClose}
      onDecideApproval={
        approvalUiState === 'approval_pending'
          ? (decision, blocker) => void approval.submitApprovalDecision(decision, blocker)
          : undefined
      }
      onOpenWorkspaceSettings={window.electron?.agentOverlay?.openWorkspaceSettings}
      openTaskFailed={ctrl.openTaskFailed}
      chatActionId={currentView?.action?.action_id ?? null}
      onShowChat={window.electron?.agentOverlay?.showChat}
      conversationCopy={conversationCopy}
      onHeaderPointerDown={headerDrag.onHeaderPointerDown}
      onHeaderPointerMove={headerDrag.onHeaderPointerMove}
      onHeaderPointerUp={headerDrag.onHeaderPointerUp}
      onHeaderPointerCancel={headerDrag.onHeaderPointerCancel}
      headerRef={ctrl.headerRef}
      scrollableRef={ctrl.scrollableContentRef}
      contentInnerRef={ctrl.contentInnerRef}
      answerAreaRef={ctrl.answerAreaRef}
      composerRef={ctrl.composerRef}
      containerRef={ctrl.containerRef}
    />
  );
};
export default AgentOverlay;
