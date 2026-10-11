import { useRef, useState, type RefObject } from 'react';
import styled from 'styled-components';
import { X } from 'lucide-react';

import { useI18n } from '@/context/useI18n';
import type { ComposerQuote } from './messageQuotes';
import { AddToReplyButton, QuoteCommentPopover } from './QuoteSelectionPopover';
import { useQuotableSelection, type QuotableSelection } from './useQuotableSelection';

/** What a composer offers for quoting: its quotes, and the conversation they are picked from. */
export type ComposerQuoting = Readonly<{
  quotes: readonly ComposerQuote[];
  /** The conversation whose selected text can be added to the reply. */
  sourceRef: RefObject<HTMLElement>;
  onAdd: (text: string, comment: string) => void;
  onRemove: (id: string) => void;
}>;

/**
 * Scrolls on its own past a third of the window, so however many quotes there are, the message
 * field and its send and stop buttons stay in view, also beside an approval's 45vh dock.
 */
const QuoteList = styled.ul`
  display: grid;
  gap: 6px;
  max-height: 30vh;
  margin: 0;
  padding: 0;
  overflow-y: auto;
  list-style: none;
`;

const QuoteCard = styled.li`
  display: flex;
  align-items: flex-start;
  gap: 6px;
  padding: 6px 6px 6px 10px;
  border-left: 3px solid var(--border-color);
  border-radius: 8px;
  background: rgba(255, 255, 255, 0.05);
  font-size: var(--text-meta-size);

  > div {
    flex: 1;
    min-width: 0;
  }

  blockquote,
  p {
    display: -webkit-box;
    margin: 0;
    overflow: hidden;
    overflow-wrap: anywhere;
    white-space: pre-wrap;
    -webkit-box-orient: vertical;
    -webkit-line-clamp: 3;
  }

  blockquote {
    color: var(--text-secondary);
  }

  p {
    margin-top: 2px;
  }

  button {
    flex: none;
    display: inline-flex;
    padding: 3px;
    border: 0;
    border-radius: 6px;
    color: var(--text-secondary);
    background: transparent;
    cursor: pointer;
  }

  button:focus-visible {
    outline: 2px solid rgba(255, 255, 255, 0.7);
    outline-offset: 1px;
  }
`;

/**
 * A composer's quotes: the 「返信に追加」 button below text selected in the conversation, the
 * popover it opens for a comment, and a removable card for each quote above the message field. All of
 * them go out with the next message, in the order they were added.
 */
export function ComposerQuotes({
  quoting,
  readOnly,
  textareaRef,
}: {
  quoting: ComposerQuoting;
  readOnly: boolean;
  textareaRef: RefObject<HTMLTextAreaElement>;
}) {
  const { t } = useI18n();
  // The selection being commented on. Focusing the comment field clears the page's selection,
  // so it is held here from the moment the button is pressed.
  const [commenting, setCommenting] = useState<QuotableSelection | null>(null);
  const selection = useQuotableSelection(quoting.sourceRef, !readOnly && commenting === null);
  const listRef = useRef<HTMLUListElement>(null);
  const { quotes } = quoting;

  // Focus goes to the message field, beside the quotes, whether a quote was added or not.
  const close = (returnFocus: boolean) => {
    setCommenting(null);
    if (returnFocus) textareaRef.current?.focus();
  };
  const remove = (id: string, index: number) => {
    const buttons = listRef.current?.querySelectorAll('button') ?? [];
    const next = buttons[index + 1] ?? buttons[index - 1] ?? textareaRef.current;
    quoting.onRemove(id);
    next?.focus();
  };

  return (
    <>
      {commenting && !readOnly ? (
        <QuoteCommentPopover
          selection={commenting}
          onAdd={(comment) => {
            quoting.onAdd(commenting.text, comment);
            document.getSelection()?.removeAllRanges();
            close(true);
          }}
          onCancel={close}
        />
      ) : selection ? (
        <AddToReplyButton selection={selection} onPress={() => setCommenting(selection)} />
      ) : null}
      {quotes.length > 0 ? (
        <QuoteList
          ref={listRef}
          aria-label={t('overlay.composer.quotes', { count: quotes.length })}
        >
          {quotes.map((quote, index) => (
            <QuoteCard key={quote.id}>
              <div>
                <blockquote>{quote.text}</blockquote>
                {quote.comment.trim() !== '' ? <p>{quote.comment}</p> : null}
              </div>
              {readOnly ? null : (
                <button
                  type="button"
                  aria-label={t('overlay.composer.quote.remove', {
                    index: index + 1,
                    count: quotes.length,
                  })}
                  title={t('overlay.composer.quote.remove', {
                    index: index + 1,
                    count: quotes.length,
                  })}
                  onClick={() => remove(quote.id, index)}
                >
                  <X size={14} aria-hidden="true" />
                </button>
              )}
            </QuoteCard>
          ))}
        </QuoteList>
      ) : null}
    </>
  );
}
