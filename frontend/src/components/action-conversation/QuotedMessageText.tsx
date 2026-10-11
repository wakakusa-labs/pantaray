import { splitQuotedText } from '@/components/agent-overlay/messageQuotes';

import { ProjectRefText, type RefSpan } from './ProjectRefText';
import './quotedMessageText.css';

// A user's message text: each passage they quoted as a blockquote, their own words as paragraphs
// with the projects, tasks and suggestions they named. Ref spans index the whole text; a ref
// inside a quoted passage (typed by hand after a `>`) shows as plain text.
export function QuotedMessageText({
  text,
  refs,
  className,
}: {
  text: string;
  refs: readonly RefSpan[];
  /** The class of each paragraph of the user's own words. */
  className?: string;
}) {
  return (
    <>
      {splitQuotedText(text).map((block, index) => {
        if (block.kind === 'quote') {
          return (
            <blockquote key={index} className="message-quote">
              {block.text}
            </blockquote>
          );
        }
        const end = block.start + Array.from(block.text).length;
        const inside = refs
          .filter((ref) => ref.start >= block.start && ref.end <= end)
          .map((ref) => ({ ...ref, start: ref.start - block.start, end: ref.end - block.start }));
        return (
          <p key={index} className={className}>
            <ProjectRefText text={block.text} refs={inside} />
          </p>
        );
      })}
    </>
  );
}
