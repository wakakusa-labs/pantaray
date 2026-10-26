import { Check, Minus } from 'lucide-react';
import { type ReactNode, useId } from 'react';

import './checklist.css';

export type CheckStatus = 'pending' | 'done' | 'skipped';

/**
 * One row of a `<ul className="checklist">`: its mark, its title with at most one note, and
 * its one action. The action gets the title's id so its control can name the row.
 */
export function CheckItem({
  title,
  status = 'pending',
  disabled = false,
  note,
  problem,
  action,
}: {
  title: string;
  status?: CheckStatus;
  /** Waits on an earlier row; the caller disables the row's controls too. */
  disabled?: boolean;
  note?: ReactNode;
  /** Why the row is stuck, drawn as a warning above the note and announced. */
  problem?: string | null;
  action: (titleId: string) => ReactNode;
}) {
  const titleId = useId();
  return (
    <li
      className={[
        'check-item',
        status !== 'pending' ? `check-item--${status}` : null,
        disabled ? 'check-item--disabled' : null,
      ]
        .filter(Boolean)
        .join(' ')}
    >
      <span className="check-item-mark" aria-hidden="true">
        {status === 'done' ? <Check size={13} strokeWidth={2.4} /> : null}
        {status === 'skipped' ? <Minus size={13} strokeWidth={2.4} /> : null}
      </span>
      <div className="check-item-text">
        <span className="check-item-title" id={titleId}>
          {title}
        </span>
        {problem ? (
          <span className="check-item-note check-item-note--problem" role="alert">
            {problem}
          </span>
        ) : null}
        {note ? <div className="check-item-note">{note}</div> : null}
      </div>
      <div className="check-item-action">{action(titleId)}</div>
    </li>
  );
}
