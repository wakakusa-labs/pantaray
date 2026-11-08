import { useId, type ReactNode } from 'react';

import { useI18n } from '@/context/useI18n';

import './todo.css';

/**
 * One project's TODOs for the week: its heading, how many are done, and its rows (`TodoRow`s,
 * in the order given, done ones in place).
 */
export function TodoProjectGroup({
  projectName,
  organizationName,
  doneCount,
  totalCount,
  children,
}: {
  /** Null for the TODOs in no project. */
  projectName: string | null;
  /** Shown after the project while all organizations are in view; null otherwise. */
  organizationName: string | null;
  doneCount: number;
  totalCount: number;
  children: ReactNode;
}) {
  const { t } = useI18n();
  const headingId = useId();
  const organizationId = useId();
  return (
    <section
      className="todo-group"
      aria-labelledby={organizationName ? `${headingId} ${organizationId}` : headingId}
    >
      <div className="todo-group__head">
        <span className="todo-group__dot" aria-hidden="true" />
        <h3 id={headingId}>{projectName ?? t('todo.noProject')}</h3>
        {organizationName ? (
          <span id={organizationId} className="todo-group__organization">
            {organizationName}
          </span>
        ) : null}
        <span className="todo-group__count">
          {t('todo.group.count', { done: doneCount, total: totalCount })}
        </span>
      </div>
      <ul className="todo-group__rows">{children}</ul>
    </section>
  );
}
