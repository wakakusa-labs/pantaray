import { Repeat, Trash2 } from 'lucide-react';

import { useI18n } from '@/context/useI18n';
import type { MessageKey } from '@/i18n/types';
import type { TodoDatesChange } from '@/todos/useTodoMutations';

import type { TodoView } from '../../../electron/src/todos/todoContracts';
import { TodoDateRangePicker } from './TodoDateRangePicker';
import { dueDay, type TodoDates } from './todoDates';

import './todo.css';

/**
 * How a state word is drawn: the user's turn in weight (a question without the dot), Pantaray's
 * work in grey, done faint.
 */
type StateTone = 'turn' | 'ask' | 'working' | 'quiet' | 'done';

/**
 * Where the TODO stands, in a word; the conversation beside the list tells the rest. `startsOn`
 * names the day a TODO scheduled for later starts.
 */
function stateWord(todo: TodoView): { key: MessageKey; tone: StateTone; startsOn?: string } {
  // Pantaray's request to delete waits for the user, whatever the work's own state.
  if (todo.deletion_request) return { key: 'todo.state.deletionRequested', tone: 'ask' };
  const { state } = todo;
  switch (state.kind) {
    case 'done':
      return { key: 'todo.state.done', tone: 'done' };
    case 'waiting':
      return { key: 'todo.state.waiting', tone: 'turn' };
    case 'working':
      return { key: 'todo.state.working', tone: 'working' };
    case 'partial':
      return { key: 'todo.state.partial', tone: 'quiet' };
    case 'stopped':
      return {
        key: state.outcome === 'failed' ? 'todo.state.failed' : 'todo.state.canceled',
        tone: 'quiet',
      };
    case 'not_started':
      if (state.reason === 'starting') return { key: 'todo.state.starting', tone: 'quiet' };
      return todo.starts_on === null
        ? { key: 'todo.state.unscheduled', tone: 'quiet' }
        : { key: 'todo.state.startsOn', tone: 'quiet', startsOn: todo.starts_on };
  }
}

/**
 * One TODO on one line: its check, a button that opens its conversation (its text and repeat
 * mark), its state in a word, its due day, then its delete button. The due day, and the state of
 * a TODO not started yet, open its start and due day on one calendar; once Pantaray started, only
 * the due day changes. A checked TODO stays in its place, struck through. The rows go in a list.
 */
export function TodoRow({
  todo,
  today,
  selected,
  pending,
  onToggleCheck,
  onSelect,
  onDelete,
  onSetDates,
}: {
  todo: TodoView;
  /** The backend's today, which decides whether the due day has passed. */
  today: string;
  selected: boolean;
  /** A write on this row is waiting for its answer; its controls wait with it. */
  pending: boolean;
  onToggleCheck: () => void;
  onSelect: () => void;
  onDelete: () => void;
  onSetDates: (change: TodoDatesChange) => void;
}) {
  const { t, language } = useI18n();
  const done = todo.state.kind === 'done';
  const state = stateWord(todo);
  const stateText =
    state.startsOn === undefined
      ? t(state.key)
      : t(state.key, { date: dueDay(language, state.startsOn) });
  const notStarted = todo.deletion_request === null && todo.state.kind === 'not_started';
  const stateClass = `todo-row__state todo-row__state--${state.tone}`;
  const late = !done && todo.due !== null && todo.due.date < today;
  const dueText = todo.due ? dueDay(language, todo.due.date) : t('todo.add.noDue');
  const dates = { start: todo.starts_on, due: todo.due?.date ?? null };
  // Only what changed is written; a new due day keeps the TODO's repeat.
  const setDates = (next: TodoDates) => {
    const due = next.due === null ? null : { date: next.due, repeat: todo.due?.repeat ?? 'none' };
    if (next.start === dates.start) onSetDates({ due });
    else if (next.due === dates.due) onSetDates({ startsOn: next.start });
    else onSetDates({ startsOn: next.start, due });
  };
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
        <span className="todo-row__title">
          <span className="todo-row__text">{todo.text}</span>
          {todo.due?.repeat === 'weekly' ? (
            <Repeat
              className="todo-row__repeat"
              size={13}
              role="img"
              aria-label={t('todo.due.weekly')}
            />
          ) : null}
        </span>
      </button>
      {/* The start matters until Pantaray starts. */}
      {notStarted ? (
        <TodoDateRangePicker
          triggerLabel={
            todo.starts_on === null
              ? t('todo.row.noStart', { text: todo.text })
              : t('todo.row.start', { state: stateText, text: todo.text })
          }
          triggerClassName={`todo-row__date ${stateClass}`}
          dates={dates}
          today={today}
          opensOn="start"
          disabled={pending}
          onChange={setDates}
        >
          {stateText}
        </TodoDateRangePicker>
      ) : (
        <span className={stateClass}>{stateText}</span>
      )}
      <TodoDateRangePicker
        triggerLabel={
          todo.due
            ? t('todo.row.due', { date: dueText, text: todo.text })
            : t('todo.row.noDue', { text: todo.text })
        }
        triggerClassName={
          todo.due === null
            ? 'todo-row__date todo-row__due todo-row__due--none'
            : late
              ? 'todo-row__date todo-row__due todo-row__due--late'
              : 'todo-row__date todo-row__due'
        }
        dates={dates}
        today={today}
        opensOn="due"
        startFixed={!notStarted}
        disabled={pending}
        onChange={setDates}
      >
        {dueText}
      </TodoDateRangePicker>
      <button
        type="button"
        className="todo-row__delete"
        aria-label={t('todo.deleteNamed', { text: todo.text })}
        data-tooltip={t('todo.delete')}
        disabled={pending}
        onClick={onDelete}
      >
        <Trash2 size={14} aria-hidden="true" />
      </button>
    </li>
  );
}
