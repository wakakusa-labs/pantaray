import { FileInput } from 'lucide-react';

import { useI18n } from '@/context/useI18n';

import type { OpenTodoImport } from '../../../electron/src/todos/todoImportContracts';
import { momentMonthDay } from './todoDates';

import './openTodoImports.css';

/**
 * The imports still being drafted, one line each over the TODOs: the add bar's mark, what the
 * user sent, that it is a draft, and the day it started. A line opens its draft beside the list.
 */
export function OpenTodoImportRows({
  imports,
  openImportId,
  onOpen,
}: {
  /** Newest first. */
  imports: readonly OpenTodoImport[];
  /** The import whose draft is open beside the list. */
  openImportId: string | null;
  onOpen: (importId: string) => void;
}) {
  const { t } = useI18n();
  if (imports.length === 0) return null;
  return (
    <ul className="todo-open-imports" aria-label={t('todo.openImports.label')}>
      {imports.map((item) => (
        <li key={item.import_id}>
          <button
            type="button"
            className="todo-open-import"
            aria-label={t('todo.openImports.open', { text: item.request })}
            aria-current={item.import_id === openImportId ? 'true' : undefined}
            onClick={() => onOpen(item.import_id)}
          >
            <FileInput className="todo-open-import__mark" size={14} aria-hidden="true" />
            <span className="todo-open-import__text">{item.request}</span>
            <span className="todo-open-import__state">{t('todo.openImports.state')}</span>
            <span className="todo-open-import__day">{momentMonthDay(item.started_at)}</span>
          </button>
        </li>
      ))}
    </ul>
  );
}
