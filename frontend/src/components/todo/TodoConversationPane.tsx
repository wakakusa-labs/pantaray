import { useId, useState, type ReactNode } from 'react';

import { SuggestionTaskPane } from '@/components/task/SuggestionTaskPane';
import { TaskWorkspace } from '@/components/task/TaskWorkspace';
import type { TaskPreview } from '@/components/task/taskFiles';
import { useI18n } from '@/context/useI18n';

import type { TodoView } from '../../../electron/src/todos/todoContracts';

/**
 * A TODO's conversation beside the list: its Action, its suggestion while no Action answers it
 * yet, or a short note while it has neither. Under it, like a suggestion's 承認, Pantaray's pending
 * request to delete the TODO, which only the user's 「消す」 carries out. A TODO's suggestion is let go by deleting the
 * TODO, so its pane does not offer 見送る. The conversation itself is the task panes' to hold.
 */
export function TodoConversationPane({
  todo,
  pending,
  deletionFailure,
  onAnswerDeletion,
  onAddProject,
}: {
  todo: TodoView;
  /** A write on the TODO is waiting for its answer; the request's buttons wait with it. */
  pending: boolean;
  /** Why the last answer to the request failed, told under it. */
  deletionFailure: ReactNode;
  onAnswerDeletion: (requestId: string, answer: 'approve' | 'keep') => void;
  onAddProject: () => void;
}) {
  const request = todo.deletion_request;
  return (
    <>
      <Conversation todo={todo} onAddProject={onAddProject} />
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

function Conversation({ todo, onAddProject }: { todo: TodoView; onAddProject: () => void }) {
  const { t } = useI18n();
  const titleId = useId();
  // An accepted suggestion's Action shows at once; the week names it once it is read again.
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
        initialPreview={started?.actionId === actionId ? started.preview : null}
        onAddProject={onAddProject}
      />
    );
  if (conversation.kind === 'suggestion')
    return (
      <SuggestionTaskPane
        key={conversation.suggestion_id}
        suggestionId={conversation.suggestion_id}
        title={todo.text}
        requestedFile={null}
        canDismiss={false}
        onStarted={(id, preview) => setStarted({ actionId: id, preview })}
        onAddProject={onAddProject}
      />
    );
  return (
    <section className="todo-conversation-none" aria-labelledby={titleId}>
      <header className="history-pane-header">
        <h1 id={titleId}>{todo.text}</h1>
      </header>
      <p className="todo-conversation-none__text">{t('todo.conversation.none')}</p>
    </section>
  );
}
