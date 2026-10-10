import { Check, CircleAlert, Clipboard, MessageCircle } from 'lucide-react';
import { useEffect, useId, useRef } from 'react';

import { MarkdownBlock } from '@/components/agent-overlay/MarkdownRenderer';
import { SuggestionDecisionButtons } from '@/components/agent-overlay/SuggestionDecisionButtons';
import { useClipboardCopy } from '@/components/agent-overlay/useClipboardCopy';
import {
  ComposerSubmissionStatus,
  OverlayComposer,
} from '@/components/agent-overlay/OverlayComposer';
import { useI18n } from '@/context/useI18n';

import { useSuggestionTask, type SuggestionStartFailure } from './useSuggestionTask';
import './actionTaskPane.css';
import './suggestionTaskPane.css';

const COPY = {
  en: {
    loading: 'Loading the suggestion…',
    loadFailed: 'Could not load this suggestion.',
    starting: 'Starting',
    dismissed: 'You dismissed this suggestion.',
    notStarted: 'This could not be started. Try again.',
    dismissFailed: 'Could not dismiss this suggestion. Try again.',
    copySuggestion: 'Copy suggestion',
  },
  ja: {
    loading: '提案を読み込んでいます',
    loadFailed: 'この提案を読み込めませんでした。',
    starting: '開始しています',
    dismissed: 'この提案は見送りました。',
    notStarted: '開始できませんでした。もう一度お試しください。',
    dismissFailed: 'この提案を見送れませんでした。もう一度お試しください。',
    copySuggestion: '提案をコピー',
  },
} as const;

const noop = () => undefined;

type SuggestionTaskPaneProps = {
  suggestionId: string;
  /** The history row's title for this suggestion. */
  title: string;
  /** Called once an Action exists for the suggestion: accepted, or replied to. */
  onStarted: (actionId: string) => void;
  /** Opens the chat, as the task pane's header button does. */
  onShowInChat: () => void;
  /** The composer's @-mention "Add project" option. */
  onAddProject: () => void;
};

/**
 * An unanswered suggestion in the main window's detail pane: its text, and a composer whose
 * 承認 / 見送る take what the user wrote, as the approval's extra instruction or as a reply after
 * the dismissal. A message-only suggestion is answered by a reply, which starts a new Action; so
 * is a dismissed one, if the user writes after all.
 */
export function SuggestionTaskPane({
  suggestionId,
  title,
  onStarted,
  onShowInChat,
  onAddProject,
}: SuggestionTaskPaneProps) {
  const { language, t } = useI18n();
  const copy = COPY[language];
  const titleId = useId();
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const submissionControlRef = useRef<HTMLButtonElement>(null);
  const task = useSuggestionTask(suggestionId);
  const { snapshot, phase, failure, composer, approvalMode } = task;
  const { status: copyStatus, copy: copyToClipboard } = useClipboardCopy();
  const suggestionText = snapshot?.suggestionText ?? '';
  const { composer: draft, setComposer } = composer;

  const onStartedRef = useRef(onStarted);
  useEffect(() => {
    onStartedRef.current = onStarted;
  });
  useEffect(() => {
    if (task.actionId !== null) onStartedRef.current(task.actionId);
  }, [task.actionId]);

  const dismissed = phase === 'dismissed';
  // An offer's composer holds 承認 / 見送る until it is dismissed; while one of them is under way
  // both stay in place, disabled.
  const decides =
    snapshot?.interactionContract === 'action_offer' &&
    snapshot.reactionState !== 'rejected' &&
    (phase === 'actionable' || phase === 'starting' || phase === 'dismissing');
  const failureText = (current: SuggestionStartFailure) =>
    current.stage === 'accept_failed'
      ? t('overlay.acceptFailed')
      : current.stage === 'dismiss_failed'
        ? copy.dismissFailed
        : (current.message ??
          (current.stage === 'start_failed' ? t('common.unexpectedError') : copy.notStarted));

  return (
    <section className="suggestion-task" aria-labelledby={titleId}>
      <header className="suggestion-task__header">
        <h1 id={titleId}>{title}</h1>
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
          <button
            type="button"
            className="action-task__icon-button"
            aria-label={copy.copySuggestion}
            title={copy.copySuggestion}
            disabled={suggestionText === ''}
            onClick={() => void copyToClipboard(() => suggestionText)}
          >
            {copyStatus === 'copied' ? (
              <Check size={17} strokeWidth={1.8} aria-hidden />
            ) : copyStatus === 'failed' ? (
              <CircleAlert size={17} strokeWidth={1.8} aria-hidden />
            ) : (
              <Clipboard size={17} strokeWidth={1.8} aria-hidden />
            )}
          </button>
        </div>
      </header>
      <div className="suggestion-task__scroll">
        <div className="suggestion-task__column">
          {phase === 'loading' ? (
            <p className="suggestion-task__status" role="status">
              {copy.loading}
            </p>
          ) : null}
          {phase === 'load_failed' ? (
            <p className="suggestion-task__alert" role="alert">
              {copy.loadFailed}
            </p>
          ) : null}
          {snapshot && snapshot.suggestionText !== '' ? (
            <MarkdownBlock
              text={snapshot.suggestionText}
              isStreamFinished={snapshot.interactionContract !== null}
            />
          ) : null}
          {phase === 'starting' ? (
            <p className="suggestion-task__status" role="status">
              {copy.starting}
            </p>
          ) : null}
          {phase === 'dismissed' ? (
            <p className="suggestion-task__status">{copy.dismissed}</p>
          ) : null}
          {failure && (phase === 'actionable' || phase === 'start_failed') ? (
            <p className="suggestion-task__alert" role="alert">
              {failureText(failure)}
            </p>
          ) : null}
          {(phase === 'actionable' || dismissed) && approvalMode.errorKey ? (
            <p className="suggestion-task__alert" role="alert">
              {t(approvalMode.errorKey)}
            </p>
          ) : null}
        </div>
      </div>
      <div className="suggestion-task__dock">
        {decides || phase === 'actionable' || dismissed ? (
          <OverlayComposer
            approvalMode={approvalMode}
            draft={draft.draft}
            mentions={draft.mentions}
            submissionControls={
              draft.submission ? (
                <ComposerSubmissionStatus
                  composer={draft}
                  controlRef={submissionControlRef}
                  onRetry={composer.retrySubmission}
                  onRefresh={(button) => void composer.refreshSubmission(button)}
                />
              ) : null
            }
            retryAcceptance={failure?.stage === 'accept_failed'}
            attachments={draft.attachments}
            attachmentFailure={draft.attachmentFailure}
            validationFailed={decides ? composer.supplementInvalid : draft.validationFailed}
            canAttach={composer.canAttach}
            action={
              decides
                ? {
                    decision: (
                      <SuggestionDecisionButtons
                        canDismiss={task.canDismiss}
                        canAccept={task.canAccept}
                        onDismiss={task.dismiss}
                        onAccept={() => void task.accept()}
                      />
                    ),
                  }
                : 'send'
            }
            canSend={task.canReply}
            resumeFailed={false}
            canResume={false}
            textareaRef={textareaRef}
            placeholder={dismissed ? t('overlay.composer.dismissedPlaceholder') : undefined}
            onAddProject={onAddProject}
            onDraftChange={(value, mentions) =>
              setComposer((current) => ({
                ...current,
                draft: value,
                mentions,
                validationFailed: false,
              }))
            }
            onAttachFiles={(files) => void composer.attachFiles(files)}
            onRemoveAttachment={composer.removeAttachment}
            onSubmit={task.reply}
            onStop={noop}
            onResume={noop}
          />
        ) : null}
      </div>
    </section>
  );
}
