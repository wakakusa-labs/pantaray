import { Repeat, Trash2 } from 'lucide-react';

import { useI18n } from '@/context/useI18n';
import type { MessageKey } from '@/i18n/types';

import type { TodoView } from '../../../electron/src/todos/todoContracts';
import { dueDay } from './todoDates';

import './todo.css';

/**
 * How a state word is drawn: the user's turn in weight (a question without the dot), Pantaray's
 * work in grey, done faint.
 */
type StateTone = 'turn' | 'ask' | 'working' | 'quiet' | 'done';

/**
 * Where the TODO stands, in a word; the conversation beside the list tells the rest. `startsOn`
 * names the day a TODO not started yet starts.
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
      return state.reason === 'starting'
        ? { key: 'todo.state.starting', tone: 'quiet' }
        : { key: 'todo.state.startsOn', tone: 'quiet', startsOn: todo.starts_on };
  }
}

/**
 * One TODO on one line: its check, then a button that opens its conversation (its text, the
 * repeat mark, its state in a word, its due day), then its delete button. A checked TODO stays
 * in its place, struck through. The rows go in a list.
 */
export function TodoRow({
  todo,
  today,
  selected,
  pending,
  onToggleCheck,
  onSelect,
  onDelete,
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
}) {
  const { t, language } = useI18n();
  const done = todo.state.kind === 'done';
  const state = stateWord(todo);
  const late = !done && todo.due !== null && todo.due.date < today;
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
        <span className={`todo-row__state todo-row__state--${state.tone}`}>
          {state.startsOn === undefined
            ? t(state.key)
            : t(state.key, { date: dueDay(language, state.startsOn) })}
        </span>
        <span className={late ? 'todo-row__due todo-row__due--late' : 'todo-row__due'}>
          {todo.due ? dueDay(language, todo.due.date) : null}
        </span>
      </button>
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
