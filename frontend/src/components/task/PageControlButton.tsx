import type { LucideIcon } from 'lucide-react';

/**
 * One of the page's controls for a task pane (expand, shrink, close), at the end of the pane's
 * header and set apart from the pane's own actions.
 */
export function PageControlButton({
  label,
  icon: Icon,
  onClick,
}: {
  label: string;
  icon: LucideIcon;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      className="action-task__icon-button action-task__page-control"
      aria-label={label}
      data-tooltip={label}
      onClick={onClick}
    >
      <Icon size={17} strokeWidth={1.8} aria-hidden="true" />
    </button>
  );
}
