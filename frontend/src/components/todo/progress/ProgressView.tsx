import { useMemo } from 'react';

import { useI18n } from '@/context/useI18n';

import type { TodoProgressPage } from '../../../../electron/src/todos/todoContracts';
import { GoalProgress } from './GoalProgress';
import { MonthCalendar, YearCalendar } from './ProgressCalendar';
import { spanOf } from './progressPages';
import { indexRecords, leadSentence, type ProjectNameOf } from './progressText';

import './progress.css';

/**
 * The progress of the TODOs in view on one month or year: one sentence on now (or on the page),
 * a row per goal, and the calendar. Every number on it is a count, sum or ratio of the records
 * and TODOs the page carries, and a day opens onto them.
 */
export function ProgressView({
  page,
  projectName,
  selectedDay,
  onSelectDay,
  onSelectMonth,
  onSetGoal,
}: {
  page: TodoProgressPage;
  projectName: ProjectNameOf;
  selectedDay: string | null;
  onSelectDay: (day: string) => void;
  /** Opens a month of the year as its calendar. */
  onSelectMonth: (monthStart: string) => void;
  /** Opens the goal form for the project in view, which has no goal yet. */
  onSetGoal: (projectId: string) => void;
}) {
  const i18n = useI18n();
  const recordsById = useMemo(() => indexRecords(page.records), [page.records]);
  // With one project's records only, a line need not say whose it is.
  const severalProjects = new Set(page.records.map((record) => record.project_id)).size > 1;
  const calendarProps = {
    page,
    recordsById,
    projectName: severalProjects ? projectName : null,
  };
  return (
    <div className="todo-progress">
      <p className="todo-progress__lead" aria-live="polite">
        {leadSentence(i18n, page, recordsById, projectName)}
      </p>
      {page.goals.length > 0 ? (
        <div className="todo-progress__goals">
          {page.goals.map((progress) => {
            const projectId =
              progress.kind === 'no_goal' ? progress.project_id : progress.goal.project_id;
            return (
              <GoalProgress
                key={projectId}
                progress={progress}
                projectName={projectName(projectId)}
                today={page.today}
                recordsById={recordsById}
                onSetGoal={() => onSetGoal(projectId)}
              />
            );
          })}
        </div>
      ) : null}
      {spanOf(page) === 'month' ? (
        <MonthCalendar {...calendarProps} selectedDay={selectedDay} onSelectDay={onSelectDay} />
      ) : (
        <YearCalendar {...calendarProps} onSelectMonth={onSelectMonth} />
      )}
    </div>
  );
}
