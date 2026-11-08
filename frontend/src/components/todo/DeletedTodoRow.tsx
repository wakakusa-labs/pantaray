import { useI18n } from '@/context/useI18n';

import type { DeletedTodoView } from '../../../electron/src/todos/todoContracts';
import { daysFrom, momentMonthDay } from './todoDates';

import './todo.css';

/** One deleted TODO, which can be restored until its `purge_on`. */
export function DeletedTodoRow({
  item,
  projectName,
  today,
  pending,
  onRestore,
}: {
  item: DeletedTodoView;
  /** Null for a TODO in no project. */
  projectName: string | null;
  /** The backend's today, from which the days left are counted. */
  today: string;
  pending: boolean;
  onRestore: () => void;
}) {
  const { t } = useI18n();
  const deletedOn = momentMonthDay(item.deleted_at);
  const daysLeft = daysFrom(today, item.purge_on);
  return (
    <li className="todo-deleted">
      <div className="todo-deleted__body">
        <span className="todo-deleted__text">{item.text}</span>
        <span className="todo-deleted__meta">
          <span>{projectName ?? t('todo.noProject')}</span>
          <span>
            {t(item.deleted_by === 'user' ? 'todo.deleted.byUser' : 'todo.deleted.byAi', {
              date: deletedOn,
            })}
          </span>
          <span>
            {daysLeft === 1
              ? t('todo.deleted.lastDay')
              : t('todo.deleted.daysLeft', { days: daysLeft })}
          </span>
        </span>
      </div>
      <button
        type="button"
        className="history-filter-button"
        aria-label={t('todo.deleted.restoreNamed', { text: item.text })}
        disabled={pending}
        onClick={onRestore}
      >
        {t('todo.deleted.restore')}
      </button>
    </li>
  );
}
