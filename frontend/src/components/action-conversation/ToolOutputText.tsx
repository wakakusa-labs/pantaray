import { useRef } from 'react';

import { useVerticalOverflow } from './useVerticalOverflow';

/** ツール出力の箱。折り返しと高さ上限は CSS が持つ（.action-conversation__tool-output-text）。 */
export function ToolOutputText({ content, label }: { content: string; label: string }) {
  const ref = useRef<HTMLPreElement>(null);
  const scrollable = useVerticalOverflow(ref, content);

  return (
    <pre
      ref={ref}
      className="action-conversation__tool-output-text"
      aria-label={label}
      role={scrollable ? 'group' : undefined}
      tabIndex={scrollable ? 0 : undefined}
    >
      {content}
    </pre>
  );
}
