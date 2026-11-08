import { useId } from 'react';

import { useI18n } from '@/context/useI18n';

import type {
  ProgressPeriodView,
  TodoProgressPage,
} from '../../../../electron/src/todos/todoContracts';
import {
  formatDay,
  formatQuantity,
  projectLabel,
  recordCount,
  indexRecords,
  recordsFor,
  todoCount,
  type ProjectNameOf,
} from './progressText';

import './progress.css';

/** A failed deletion, told next to its record. */
export type RecordFailure = { recordId: string; message: string };

export type RecordActions = {
  pendingRecordIds: ReadonlySet<string>;
  recordFailure: RecordFailure | null;
  /** Opens the conversation whose results the record came from. */
  onOpenEvidence: (actionId: string) => void;
  onDeleteRecord: (recordId: string) => void;
};

/**
 * One day beside the calendar: the records behind its numbers, each opening onto the
 * conversation it came from, and the TODOs checked that day as they were written.
 */
export function ProgressDayPane({
  row,
  page,
  projectName,
  pendingRecordIds,
  recordFailure,
  onOpenEvidence,
  onDeleteRecord,
}: {
  row: ProgressPeriodView;
  /** The page the day is on, which carries its records. */
  page: TodoProgressPage;
  projectName: ProjectNameOf;
} & RecordActions) {
  const i18n = useI18n();
  const { t } = i18n;
  const headingId = useId();
  const records = recordsFor(row.record_ids, indexRecords(page.records));
  return (
    <section className="todo-progress-day" aria-labelledby={headingId}>
      <header className="todo-progress-day__head">
        <span className="todo-progress-day__crumb">{t('todoProgress.title')}</span>
        <h2 id={headingId}>{formatDay(i18n, row.start)}</h2>
        <p className="todo-progress-day__facts">
          <span>
            {t('todoProgress.day.recordCount', { records: recordCount(i18n, records.length) })}
          </span>
          <span>
            {t('todoProgress.day.checkedCount', { todos: todoCount(i18n, row.checked.length) })}
          </span>
        </p>
      </header>
      <div className="todo-progress-day__body">
        {records.length === 0 && row.checked.length === 0 ? (
          <p className="todo-progress-day__empty">{t('todoProgress.day.empty')}</p>
        ) : null}
        {records.length > 0 ? (
          <>
            <h3>{t('todoProgress.day.records')}</h3>
            <ul>
              {records.map((record) => {
                const { evidence } = record;
                return (
                  <li key={record.record_id} className="todo-progress-entry">
                    <span className="todo-progress-dot" aria-hidden="true" />
                    <span className="todo-progress-entry__text">{record.note}</span>
                    <span className="todo-progress-entry__quantity">
                      {formatQuantity(i18n, record.quantity, record.unit)}
                    </span>
                    <span className="todo-progress-entry__meta">
                      <span>{projectLabel(i18n, projectName, record.project_id)}</span>
                      {evidence.kind === 'action_steps' ? (
                        <button
                          type="button"
                          className="todo-progress-link"
                          aria-label={t('todoProgress.record.openConversationNamed', {
                            note: record.note,
                          })}
                          onClick={() => onOpenEvidence(evidence.action_id)}
                        >
                          {t('todoProgress.record.openConversation')}
                        </button>
                      ) : null}
                      <button
                        type="button"
                        className="todo-progress-link"
                        aria-label={t('todoProgress.record.deleteNamed', { note: record.note })}
                        disabled={pendingRecordIds.has(record.record_id)}
                        onClick={() => onDeleteRecord(record.record_id)}
                      >
                        {t('todoProgress.record.delete')}
                      </button>
                    </span>
                    {recordFailure?.recordId === record.record_id ? (
                      <span className="todo-progress-entry__error" role="alert">
                        {recordFailure.message}
                      </span>
                    ) : null}
                  </li>
                );
              })}
            </ul>
          </>
        ) : null}
        {row.checked.length > 0 ? (
          <>
            <h3>{t('todoProgress.day.checked')}</h3>
            <ul>
              {row.checked.map((todo) => (
                <li key={todo.todo_id} className="todo-progress-entry todo-progress-entry--checked">
                  <span className="todo-progress-dot" aria-hidden="true" />
                  <span className="todo-progress-entry__text">{todo.text}</span>
                </li>
              ))}
            </ul>
          </>
        ) : null}
      </div>
    </section>
  );
}
