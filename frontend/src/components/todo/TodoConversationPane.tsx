import { useContext, useId, useState, type ReactNode } from 'react';

import { OpenWorkContext } from '@/components/action-conversation/sentMessageRefs';
import { useSchedules } from '@/components/schedules/useSchedules';
import { SuggestionTaskPane } from '@/components/task/SuggestionTaskPane';
import { TaskWorkspace } from '@/components/task/TaskWorkspace';
import type { TaskPreview } from '@/components/task/taskFiles';
import { useI18n } from '@/context/useI18n';

import type { TodoView } from '../../../electron/src/todos/todoContracts';

/**
 * A TODO's conversation beside the list, or in its place: its Action, its suggestion while no Action answers it
 * yet, or a short note while it has neither. Under it, Pantaray's pending request to delete the
 * TODO, which only the user's 「消す」 carries out. The conversation itself is the task panes' to
 * hold; the page's controls (expand or shrink, close) sit at the end of whichever header it has.
 */
export function TodoConversationPane({
  todo,
  pending,
  deletionFailure,
  onAnswerDeletion,
  onAddProject,
  requestedFile,
  handedPreview,
  pageControls,
}: {
  todo: TodoView;
  /** A write on the TODO is waiting for its answer; the request's buttons wait with it. */
  pending: boolean;
  /** Why the last answer to the request failed, told under it. */
  deletionFailure: ReactNode;
  onAnswerDeletion: (requestId: string, answer: 'approve' | 'keep') => void;
  onAddProject: () => void;
  /** One of its suggestion's files to preview, asked for by an Overlay chip. */
  requestedFile: string | null;
  /** The file a suggestion had open, handed to the Action that took its place. */
  handedPreview: { actionId: string; preview: TaskPreview } | null;
  pageControls: ReactNode;
}) {
  const request = todo.deletion_request;
  return (
    <>
      <Conversation
        todo={todo}
        onAddProject={onAddProject}
        requestedFile={requestedFile}
        handedPreview={handedPreview}
        pageControls={pageControls}
      />
      {request ? (
        <DeletionRequest
          reason={request.reason}
          pending={pending}
          onAnswer={(answer) => onAnswerDeletion(request.request_id, answer)}
        />
      ) : null}
      {request ? deletionFailure : null}
    </>
  );
}

function DeletionRequest({
  reason,
  pending,
  onAnswer,
}: {
  reason: string;
  pending: boolean;
  onAnswer: (answer: 'approve' | 'keep') => void;
}) {
  const { t } = useI18n();
  const titleId = useId();
  return (
    <div className="todo-deletion" role="group" aria-labelledby={titleId}>
      <p className="todo-deletion__text">
        <span id={titleId} className="todo-deletion__title">
          {t('todo.deletionRequest.title')}
        </span>
        {reason}
      </p>
      <button
        type="button"
        className="history-filter-button todo-deletion__approve"
        disabled={pending}
        onClick={() => onAnswer('approve')}
      >
        {t('todo.deletionRequest.approve')}
      </button>
      <button
        type="button"
        className="history-filter-button"
        disabled={pending}
        onClick={() => onAnswer('keep')}
      >
        {t('todo.deletionRequest.keep')}
      </button>
    </div>
  );
}

function Conversation({
  todo,
  onAddProject,
  requestedFile,
  handedPreview,
  pageControls,
}: {
  todo: TodoView;
  onAddProject: () => void;
  requestedFile: string | null;
  handedPreview: { actionId: string; preview: TaskPreview } | null;
  pageControls: ReactNode;
}) {
  const { t } = useI18n();
  const titleId = useId();
  // An accepted suggestion's Action shows at once; the list names it once it is read again.
  const [started, setStarted] = useState<{ actionId: string; preview: TaskPreview | null } | null>(
    null
  );
  const { conversation } = todo;
  const actionId =
    conversation.kind === 'action' ? conversation.action_id : (started?.actionId ?? null);
  if (actionId !== null)
    return (
      <TaskWorkspace
        key={actionId}
        actionId={actionId}
        title={todo.text}
        initialPreview={
          started?.actionId === actionId
            ? started.preview
            : handedPreview?.actionId === actionId
              ? handedPreview.preview
              : null
        }
        onAddProject={onAddProject}
        pageControls={pageControls}
        conversationStart={
          todo.origin.kind === 'hook' ? (
            <HookStartLine askedIn={todo.origin.origin_action_id} />
          ) : null
        }
      />
    );
  if (conversation.kind === 'suggestion')
    return (
      <SuggestionTaskPane
        key={conversation.suggestion_id}
        suggestionId={conversation.suggestion_id}
        title={todo.text}
        requestedFile={requestedFile}
        onStarted={(id, preview) => setStarted({ actionId: id, preview })}
        onAddProject={onAddProject}
        pageControls={pageControls}
      />
    );
  return (
    <section className="todo-conversation-none" aria-labelledby={titleId}>
      <header className="action-task__header">
        <h1 id={titleId}>{todo.text}</h1>
        <div className="action-task__header-actions">{pageControls}</div>
      </header>
      <p className="todo-conversation-none__text">{t('todo.conversation.none')}</p>
    </section>
  );
}

/**
 * Why a schedule's conversation exists: the conversation the schedule was asked for in, named by
 * its TODO and opened from here. The schedules list names it while that conversation is there;
 * once it is gone, or has no TODO to name it, the line says only that a schedule started this.
 */
function HookStartLine({ askedIn }: { askedIn: string }) {
  const { t } = useI18n();
  const openWork = useContext(OpenWorkContext);
  const { list, loadFailed } = useSchedules();
  // Said once the list answers, so a name that arrives does not replace the plain line.
  if (list === null && !loadFailed) return null;
  const title =
    list?.hooks.find((hook) => hook.created_from_action_id === askedIn)?.created_from_title ?? null;
  if (title === null || openWork === null)
    return <p className="todo-hook-start">{t('todo.hookStart.untitled')}</p>;
  const [before, after] = t('todo.hookStart.askedIn').split('{title}');
  return (
    <p className="todo-hook-start">
      {before}
      <button
        type="button"
        className="todo-hook-start__open"
        aria-label={t('history.chat.workRef.open', { title })}
        onClick={() => openWork(`action:${askedIn}`)}
      >
        {title}
      </button>
      {after}
    </p>
  );
}
