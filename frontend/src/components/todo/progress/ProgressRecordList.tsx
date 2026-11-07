import { useI18n } from '@/context/useI18n';

import type { ProgressRecordView } from '../../../../electron/src/todos/todoContracts';
import { formatDate, formatQuantity, momentLocalDate } from './progressText';

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

/** The records behind a number, behind a disclosure: each with what it was and who wrote it. */
export function ProgressRecordList({
  summary,
  records,
  today,
  pendingRecordIds,
  recordFailure,
  onOpenEvidence,
  onDeleteRecord,
}: {
  summary: string;
  records: readonly ProgressRecordView[];
  today: string;
} & RecordActions) {
  const i18n = useI18n();
  const { t } = i18n;
  return (
    <details className="todo-progress-records">
      <summary>{summary}</summary>
      <ul>
        {records.map((record) => {
          const { evidence } = record;
          return (
            <li key={record.record_id} className="todo-progress-record">
              <div className="todo-progress-record__body">
                <span className="todo-progress-record__note">{record.note}</span>
                <span className="todo-progress-record__meta">
                  <span>{formatQuantity(i18n, record.quantity, record.unit)}</span>
                  <span>{formatDate(i18n, today, momentLocalDate(record.recorded_at))}</span>
                  <span>
                    {t(
                      evidence.kind === 'user'
                        ? 'todoProgress.record.byUser'
                        : 'todoProgress.record.byAction'
                    )}
                  </span>
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
                </span>
                {recordFailure?.recordId === record.record_id ? (
                  <span className="todo-progress-record__error" role="alert">
                    {recordFailure.message}
                  </span>
                ) : null}
              </div>
              <button
                type="button"
                className="history-filter-button"
                aria-label={t('todoProgress.record.deleteNamed', { note: record.note })}
                disabled={pendingRecordIds.has(record.record_id)}
                onClick={() => onDeleteRecord(record.record_id)}
              >
                {t('todoProgress.record.delete')}
              </button>
            </li>
          );
        })}
      </ul>
    </details>
  );
}
