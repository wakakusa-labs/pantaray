import { useEffect, useId, useRef } from 'react';

import {
  ComposerSubmissionStatus,
  OverlayComposer,
} from '@/components/agent-overlay/OverlayComposer';
import { useActionApprovalMode } from '@/components/agent-overlay/useActionApprovalMode';
import { useOverlayComposerController } from '@/components/agent-overlay/useOverlayComposerController';
import { useI18n } from '@/context/useI18n';

import './actionTaskPane.css';
import './suggestionTaskPane.css';

const noop = () => undefined;

/**
 * A new task in the main window's detail pane: an empty composer whose first send opens a new
 * Action, as the New task window's does, and then hands the pane to that Action.
 */
export function NewTaskPane({
  onStarted,
  onAddProject,
}: {
  /** Called once the send has opened its Action. */
  onStarted: (actionId: string) => void;
  /** The composer's @-mention "Add project" option. */
  onAddProject: () => void;
}) {
  const { language, t } = useI18n();
  const titleId = useId();
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const submissionControlRef = useRef<HTMLButtonElement>(null);
  const actions = window.electron?.actions;
  const controller = useOverlayComposerController({
    actions,
    initialActionId: null,
    suggestionId: null,
    suggestionAccepted: false,
    language,
    // Refreshing belongs to a conversation; once the send opens one, the Action pane shows it.
    onRefreshedPage: noop,
    // The unsent draft stays with the owner's new task while the pane is away.
    newTask: true,
  });
  const approvalMode = useActionApprovalMode(null, null);
  const { composer, setComposer } = controller;
  const actionId = composer.initialActionId;

  const onStartedRef = useRef(onStarted);
  useEffect(() => {
    onStartedRef.current = onStarted;
  });
  useEffect(() => {
    if (actionId !== null) onStartedRef.current(actionId);
  }, [actionId]);

  const canSend =
    approvalMode.mode !== null &&
    !approvalMode.isSaving &&
    composer.submission === null &&
    composer.draft.trim() !== '' &&
    composer.attachmentsInFlight === 0;
  const send = () => {
    if (canSend) controller.submitDraft(null, true, null, 0, approvalMode.mode, null);
  };

  return (
    <section className="suggestion-task" aria-labelledby={titleId}>
      <header className="suggestion-task__header">
        <h1 id={titleId}>{t('history.newConversation')}</h1>
      </header>
      <div className="suggestion-task__scroll">
        {approvalMode.errorKey ? (
          <div className="suggestion-task__column">
            <p className="suggestion-task__alert" role="alert">
              {t(approvalMode.errorKey)}
            </p>
          </div>
        ) : null}
      </div>
      <div className="suggestion-task__dock">
        <OverlayComposer
          approvalMode={approvalMode}
          draft={composer.draft}
          mentions={composer.mentions}
          submissionControls={
            composer.submission ? (
              <ComposerSubmissionStatus
                composer={composer}
                controlRef={submissionControlRef}
                onRetry={controller.retrySubmission}
                onRefresh={(button) => void controller.refreshSubmission(button)}
              />
            ) : null
          }
          retryAcceptance={false}
          attachments={composer.attachments}
          attachmentFailure={composer.attachmentFailure}
          validationFailed={composer.validationFailed}
          canAttach={controller.canAttach}
          action="send"
          canSend={canSend}
          resumeFailed={false}
          sendSuperseded={composer.failureKind === 'superseded'}
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
          onAttachFiles={(files) => void controller.attachFiles(files)}
          onRemoveAttachment={controller.removeAttachment}
          onSubmit={send}
          onStop={noop}
          onResume={noop}
        />
      </div>
    </section>
  );
}
