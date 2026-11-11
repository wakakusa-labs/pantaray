import { ArrowUp } from 'lucide-react';
import { useState } from 'react';

import { isImeKey } from '@/components/ui/imeKey';
import { useI18n } from '@/context/useI18n';
import type { TodoProjectSlot } from '@/todos/groupTodosByProject';

import type { TodoDue } from '../../../electron/src/todos/todoContracts';
import { TodoDatePicker } from './TodoDatePicker';
import { dueDay } from './todoDates';

import './todo.css';

/**
 * The bar under the list that adds a TODO: its text, its project (while several are in view),
 * due day (none at first) and start (today at first, or none), and send. An IME's Enter does not send.
 * The text empties once the TODO was taken; the choices stay for the next one.
 */
export function TodoAddBar({
  today,
  projects,
  disabled,
  onSubmit,
}: {
  /** The backend's today. */
  today: string;
  /** Not empty: a scope with no project takes no TODO. */
  projects: readonly TodoProjectSlot[];
  /** The list on screen is not the one selected yet, so nothing is added to it. */
  disabled: boolean;
  onSubmit: (
    projectId: string | null,
    startsOn: string | null,
    text: string,
    due: TodoDue | null
  ) => Promise<boolean>;
}) {
  const { t, language } = useI18n();
  const [text, setText] = useState('');
  const [sending, setSending] = useState(false);
  const [chosenProject, setChosenProject] = useState(projects[0].projectId ?? '');
  const [due, setDue] = useState<string | null>(null);
  // 'today' is whichever day that is when the TODO is sent; a null date is no start.
  const [start, setStart] = useState<'today' | { date: string | null }>('today');
  // A project gone from view falls back to the first one.
  const project = projects.find((slot) => (slot.projectId ?? '') === chosenProject) ?? projects[0];
  const startsOn = start === 'today' ? today : start.date;

  const send = async () => {
    if (sending || !text.trim()) return;
    setSending(true);
    const sent = text;
    const taken = await onSubmit(
      project.projectId,
      startsOn,
      sent,
      due === null ? null : { date: due, repeat: 'none' }
    );
    // Text typed while the add was deciding is the next TODO, so only the sent text is cleared.
    if (taken) setText((current) => (current === sent ? '' : current));
    setSending(false);
  };

  return (
    <fieldset className="todo-add-bar" disabled={disabled}>
      <input
        type="text"
        className="todo-add-bar__field"
        aria-label={t('todo.add')}
        placeholder={t('todo.add')}
        value={text}
        onChange={(event) => setText(event.target.value)}
        onKeyDown={(event) => {
          if (event.key !== 'Enter' || isImeKey(event)) return;
          event.preventDefault();
          void send();
        }}
      />
      <div className="todo-add-bar__choices">
        {projects.length > 1 ? (
          <span className="todo-add-bar__project">
            <span className="todo-group__dot" aria-hidden="true" />
            <select
              aria-label={t('todo.add.project')}
              value={project.projectId ?? ''}
              onChange={(event) => setChosenProject(event.target.value)}
            >
              {projects.map((slot) => (
                <option key={slot.projectId ?? ''} value={slot.projectId ?? ''}>
                  {slot.projectName ?? t('todo.noProject')}
                </option>
              ))}
            </select>
          </span>
        ) : null}
        <TodoDatePicker
          label={t('todo.date.due')}
          triggerClassName="todo-add-bar__chip"
          value={due}
          today={today}
          clearLabel={t('todo.date.clearDue')}
          onChoose={setDue}
        >
          {due ? t('todo.add.due', { date: dueDay(language, due) }) : t('todo.add.noDue')}
        </TodoDatePicker>
        <TodoDatePicker
          label={t('todo.date.start')}
          triggerClassName="todo-add-bar__chip"
          value={startsOn}
          today={today}
          clearLabel={t('todo.date.unsetStart')}
          onChoose={(date) => setStart(date === today ? 'today' : { date })}
        >
          {startsOn === null
            ? t('todo.add.startsUnset')
            : startsOn === today
              ? t('todo.add.startsToday')
              : t('todo.add.startsOn', { date: dueDay(language, startsOn) })}
        </TodoDatePicker>
        <button
          type="button"
          className="todo-add-bar__send"
          aria-label={t('todo.add.send')}
          data-tooltip={t('todo.add.send')}
          disabled={sending || !text.trim()}
          onClick={() => void send()}
        >
          <ArrowUp size={15} strokeWidth={2.25} aria-hidden="true" />
        </button>
      </div>
    </fieldset>
  );
}
