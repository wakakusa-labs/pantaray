import type { ReactNode } from 'react';

import './projectRefText.css';

/** A named project, task or suggestion; `open` names and opens the item it points at. */
export type RefSpan = Readonly<{
  start: number;
  end: number;
  open?: Readonly<{ label: string; onOpen: () => void }>;
}>;

/**
 * The text with each referenced workspace project, task and suggestion in its own span. Spans are
 * Unicode code-point offsets (as the backend stores them), so the text is indexed by code point
 * rather than by UTF-16 unit. The backend guarantees the spans fall inside the text and never
 * overlap; projects and work come as two lists, so they are put in text order here.
 *
 * A span that opens is not a `<button>`: a drag that starts in a button selects nothing past it,
 * and the bubble's text has to stay copyable.
 */
export function ProjectRefText({ text, refs }: { text: string; refs: readonly RefSpan[] }) {
  if (refs.length === 0) return <>{text}</>;
  const codePoints = Array.from(text);
  const parts: ReactNode[] = [];
  let cursor = 0;
  for (const ref of [...refs].sort((left, right) => left.start - right.start)) {
    parts.push(codePoints.slice(cursor, ref.start).join(''));
    const label = codePoints.slice(ref.start, ref.end).join('');
    const { open } = ref;
    parts.push(
      open ? (
        <span
          key={ref.start}
          className="action-conversation__project-ref action-conversation__project-ref--opens"
          role="link"
          tabIndex={0}
          aria-label={open.label}
          onClick={() => {
            // A drag that selected text ends in a click; it was a copy, not an open.
            if (window.getSelection()?.isCollapsed === false) return;
            open.onOpen();
          }}
          onKeyDown={(event) => {
            if (event.key !== 'Enter') return;
            event.preventDefault();
            open.onOpen();
          }}
        >
          {label}
        </span>
      ) : (
        <span key={ref.start} className="action-conversation__project-ref">
          {label}
        </span>
      )
    );
    cursor = ref.end;
  }
  parts.push(codePoints.slice(cursor).join(''));
  return <>{parts}</>;
}
