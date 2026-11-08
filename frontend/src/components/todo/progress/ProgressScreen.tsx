import { ChevronLeft, ChevronRight, X } from 'lucide-react';
import { useRef, useState, type ReactNode } from 'react';
import { flushSync } from 'react-dom';

import { AiConnectionNotice } from '@/components/AiConnectionNotice';
import { useI18n } from '@/context/useI18n';

import type { ProgressSpan, TodoSelection } from '../../../todos/todoSelection';
import { useTodoProgress } from '../../../todos/useTodoProgress';
import { useTodoProgressMutations } from '../../../todos/useTodoProgressMutations';
import { GoalForm } from './GoalForm';
import { ProgressDayPane } from './ProgressDayPane';
import { ProgressView } from './ProgressView';
import { PERIOD_OF_SPAN, pageStartOf, shiftPage } from './progressPages';
import { formatPage, setGoalButtonId, type ProjectNameOf } from './progressText';

import './progress.css';

const SPANS: readonly ProgressSpan[] = ['month', 'year'];

/**
 * The TODO page's progress: the month or year it names, read for the TODOs in view, with the
 * day chosen beside it. It writes the records and the goal of a project without one, and each
 * record opens the conversation it came from. The page's layout and header are the TODO list's.
 */
export function ProgressScreen({
  selection,
  projectName,
  settingsFailed,
  onSelect,
  onOpenEvidence,
}: {
  selection: TodoSelection;
  /** Null until the workspace, which names the projects, is read. */
  projectName: ProjectNameOf | null;
  settingsFailed: boolean;
  onSelect: (next: TodoSelection) => void;
  /** Opens the conversation whose results a record came from. */
  onOpenEvidence: (actionId: string) => void;
}) {
  const i18n = useI18n();
  const { t } = i18n;
  const headingRef = useRef<HTMLHeadingElement>(null);
  const span = selection.progressSpan;
  const period = PERIOD_OF_SPAN[span];
  const progress = useTodoProgress(
    selection.scope,
    selection.project,
    period,
    selection.progressOn
  );
  const mutations = useTodoProgressMutations({ reloadProgress: progress.reload });
  const [goalFormFor, setGoalFormFor] = useState<string | null>(null);
  const { page } = progress;
  const { failure } = mutations;
  const focusHeading = () => headingRef.current?.focus();

  // The page asked for, which the one on screen may not be yet while it is read.
  const currentStart = page ? pageStartOf(span, page.today) : null;
  const shownStart = page ? pageStartOf(span, selection.progressOn ?? page.today) : null;
  const showsSelection = page !== null && page.period === period && page.page_start === shownStart;
  const dayRow =
    showsSelection && span === 'month'
      ? (page.rows.find((row) => row.start === selection.progressDay && row.start <= page.today) ??
        null)
      : null;
  // The page holding today is named by no date, so the address stays the same from day to day.
  const showPage = (nextSpan: ProgressSpan, on: string | null) => {
    const start = on === null ? null : pageStartOf(nextSpan, on);
    onSelect({
      ...selection,
      progressSpan: nextSpan,
      progressOn: page && start !== pageStartOf(nextSpan, page.today) ? start : null,
      progressDay: null,
    });
  };

  const renderControls = (): ReactNode => {
    if (!shownStart) return null;
    const [previous, next, current] =
      span === 'month'
        ? ([
            'todoProgress.page.previousMonth',
            'todoProgress.page.nextMonth',
            'todoProgress.page.currentMonth',
          ] as const)
        : ([
            'todoProgress.page.previousYear',
            'todoProgress.page.nextYear',
            'todoProgress.page.currentYear',
          ] as const);
    return (
      <>
        <span className="todo-screen__range">{formatPage(i18n, span, shownStart)}</span>
        <span className="todo-screen__steps">
          <button
            type="button"
            className="todo-screen__step"
            aria-label={t(previous)}
            data-tooltip={t(previous)}
            onClick={() => showPage(span, shiftPage(span, shownStart, -1))}
          >
            <ChevronLeft size={15} aria-hidden="true" />
          </button>
          <button
            type="button"
            className="todo-screen__step"
            aria-label={t(next)}
            data-tooltip={t(next)}
            onClick={() => showPage(span, shiftPage(span, shownStart, 1))}
          >
            <ChevronRight size={15} aria-hidden="true" />
          </button>
          {shownStart === currentStart ? null : (
            <button
              type="button"
              className="history-filter-button todo-screen__current"
              onClick={() => {
                showPage(span, null);
                focusHeading();
              }}
            >
              {t(current)}
            </button>
          )}
        </span>
        <span
          className="todo-progress__spans"
          role="group"
          aria-label={t('todoProgress.span.label')}
        >
          {SPANS.map((candidate) => (
            <button
              key={candidate}
              type="button"
              className="history-filter-button todo-progress__span"
              aria-pressed={candidate === span}
              onClick={() => showPage(candidate, shownStart)}
            >
              {t(candidate === 'month' ? 'todoProgress.span.month' : 'todoProgress.span.year')}
            </button>
          ))}
        </span>
      </>
    );
  };

  const renderPage = (): ReactNode => {
    if (!projectName)
      return (
        <p
          className={settingsFailed ? 'history-error' : 'history-loading'}
          role={settingsFailed ? 'alert' : 'status'}
        >
          {t(settingsFailed ? 'todoProgress.loadFailed' : 'todoProgress.loading')}
        </p>
      );
    if (!page) {
      return progress.error ? (
        <p className="history-error" role="alert">
          {t('todoProgress.loadFailed')}
        </p>
      ) : (
        <p className="history-loading" role="status">
          {t('todoProgress.loading')}
        </p>
      );
    }
    return (
      <>
        {/* What was read before the failure stays; the next change reads again. */}
        {progress.error ? (
          <p className="history-error" role="alert">
            {t('todoProgress.loadFailedStale')}
          </p>
        ) : null}
        {/* The last page, while the one asked for is read: closed to the pointer and keys. */}
        <fieldset
          className={
            showsSelection
              ? 'todo-progress__page'
              : 'todo-progress__page todo-progress__page--stale'
          }
          disabled={!showsSelection}
          aria-busy={!showsSelection && !progress.error}
        >
          <ProgressView
            page={page}
            projectName={projectName}
            selectedDay={dayRow?.start ?? null}
            onSelectDay={(day) =>
              onSelect({ ...selection, progressDay: day === dayRow?.start ? null : day })
            }
            onSelectMonth={(monthStart) => showPage('month', monthStart)}
            onSetGoal={setGoalFormFor}
          />
        </fieldset>
      </>
    );
  };

  // A modal dialog holds focus until it is gone, so it closes before focus moves.
  const closeGoalForm = (projectId: string) => {
    flushSync(() => setGoalFormFor(null));
    const button = document.getElementById(setGoalButtonId(projectId));
    if (button) button.focus();
    // The goal saved, its 「目標を設定」 is gone.
    else focusHeading();
  };

  return (
    <div className={dayRow ? 'todo-screen todo-screen--with-pane' : 'todo-screen'}>
      <section className="todo-screen__list" aria-labelledby="todo-screen-title">
        <header className="history-pane-header todo-screen__header">
          <h1 id="todo-screen-title" ref={headingRef} tabIndex={-1}>
            {t('todoProgress.title')}
          </h1>
          {renderControls()}
          {dayRow ? (
            <button
              type="button"
              className="action-task__icon-button"
              aria-label={t('todoProgress.day.close')}
              data-tooltip={t('todoProgress.day.close')}
              onClick={() => {
                onSelect({ ...selection, progressDay: null });
                focusHeading();
              }}
            >
              <X size={17} strokeWidth={1.8} aria-hidden="true" />
            </button>
          ) : null}
        </header>
        <div className="todo-screen__scroll">
          <AiConnectionNotice />
          {renderPage()}
        </div>
      </section>
      {dayRow && page && projectName ? (
        <div className="history-detail todo-screen__conversation">
          <ProgressDayPane
            key={dayRow.start}
            row={dayRow}
            page={page}
            projectName={projectName}
            pendingRecordIds={mutations.pendingRecordIds}
            recordFailure={
              failure?.target.kind === 'record'
                ? { recordId: failure.target.recordId, message: failure.message }
                : null
            }
            onOpenEvidence={onOpenEvidence}
            onDeleteRecord={(recordId) => void mutations.deleteRecord(recordId)}
          />
        </div>
      ) : null}
      {goalFormFor !== null && projectName ? (
        <GoalForm
          projectName={projectName(goalFormFor)}
          // Only a project without a goal offers its form here.
          goal={null}
          failure={
            failure?.target.kind === 'goal' && failure.target.projectId === goalFormFor
              ? failure.message
              : null
          }
          onSave={(goal) => mutations.setGoal(goalFormFor, goal)}
          onClear={() => mutations.clearGoal(goalFormFor)}
          onClose={() => closeGoalForm(goalFormFor)}
        />
      ) : null}
    </div>
  );
}
