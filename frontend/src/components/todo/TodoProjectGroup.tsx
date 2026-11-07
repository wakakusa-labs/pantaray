import { useId, type ReactNode } from 'react';

import { LineTextField } from '@/components/ui/LineTextField';
import { useI18n } from '@/context/useI18n';

import './todo.css';

/**
 * One project's TODOs for the week: its heading, how many are done, its rows (`TodoRow`s, in the
 * order given, done ones in place), and the add field when the week can still take TODOs.
 */
export function TodoProjectGroup({
  projectName,
  organizationName,
  doneCount,
  totalCount,
  children,
  addField,
}: {
  /** Null for the TODOs in no project. */
  projectName: string | null;
  /** Shown before the project while all organizations are in view; null otherwise. */
  organizationName: string | null;
  doneCount: number;
  totalCount: number;
  children: ReactNode;
  /** Null for a week that has ended. */
  addField: ReactNode;
}) {
  const { t } = useI18n();
  const headingId = useId();
  const organizationId = useId();
  const count =
    totalCount === 0
      ? null
      : doneCount === totalCount
        ? t('todo.group.allDone')
        : t('todo.group.count', { done: doneCount, total: totalCount });
  return (
    <section
      className="todo-group"
      aria-labelledby={organizationName ? `${organizationId} ${headingId}` : headingId}
    >
      <div className="todo-group__head">
        {organizationName ? (
          <span>
            <span id={organizationId}>{organizationName}</span>
            <span aria-hidden="true"> ›</span>
          </span>
        ) : null}
        <h3 id={headingId}>{projectName ?? t('todo.noProject')}</h3>
        {count ? <span>{count}</span> : null}
      </div>
      <ul className="todo-group__rows">{children}</ul>
      {addField}
    </section>
  );
}

/** The line under a group's rows that adds a TODO; the field empties once the TODO was taken. */
export function TodoAddField({ onSubmit }: { onSubmit: (text: string) => Promise<boolean> }) {
  const { t } = useI18n();
  return (
    <div className="todo-add">
      <LineTextField label={t('todo.add')} onSubmit={onSubmit} />
    </div>
  );
}
