import type { ReactNode } from 'react';

import './projectRefText.css';

type ProjectRefSpan = Readonly<{ start: number; end: number }>;

/**
 * The text with each referenced workspace project in its own span. Spans are Unicode
 * code-point offsets (as the backend stores them), so the text is indexed by code point
 * rather than by UTF-16 unit. The backend guarantees the spans are in order, do not
 * overlap, and fall inside the text.
 */
export function ProjectRefText({ text, refs }: { text: string; refs: readonly ProjectRefSpan[] }) {
  if (refs.length === 0) return <>{text}</>;
  const codePoints = Array.from(text);
  const parts: ReactNode[] = [];
  let cursor = 0;
  for (const ref of refs) {
    parts.push(codePoints.slice(cursor, ref.start).join(''));
    parts.push(
      <span key={ref.start} className="action-conversation__project-ref">
        {codePoints.slice(ref.start, ref.end).join('')}
      </span>
    );
    cursor = ref.end;
  }
  parts.push(codePoints.slice(cursor).join(''));
  return <>{parts}</>;
}
