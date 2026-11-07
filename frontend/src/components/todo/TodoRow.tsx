import { Repeat, Trash2 } from 'lucide-react';
import { useId } from 'react';

import type { UiLanguageContextValue } from '@/context/uiLanguageContextShared';
import { useI18n } from '@/context/useI18n';

import type { TodoDue, TodoState, TodoView } from '../../../electron/src/todos/todoContracts';
import { daysFrom, monthDay, momentMonthDay } from './todoDates';

import './todo.css';

type Translate = UiLanguageContextValue['t'];

/** A due date this close to today, or already past, is drawn in the warm tone. */
const SOON_WITHIN_DAYS = 2;

/** 「11/10 まで」. A done TODO's due date is only a date: it is no longer close to anything. */
function DueChip({ due, today, done }: { due: TodoDue; today: string; done: boolean }) {
  const { t } = useI18n();
  const soon = !done && daysFrom(today, due.date) <= SOON_WITHIN_DAYS;
  return (
    <span className={soon ? 'todo-due todo-due--soon' : 'todo-due'}>
      {due.repeat === 'weekly' ? (
        <span role="img" aria-label={t('todo.due.weekly')}>
          <Repeat size={12} aria-hidden="true" />
        </span>
      ) : null}
      {t('todo.due', { date: monthDay(due.date) })}
    </span>
  );
}

const withNote = (t: Translate, state: string, note: string | null) =>
  note ? t('todo.state.withNote', { state, note }) : state;

/** Where the TODO stands, in one short sentence. */
function stateSentence(t: Translate, state: TodoState): string {
  switch (state.kind) {
    case 'done':
      return t(state.by === 'user' ? 'todo.state.doneByUser' : 'todo.state.doneByAi', {
        date: momentMonthDay(state.at),
      });
    case 'working':
      return withNote(t, t('todo.state.working'), state.note);
    case 'waiting':
      return withNote(t, t('todo.state.waiting'), state.note);
    case 'partial':
      return withNote(t, t('todo.state.partial'), state.note);
    case 'stopped':
      return t(state.outcome === 'failed' ? 'todo.state.failed' : 'todo.state.canceled');
    case 'not_started':
      return t(state.reason === 'starting' ? 'todo.state.starting' : 'todo.state.weekNotStarted');
  }
}

/** Who wrote it: a weekly TODO's next instance is the week's, not a person's. */
function originText(t: Translate, todo: TodoView): string {
  if (todo.origin === 'recurrence') return t('todo.origin.recurrence');
  return t(todo.author === 'user' ? 'todo.origin.user' : 'todo.origin.ai');
}

/**
 * One TODO: its check, its text and meta line (a button that opens its conversation), its due
 * date, its delete button, and Pantaray's pending request to delete it. A checked TODO stays in
 * its place, struck through. The rows go in a list.
 */
export function TodoRow({
  todo,
  today,
  selected,
  pending,
  onToggleCheck,
  onSelect,
  onDelete,
  onAnswerDeletion,
}: {
  todo: TodoView;
  /** The backend's today, which decides how close the due date is. */
  today: string;
  selected: boolean;
  /** A write on this row is waiting for its answer; its controls wait with it. */
  pending: boolean;
  onToggleCheck: () => void;
  onSelect: () => void;
  onDelete: () => void;
  onAnswerDeletion: (answer: 'approve' | 'keep') => void;
}) {
  const { t } = useI18n();
  const deletionTitleId = useId();
  const done = todo.state.kind === 'done';
  return (
    <li className={done ? 'todo-row todo-row--done' : 'todo-row'}>
      {/* Named with the text so each row's check is told apart; the backend's answer checks it. */}
      <input
        type="checkbox"
        className="todo-check"
        aria-label={t('todo.check', { text: todo.text })}
        checked={done}
        disabled={pending}
        onChange={onToggleCheck}
      />
      <button
        type="button"
        className="todo-row__body"
        aria-current={selected ? 'true' : undefined}
        onClick={onSelect}
      >
        <span className="todo-row__text">{todo.text}</span>
        <span className="todo-row__meta">
          {todo.carried ? <span>{t('todo.origin.carried')}</span> : null}
          <span>{originText(t, todo)}</span>
          <span className={`todo-row__state todo-row__state--${todo.state.kind}`}>
            {stateSentence(t, todo.state)}
          </span>
        </span>
      </button>
      <div className="todo-row__end">
        {todo.due ? <DueChip due={todo.due} today={today} done={done} /> : null}
        <button
          type="button"
          className="todo-row__delete"
          aria-label={t('todo.deleteNamed', { text: todo.text })}
          data-tooltip={t('todo.delete')}
          disabled={pending}
          onClick={onDelete}
        >
          <Trash2 size={15} aria-hidden="true" />
        </button>
      </div>
      {/* Only the user's 「消す」 deletes the TODO Pantaray asked to delete. */}
      {todo.deletion_request ? (
        <div className="todo-deletion" role="group" aria-labelledby={deletionTitleId}>
          <p className="todo-deletion__text">
            <span id={deletionTitleId} className="todo-deletion__title">
              {t('todo.deletionRequest.title')}
            </span>
            {todo.deletion_request.reason}
          </p>
          <button
            type="button"
            className="history-filter-button todo-deletion__approve"
            disabled={pending}
            onClick={() => onAnswerDeletion('approve')}
          >
            {t('todo.deletionRequest.approve')}
          </button>
          <button
            type="button"
            className="history-filter-button"
            disabled={pending}
            onClick={() => onAnswerDeletion('keep')}
          >
            {t('todo.deletionRequest.keep')}
          </button>
        </div>
      ) : null}
    </li>
  );
}
