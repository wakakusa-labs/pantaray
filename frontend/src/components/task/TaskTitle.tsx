import { TodoPlace } from '@/components/todo/TodoPlace';
import type { TodoPlaceName } from '@/todos/groupTodosByProject';

import './actionTaskPane.css';

/**
 * A task pane's title, and under it where its TODO sits; a conversation with no TODO, or one
 * whose place is not read yet, has the title alone.
 */
export function TaskTitle({
  id,
  title,
  place,
}: {
  id: string;
  title: string;
  place?: TodoPlaceName | null;
}) {
  return (
    <div className="task-title">
      <h1 id={id}>{title}</h1>
      {place ? <TodoPlace place={place} className="task-title__place" /> : null}
    </div>
  );
}
