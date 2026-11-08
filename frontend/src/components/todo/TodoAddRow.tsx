import { Plus } from 'lucide-react';
import { useState } from 'react';

import { LineTextField } from '@/components/ui/LineTextField';
import { useI18n } from '@/context/useI18n';
import type { TodoProjectSlot } from '@/todos/groupTodosByProject';

import type { TodoDue, TodoWeekPage } from '../../../electron/src/todos/todoContracts';
import { dueDay } from './todoDates';

import './todo.css';

const DAY_MS = 86_400_000;

/** The week's days from today on (all of them for a week ahead), as YYYY-MM-DD. */
function dueChoices(page: TodoWeekPage): string[] {
  const first = page.today > page.week.start ? page.today : page.week.start;
  const days: string[] = [];
  for (let at = Date.parse(`${first}T00:00:00Z`); ; at += DAY_MS) {
    const day = new Date(at).toISOString().slice(0, 10);
    if (day > page.week.end) return days;
    days.push(day);
  }
}

/**
 * The one line above the week's groups that adds a TODO: its text, the project it goes in
 * (one of the projects in view) and its due day. The text empties once the TODO was taken; the
 * project and the due stay for the next one.
 */
export function TodoAddRow({
  page,
  projects,
  onSubmit,
}: {
  page: TodoWeekPage;
  /** Not empty: a scope with no project takes no TODO. */
  projects: readonly TodoProjectSlot[];
  onSubmit: (projectId: string | null, text: string, due: TodoDue | null) => Promise<boolean>;
}) {
  const { t, language } = useI18n();
  const [chosenProject, setChosenProject] = useState(projects[0].projectId ?? '');
  const [chosenDue, setChosenDue] = useState('');
  // A project gone from view, or a day gone by, falls back to the first choice.
  const project = projects.find((slot) => (slot.projectId ?? '') === chosenProject) ?? projects[0];
  const days = dueChoices(page);
  const due = days.includes(chosenDue) ? chosenDue : '';
  return (
    <div className="todo-add">
      <Plus className="todo-add__plus" size={16} aria-hidden="true" />
      <LineTextField
        className="todo-add__field"
        label={t('todo.add')}
        onSubmit={(text) =>
          onSubmit(project.projectId, text, due ? { date: due, repeat: 'none' } : null)
        }
      />
      <span className="todo-add__choice">
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
      <span className="todo-add__choice">
        <select
          aria-label={t('todo.add.due')}
          value={due}
          onChange={(event) => setChosenDue(event.target.value)}
        >
          <option value="">{t('todo.add.noDue')}</option>
          {days.map((day) => (
            <option key={day} value={day}>
              {dueDay(language, day)}
            </option>
          ))}
        </select>
      </span>
    </div>
  );
}
