import { useId, useRef, useState, type ReactNode } from 'react';
import { Check, CircleAlert, Clipboard, MessageCircle } from 'lucide-react';

import type { ActionConversationView as ActionConversationViewModel } from '../../../electron/src/actions/actionConversationModel';
import { ActionConversationView } from '@/components/action-conversation/ActionConversationView';
import { ApprovalPanel } from '@/components/agent-overlay/ApprovalPanel';
import {
  ComposerSubmissionStatus,
  OverlayComposer,
} from '@/components/agent-overlay/OverlayComposer';
import { useConversationCopy } from '@/components/agent-overlay/conversationCopy';
import { useCompletionViewed } from '@/components/agent-overlay/useCompletionViewed';
import { useConversationScroll } from '@/components/agent-overlay/useConversationScroll';
import { useI18n } from '@/context/useI18n';
import { liveStageText } from '@/components/history/liveStageText';
import { useHistoryLiveStages } from '@/hooks/useHistoryLiveStages';

import type { TaskComposerDrafts } from './taskComposerDrafts';
import { useActionTask } from './useActionTask';
import './actionTaskPane.css';

type ActionTaskPaneProps = {
  /** The pane holds one Action's state, so the page keys it by this id. */
  actionId: string;
  /** The history row's title for this Action. */
  title: string;
  /**
   * A file's preview, shown left of the conversation, which narrows to a column. It sits below
   * the header and beside the conversation, so opening and closing it keeps the conversation
   * mounted with its scroll position and draft. It gets the conversation so far, so a preview
   * can follow the Action's later edits.
   */
  renderPreview?: (view: ActionConversationViewModel | null) => ReactNode;
  onShowInChat: () => void;
  /** Where projects are added: the composer's @-mention option and an approval's folder hint. */
  onAddProject: () => void;
  /** Content under the latest answer, such as the files it produced. */
  renderFileChips?: (view: ActionConversationViewModel) => ReactNode;
  /** Where the composer waits while the pane is not shown. */
  drafts: TaskComposerDrafts;
};

/**
 * One Action in the main window's detail pane: its conversation (steps, diffs, answers), the
 * approvals it waits on, and the composer that continues, stops or resumes it.
 */
export function ActionTaskPane({
  actionId,
  title,
  renderPreview,
  onShowInChat,
  onAddProject,
  renderFileChips,
  drafts,
}: ActionTaskPaneProps) {
  const { language, t } = useI18n();
  const titleId = useId();
  const scrollRef = useRef<HTMLDivElement>(null);
  const contentRef = useRef<HTMLDivElement>(null);
  const answerRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const submissionControlRef = useRef<HTMLButtonElement>(null);
  const [reopenRequested, setReopenRequested] = useState(false);

  const task = useActionTask(actionId, drafts);
  const { view, lifecycle, status, conversation, approval, composer } = task;
  const draft = composer.state;
  const conversationCopy = useConversationCopy(window.electron?.actions, actionId);
  const copyFailed =
    conversationCopy?.status === 'failed' ? t('overlay.copyConversationFailed') : null;
  useConversationScroll({
    actionId,
    userTurnId: draft.submission?.request.message.message_id ?? draft.resume?.messageId ?? null,
    liveUpdate: conversation.liveUpdate,
    paging: conversation.paging,
    scrollRef,
    contentRef,
    answerRef,
  });
  // The hook itself waits for a focused window with the answer's end in view.
  const { endRef: completionEndRef, failed: completionReadFailed } = useCompletionViewed(
    actionId,
    status.completionEventId,
    true
  );

  const liveStage = useHistoryLiveStages().get(actionId);
  const canStop = status.kind === 'running' && status.stopTarget !== null;
  // A run that ended but whose page read failed leaves no answer; reopening reads it again.
  const runEndedUnread = lifecycle !== null && lifecycle.status !== 'processing';
  const showReopen =
    conversation.openState === 'failed' ||
    (conversation.openState === 'opening' && reopenRequested) ||
    runEndedUnread;
  const reopenLabel = t(
    `overlay.composer.refresh.${
      conversation.openState === 'failed'
        ? 'failed'
        : conversation.openState === 'opening'
          ? 'loading'
          : 'idle'
    }`
  );

  const reopen = async (button: HTMLButtonElement) => {
    if (conversation.openState === 'opening') return;
    setReopenRequested(true);
    await conversation.reopen();
    window.requestAnimationFrame(() => {
      if (!button.isConnected && document.activeElement === document.body)
        textareaRef.current?.focus();
    });
  };
  const loadOlder = async (button: HTMLButtonElement) => {
    if (conversation.olderPageState === 'loading') return;
    const page = await conversation.loadOlder();
    if (page?.next_cursor === null && document.activeElement === button) answerRef.current?.focus();
  };

  const preview = renderPreview?.(view) ?? null;

  return (
    <section
      className={preview ? 'action-task action-task--split' : 'action-task'}
      aria-labelledby={titleId}
    >
      <header className="action-task__header">
        <h1 id={titleId}>{title}</h1>
        {status.kind === 'approval_pending' ? (
          <p className="action-task__state">{t('history.status.approvalPending')}</p>
        ) : liveStage ? (
          // Visual only: it changes on every step, and the conversation announces the run.
          <p className="action-task__state" aria-hidden="true">
            {liveStageText(liveStage, language, t)}
          </p>
        ) : null}
        <div className="action-task__header-actions">
          <button
            type="button"
            className="action-task__icon-button"
            aria-label={t('overlay.showInChat')}
            title={t('overlay.showInChat')}
            onClick={onShowInChat}
          >
            <MessageCircle size={17} strokeWidth={1.8} aria-hidden />
          </button>
          {conversationCopy ? (
            <button
              type="button"
              className="action-task__icon-button"
              aria-label={t('overlay.copyConversation')}
              title={copyFailed ?? t('overlay.copyConversation')}
              onClick={conversationCopy.copy}
            >
              {conversationCopy.status === 'copied' ? (
                <Check size={17} strokeWidth={1.8} aria-hidden />
              ) : conversationCopy.status === 'failed' ? (
                <CircleAlert size={17} strokeWidth={1.8} aria-hidden />
              ) : (
                <Clipboard size={17} strokeWidth={1.8} aria-hidden />
              )}
            </button>
          ) : null}
          {copyFailed ? (
            <span className="action-task__sr-only" role="alert">
              {copyFailed}
            </span>
          ) : null}
        </div>
      </header>
      {preview}
      <div className="action-task__scroll" ref={scrollRef}>
        <div className="action-task__column" ref={contentRef}>
          <div className="action-task__conversation" ref={answerRef} tabIndex={-1}>
            {view?.action &&
            (view.nextCursor !== null || conversation.olderPageState === 'failed') ? (
              <button
                className="action-conversation__disclosure"
                type="button"
                aria-disabled={conversation.olderPageState === 'loading'}
                aria-live={conversation.olderPageState === 'failed' ? 'assertive' : undefined}
                onClick={(event) => void loadOlder(event.currentTarget)}
              >
                {t(`overlay.conversation.${conversation.olderPageState}`)}
              </button>
            ) : null}
            {conversation.awaitingPage && !showReopen ? (
              <p className="action-task__status" role="status">
                {t('history.task.loading')}
              </p>
            ) : null}
            {view ? (
              <ActionConversationView
                view={view}
                lifecycle={lifecycle}
                toolOutputLoader={task.toolOutputLoader}
              />
            ) : null}
            {status.thinking ? (
              // Screen readers already hear "Running" from the conversation.
              <p className="action-task__thinking" aria-hidden>
                {t('overlay.thinking')}
              </p>
            ) : null}
            {status.failedWithoutOutcome ? (
              <section className="action-conversation__outcome" role="status">
                <p>
                  {lifecycle ? t('overlay.executionStoppedWithError') : t('common.unexpectedError')}
                </p>
              </section>
            ) : null}
            {status.completionEventId !== null ? (
              <>
                <div ref={completionEndRef} aria-hidden="true" className="action-task__end" />
                {view && renderFileChips ? renderFileChips(view) : null}
              </>
            ) : null}
            {completionReadFailed ? (
              <p className="action-task__alert" role="alert">
                {t('history.failedToMarkCompletionViewed')}
              </p>
            ) : null}
            {showReopen ? (
              <button
                className="action-conversation__retry"
                type="button"
                aria-disabled={conversation.openState === 'opening'}
                aria-live={conversation.openState === 'failed' ? 'assertive' : undefined}
                onClick={(event) => void reopen(event.currentTarget)}
              >
                {reopenLabel}
              </button>
            ) : null}
          </div>
        </div>
      </div>
      <div className="action-task__dock">
        {status.kind === 'approval_pending' && approval.blockers.length > 0 ? (
          <div className="action-task__approvals">
            {approval.blockers.map((blocker) => (
              <ApprovalPanel
                key={`${blocker.approvalSessionId}:${blocker.toolRequestId}`}
                approvalPanel={blocker}
                approvalErrorMessage={approval.errorMessage}
                isSubmittingApproval={approval.isSubmitting}
                onDecide={(decision) => void approval.decide(decision, blocker)}
                onOpenWorkspaceSettings={onAddProject}
                t={t}
              />
            ))}
          </div>
        ) : null}
        <OverlayComposer
          approvalMode={task.approvalMode}
          draft={draft.draft}
          mentions={draft.mentions}
          submissionControls={
            draft.submission ? (
              <ComposerSubmissionStatus
                composer={draft}
                controlRef={submissionControlRef}
                onRetry={composer.retrySubmission}
                onRefresh={(button) => void composer.refreshSubmission(button)}
                onStop={canStop ? task.stop : undefined}
              />
            ) : null
          }
          retryAcceptance={false}
          attachments={draft.attachments}
          attachmentFailure={draft.attachmentFailure}
          validationFailed={draft.validationFailed}
          canAttach={composer.canAttach}
          action={composer.action}
          canSend={composer.canSend}
          resumeFailed={draft.resume?.state === 'failed'}
          canResume={composer.canResume}
          textareaRef={textareaRef}
          onAddProject={onAddProject}
          onDraftChange={composer.changeDraft}
          onAttachFiles={(files) => void composer.attachFiles(files)}
          onRemoveAttachment={composer.removeAttachment}
          onSubmit={task.send}
          onStop={task.stop}
          onResume={task.resume}
        />
      </div>
    </section>
  );
}
