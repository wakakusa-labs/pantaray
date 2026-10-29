import { Check, CircleAlert, Clipboard, PictureInPicture2 } from 'lucide-react';
import { useEffect, useId, useRef, useState } from 'react';

import { DismissalReason } from '@/components/agent-overlay/DismissalReason';
import { MarkdownBlock } from '@/components/agent-overlay/MarkdownRenderer';
import { SuggestionDecisionButtons } from '@/components/agent-overlay/SuggestionDecisionButtons';
import { useClipboardCopy } from '@/components/agent-overlay/useClipboardCopy';
import { useCopyWorkLink } from '@/history/useCopyWorkLink';
import {
  ComposerSubmissionStatus,
  OverlayComposer,
} from '@/components/agent-overlay/OverlayComposer';
import { useI18n } from '@/context/useI18n';

import { FileChips } from './FileChips';
import { FilePreviewPane } from './FilePreviewPane';
import { taskFileAt, type TaskFile, type TaskPreview } from './taskFiles';
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
    openSmallFailed: 'Could not open this suggestion in a small window.',
  },
  ja: {
    loading: '提案を読み込んでいます',
    loadFailed: 'この提案を読み込めませんでした。',
    starting: '開始しています',
    dismissed: 'この提案は見送りました。',
    notStarted: '開始できませんでした。もう一度お試しください。',
    dismissFailed: 'この提案を見送れませんでした。もう一度お試しください。',
    copySuggestion: '提案をコピー',
    openSmallFailed: 'この提案を小さく開けませんでした。',
  },
} as const;

const noop = () => undefined;

type SuggestionTaskPaneProps = {
  suggestionId: string;
  /** The history row's title for this suggestion. */
  title: string;
  /** One of its files to preview, asked for by an Overlay chip; each new request opens it. */
  requestedFile: string | null;
  /**
   * Called once an Action exists for the suggestion: accepted, or replied to. The file open in
   * the preview goes with it, so the Action's pane taking over keeps it open.
   */
  onStarted: (actionId: string, preview: TaskPreview | null) => void;
  /** Opens the chat, as the task pane's header button does. */
  /** The composer's @-mention "Add project" option. */
  onAddProject: () => void;
};

/**
 * An unanswered suggestion in the main window's detail pane: its text, and a composer whose
 * 承認 / 見送る take what the user wrote, as the approval's extra instruction or as the reason for
 * the dismissal. A message-only suggestion is answered by a reply, which starts a new Action; so
 * is a dismissed one, if the user writes after all.
 */
export function SuggestionTaskPane({
  suggestionId,
  title,
  requestedFile,
  onStarted,
  onAddProject,
}: SuggestionTaskPaneProps) {
  const { language, t } = useI18n();
  const copy = COPY[language];
  const titleId = useId();
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const columnRef = useRef<HTMLDivElement>(null);
  const submissionControlRef = useRef<HTMLButtonElement>(null);
  const task = useSuggestionTask(suggestionId);
  const { snapshot, phase, failure, composer, approvalMode } = task;
  const { status: copyStatus, copy: copyToClipboard } = useClipboardCopy();
  const link = useCopyWorkLink('suggestion', suggestionId);
  const suggestionText = snapshot?.suggestionText ?? '';
  const { composer: draft, setComposer } = composer;
  const [openSmallFailed, setOpenSmallFailed] = useState(false);
  const [previewFile, setPreviewFile] = useState<TaskFile | null>(() =>
    requestedFile === null ? null : taskFileAt(requestedFile)
  );
  const [takenRequest, setTakenRequest] = useState(requestedFile);
  if (requestedFile !== takenRequest) {
    setTakenRequest(requestedFile);
    if (requestedFile !== null) setPreviewFile(taskFileAt(requestedFile));
  }
  const files = (snapshot?.artifacts ?? []).map(taskFileAt);
  // The small window shows it with its decision, as when it arrived; this pane stays.
  const openSmall = () => {
    setOpenSmallFailed(false);
    window.electron?.agentOverlay
      ?.showHistory?.({ suggestionId, initialUiState: { expand: true } })
      .catch((error: unknown) => {
        console.error('Failed to open the suggestion in a small window:', error);
        setOpenSmallFailed(true);
      });
  };

  const handOver = (actionId: string) =>
    onStarted(
      actionId,
      previewFile && { target: { kind: 'suggestion', id: suggestionId }, file: previewFile }
    );
  const handOverRef = useRef(handOver);
  useEffect(() => {
    handOverRef.current = handOver;
  });
  useEffect(() => {
    if (task.actionId !== null) handOverRef.current(task.actionId);
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
    <section
      className={previewFile ? 'suggestion-task suggestion-task--split' : 'suggestion-task'}
      aria-labelledby={titleId}
    >
      <header className="suggestion-task__header">
        <h1 id={titleId}>{title}</h1>
        <div className="action-task__header-actions">
          <button
            type="button"
            className="action-task__icon-button"
            aria-label={link.label}
            data-tooltip={link.title}
            onClick={link.copy}
          >
            <link.Icon size={17} strokeWidth={1.8} aria-hidden />
          </button>
          {link.failed ? (
            <span className="action-task__sr-only" role="alert">
              {link.failed}
            </span>
          ) : null}
          <button
            type="button"
            className="action-task__icon-button"
            aria-label={t('history.openSmall')}
            data-tooltip={t('history.openSmall')}
            disabled={snapshot === null}
            onClick={openSmall}
          >
            <PictureInPicture2 size={17} strokeWidth={1.8} aria-hidden />
          </button>
          <button
            type="button"
            className="action-task__icon-button"
            aria-label={copy.copySuggestion}
            data-tooltip={copy.copySuggestion}
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
          {openSmallFailed ? (
            <p className="action-task__state" role="alert">
              {copy.openSmallFailed}
            </p>
          ) : null}
        </div>
      </header>
      {previewFile ? (
        <FilePreviewPane
          key={previewFile.path}
          target={{ kind: 'suggestion', id: suggestionId }}
          file={previewFile}
          revision={null}
          onClose={() => setPreviewFile(null)}
        />
      ) : null}
      <div className="suggestion-task__scroll">
        <div className="suggestion-task__column" ref={columnRef}>
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
          <FileChips
            owner="suggestion"
            files={files}
            selectedPath={previewFile?.path ?? null}
            onOpen={(file) => setPreviewFile(file.path === previewFile?.path ? null : file)}
          />
          {phase === 'starting' ? (
            <p className="suggestion-task__status" role="status">
              {copy.starting}
            </p>
          ) : null}
          {phase === 'dismissed' ? (
            <>
              {snapshot?.dismissReason ? <DismissalReason reason={snapshot.dismissReason} /> : null}
              <p className="suggestion-task__status">{copy.dismissed}</p>
            </>
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
            quoting={{ ...composer.quoting, sourceRef: columnRef }}
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
            sendSuperseded={draft.failureKind === 'superseded'}
            canResume={false}
            textareaRef={textareaRef}
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
