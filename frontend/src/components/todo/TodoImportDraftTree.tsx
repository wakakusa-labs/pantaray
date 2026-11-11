import { X } from 'lucide-react';

import { isImeKey } from '@/components/ui/imeKey';
import { useI18n } from '@/context/useI18n';
import type {
  DraftGroupView,
  DraftTodoAt,
  DraftTodoPatch,
  DraftTodoView,
} from '@/todos/todoImportDraft';

import { TodoDatePicker } from './TodoDatePicker';
import { dueDay } from './todoDates';

/**
 * An import's draft as a tree: organization (when every organization is in view), project, and
 * its TODOs, each name marked 新規 when registering creates it. A TODO that repeats an open one
 * is marked 「すでにある」 and is left out unless kept. Each TODO's text, due day and start
 * change here, and it can be taken out; every change is the caller's to write.
 */
export function TodoImportDraftTree({
  groups,
  showOrganizations,
  fieldsKey,
  today,
  disabled,
  onEdit,
  onRemove,
}: {
  groups: readonly DraftGroupView[];
  showOrganizations: boolean;
  /** A new key resets the text fields to the draft they show: a new revision, or an edit let go. */
  fieldsKey: string;
  today: string;
  disabled: boolean;
  onEdit: (at: DraftTodoAt, patch: DraftTodoPatch) => void;
  onRemove: (at: DraftTodoAt) => void;
}) {
  const { t } = useI18n();
  // Groups of one organization come one after another; each run is drawn under its name.
  const sections: { group: DraftGroupView; index: number }[][] = [];
  groups.forEach((group, index) => {
    const last = sections[sections.length - 1];
    if (last && last[0].group.organization?.name === group.organization?.name)
      last.push({ group, index });
    else sections.push([{ group, index }]);
  });
  return (
    <fieldset
      className={
        showOrganizations
          ? 'todo-import__tree todo-import__tree--organizations'
          : 'todo-import__tree'
      }
      disabled={disabled}
    >
      <legend className="action-task__sr-only">{t('todo.import.draft')}</legend>
      {sections.map((section) => {
        const { organization } = section[0].group;
        return (
          <div key={section[0].index} className="todo-import__organization">
            {showOrganizations ? (
              <Name
                level="organization"
                name={organization?.name ?? t('todo.rail.none')}
                isNew={organization?.new ?? false}
              />
            ) : null}
            {section.map(({ group, index }) => (
              <div key={index} className="todo-import__project">
                <Name
                  level="project"
                  name={group.project?.name ?? t('todo.noProject')}
                  isNew={group.project?.new ?? false}
                />
                <ul className="todo-import__todos">
                  {group.todos.map((todo, todoIndex) => (
                    <DraftRow
                      key={`${fieldsKey}:${todoIndex}`}
                      todo={todo}
                      at={{ group: index, todo: todoIndex }}
                      today={today}
                      disabled={disabled}
                      onEdit={onEdit}
                      onRemove={onRemove}
                    />
                  ))}
                </ul>
              </div>
            ))}
          </div>
        );
      })}
    </fieldset>
  );
}

function Name({
  level,
  name,
  isNew,
}: {
  level: 'organization' | 'project';
  name: string;
  isNew: boolean;
}) {
  const { t } = useI18n();
  const Heading = level === 'organization' ? 'h3' : 'h4';
  return (
    <Heading className={`todo-import__name todo-import__name--${level}`}>
      <span className="todo-import__name-text">{name}</span>
      {isNew ? <span className="todo-import__mark">{t('todo.import.new')}</span> : null}
    </Heading>
  );
}

function DraftRow({
  todo,
  at,
  today,
  disabled,
  onEdit,
  onRemove,
}: {
  todo: DraftTodoView;
  at: DraftTodoAt;
  today: string;
  /** The tree is closed to edits; the native fieldset closes the controls. */
  disabled: boolean;
  onEdit: (at: DraftTodoAt, patch: DraftTodoPatch) => void;
  onRemove: (at: DraftTodoAt) => void;
}) {
  const { t, language } = useI18n();
  // A blank text is no TODO: the field goes back to the draft's text instead.
  const commit = (field: HTMLInputElement) => {
    // A field closed while it kept focus blurs later; its edit is the unsaved one, not a new one.
    if (disabled) return;
    const text = field.value.trim();
    if (text === '') field.value = todo.text;
    else if (text !== todo.text) onEdit(at, { text });
  };
  const leftOut = todo.duplicate && !todo.keep_duplicate;
  return (
    <li className={leftOut ? 'todo-import__todo todo-import__todo--left-out' : 'todo-import__todo'}>
      <input
        type="text"
        className="todo-import__text"
        aria-label={t('todo.import.text')}
        defaultValue={todo.text}
        onBlur={(event) => commit(event.currentTarget)}
        onKeyDown={(event) => {
          if (event.key === 'Escape') event.currentTarget.value = todo.text;
          if (event.key !== 'Enter' || isImeKey(event)) return;
          event.preventDefault();
          commit(event.currentTarget);
        }}
      />
      {todo.duplicate ? (
        <span className="todo-import__duplicate">
          <span className="todo-import__mark">{t('todo.import.duplicate')}</span>
          <label>
            <input
              type="checkbox"
              checked={todo.keep_duplicate}
              onChange={(event) => onEdit(at, { keep_duplicate: event.target.checked })}
            />
            {t('todo.import.keepDuplicate')}
          </label>
        </span>
      ) : null}
      <TodoDatePicker
        label={t('todo.date.due')}
        triggerLabel={
          todo.due
            ? t('todo.row.due', { date: dueDay(language, todo.due), text: todo.text })
            : t('todo.row.noDue', { text: todo.text })
        }
        triggerClassName="todo-import__date"
        value={todo.due}
        today={today}
        clearLabel={t('todo.date.clearDue')}
        onChoose={(due) => onEdit(at, { due })}
      >
        {todo.due ? dueDay(language, todo.due) : t('todo.add.noDue')}
      </TodoDatePicker>
      <TodoDatePicker
        label={t('todo.date.start')}
        triggerLabel={
          todo.starts_on
            ? t('todo.import.start', { date: dueDay(language, todo.starts_on), text: todo.text })
            : t('todo.row.noStart', { text: todo.text })
        }
        triggerClassName="todo-import__date"
        value={todo.starts_on}
        today={today}
        clearLabel={t('todo.date.unsetStart')}
        onChoose={(startsOn) => onEdit(at, { starts_on: startsOn })}
      >
        {todo.starts_on
          ? t('todo.add.startsOn', { date: dueDay(language, todo.starts_on) })
          : t('todo.add.startsUnset')}
      </TodoDatePicker>
      <button
        type="button"
        className="todo-import__remove"
        aria-label={t('todo.import.remove', { text: todo.text })}
        data-tooltip={t('todo.import.remove', { text: todo.text })}
        onClick={() => onRemove(at)}
      >
        <X size={14} strokeWidth={1.8} aria-hidden="true" />
      </button>
    </li>
  );
}
