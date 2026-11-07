import { useId, useState } from 'react';

import { SuggestionTaskPane } from '@/components/task/SuggestionTaskPane';
import { TaskWorkspace } from '@/components/task/TaskWorkspace';
import type { TaskPreview } from '@/components/task/taskFiles';
import { useI18n } from '@/context/useI18n';

import type { TodoView } from '../../../electron/src/todos/todoContracts';

/**
 * A TODO's conversation beside the list: its Action, its suggestion while no Action answers it
 * yet, or a short note while it has neither. A TODO's suggestion is let go by deleting the TODO,
 * so its pane does not offer 見送る. The conversation itself is the task panes' to hold.
 */
export function TodoConversationPane({
  todo,
  onAddProject,
}: {
  todo: TodoView;
  onAddProject: () => void;
}) {
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
