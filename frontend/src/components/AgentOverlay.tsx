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
  useEffect(() => {
    if (entryMode === 'standalone') standaloneComposerRef.current?.focus();
  }, [entryMode]);
  useEffect(() => {
    if (entryMode !== 'standalone') return;
    return window.electron?.ipcRenderer.on('overlay:focusComposer', () => {
      standaloneComposerRef.current?.focus();
    });
  }, [entryMode]);
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
  const approvedSuggestion = currentView?.action?.approved_suggestion ?? null;
  const suggestionDisplay = {
    suggestionText:
      approvedSuggestion?.content ??
      (state.interactionContract === 'message_only' && currentView?.action
        ? ''
        : state.suggestionText),
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
  const canDecide =
    state.interactionContract === 'action_offer' &&
    state.reactionState === null &&
    !state.decisionLocked &&
    Boolean(state.suggestionId) &&
    Boolean(state.suggestionText);
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
  // 提案オーバーレイから新規会話を始められるのは、開始経路を持たないコメントのみの提案で、
  // まだアクションも要求も存在しないときだけ。コメントのみの提案は execute_action を受け付け
  // ないため、提案を最初のアシスタント発言とする新規会話として開始する。
  // アクションが既にある場合（採用直後の accepted_pending_start / processing / 終了直後で
  // 会話ページがまだ届いていない間を含む）は入力欄を出さない。別アクションを作ってしまうため、
  // ページが届いてから既存会話への追記として送る。
  const canStartConversation =
    composer.initialActionId === null &&
    visiblePages === null &&
    (entryMode === 'standalone' ||
      (state.interactionContract === 'message_only' &&
        Boolean(state.suggestionId) &&
        state.isSuggestionStreamFinished &&
        state.currentActionId === null &&
        state.requestState === 'idle'));
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
    (canCompose || canDecide || composer.submission) ? (
      <OverlayComposer
        approvalMode={approvalMode}
        draft={composer.draft}
        mentions={composer.mentions}
        submissionControls={submissionControls}
        retryAcceptance={canDecide && ctrl.acceptFailed}
        attachments={composer.attachments}
        attachmentFailure={composer.attachmentFailure}
        validationFailed={canDecide ? supplementInvalid : composer.validationFailed}
        canAttach={canAttach}
        action={canDecide ? 'accept' : composerAction}
        canSend={
          canDecide
            ? canAcceptSuggestion
            : !composer.submission &&
              composer.draft.trim() !== '' &&
              composer.attachmentsInFlight === 0 &&
              permissionsReady
        }
        resumeFailed={composer.resume?.state === 'failed'}
        canResume={
          permissionsReady && (composer.resume === null || composer.resume.state === 'failed')
        }
        textareaRef={standaloneComposerRef}
        onDraftChange={(draft, mentions) =>
          setComposer((current) => ({
            ...current,
            draft,
            mentions,
            validationFailed: false,
            resume: current.resume?.state === 'failed' ? null : current.resume,
          }))
        }
        onAttachFiles={(files) => void attachFiles(files)}
        onRemoveAttachment={removeAttachment}
        onSubmit={() => {
          if (canDecide) {
            acceptSuggestion();
            return;
          }
          if (permissionsReady)
            submitDraft(
              visiblePages,
              canStartConversation,
              stopProcessId,
              state.lastSequence,
              approvalMode.mode,
              entryMode === 'overlay' && state.interactionContract === 'message_only'
                ? state.suggestionId
                : null
            );
        }}
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
        canDecide ? null : entryMode === 'overlay' && canStartConversation && composerContent ? (
          <SuggestionInputDisclosure
            key={state.suggestionId}
            label={t('overlay.composer.open')}
            inputRef={standaloneComposerRef}
          >
            {composerContent}
          </SuggestionInputDisclosure>
        ) : (
          composerContent
        )
      }
      suggestionId={state.suggestionId}
      isActionStreamFinished={state.isActionStreamFinished}
      approvalUiState={approvalUiState}
      approvalBlockers={approvalBlockers}
      isSubmittingApproval={approval.isSubmittingApproval}
      approvalErrorMessage={approval.approvalErrorMessage}
      showBusyIndicator={showBusyIndicator}
      showThinking={showThinking}
      showFooterActions={approvalUiState === 'hidden' && canDecide}
      fadeDurationMs={600}
      onToggleExpand={ctrl.onToggleExpand}
      onClose={ctrl.onClose}
      suggestionDecision={
        canDecide
          ? {
              input: composerContent,
              inputRef: standaloneComposerRef,
              canAccept: canAcceptSuggestion,
              accept: acceptSuggestion,
              errorMessage: ctrl.acceptFailed
                ? t('overlay.acceptFailed')
                : approvalMode.errorKey
                  ? t(approvalMode.errorKey)
                  : null,
            }
          : undefined
      }
      onReject={canDecide ? ctrl.onReject : undefined}
      onDecideApproval={
        approvalUiState === 'approval_pending'
          ? (decision, blocker) => void approval.submitApprovalDecision(decision, blocker)
          : undefined
      }
      onOpenWorkspaceSettings={window.electron?.agentOverlay?.openWorkspaceSettings}
      conversationCopy={conversationCopy}
      onHeaderPointerDown={headerDrag.onHeaderPointerDown}
      onHeaderPointerMove={headerDrag.onHeaderPointerMove}
      onHeaderPointerUp={headerDrag.onHeaderPointerUp}
      onHeaderPointerCancel={headerDrag.onHeaderPointerCancel}
      headerRef={ctrl.headerRef}
      scrollableRef={ctrl.scrollableContentRef}
      contentInnerRef={ctrl.contentInnerRef}
      answerAreaRef={ctrl.answerAreaRef}
      footerRef={ctrl.footerRef}
      composerRef={ctrl.composerRef}
      containerRef={ctrl.containerRef}
    />
  );
};
export default AgentOverlay;
