import { useEffect, useLayoutEffect, useRef, useState, type RefObject } from 'react';
import styled from 'styled-components';
import { X } from 'lucide-react';

import { useI18n } from '@/context/useI18n';
import type { ComposerQuote } from './messageQuotes';
import { useQuotableSelection, type QuotableSelection } from './useQuotableSelection';

/** What a composer offers for quoting: its quotes, and the conversation they are picked from. */
export type ComposerQuoting = Readonly<{
  quotes: readonly ComposerQuote[];
  /** The conversation whose selected text can be added to the reply. */
  sourceRef: RefObject<HTMLElement>;
  onAdd: (text: string, comment: string) => void;
  onRemove: (id: string) => void;
}>;

// Chromium reports keyCode 229 for the Enter that confirms an IME conversion.
const IME_PROCESS_KEY_CODE = 229;
const VIEWPORT_MARGIN_PX = 8;
const SELECTION_GAP_PX = 6;

/**
 * Laid out at the end of the selection's last line, or above it where there is no room below,
 * and kept inside the window. The containing block differs per host, so the element is moved by
 * the distance between where it is and where it should be.
 */
const Floating = styled.div`
  position: absolute;
  top: 0;
  left: 0;
  z-index: 10;
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 4px;
  border: 1px solid var(--border-color);
  border-radius: 10px;
  background: rgba(20, 24, 30, 0.96);
  box-shadow: 0 6px 20px rgba(0, 0, 0, 0.35);

  input {
    width: min(320px, 70vw);
    padding: 4px 6px;
    border: 0;
    color: var(--text-primary);
    background: transparent;
    font: inherit;
  }

  input:focus-visible {
    outline: none;
  }
`;

const FloatingButton = styled.button`
  padding: 3px 8px;
  border: 0;
  border-radius: 6px;
  color: var(--text-primary);
  background: transparent;
  font: inherit;
  font-size: var(--text-meta-size);
  cursor: pointer;

  &:hover,
  &:focus-visible {
    background: rgba(255, 255, 255, 0.1);
  }

  &:focus-visible {
    outline: 2px solid rgba(255, 255, 255, 0.7);
    outline-offset: 1px;
  }
`;

const QuoteList = styled.ul`
  display: grid;
  gap: 6px;
  margin: 0;
  padding: 0;
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

  blockquote {
    display: -webkit-box;
    margin: 0;
    overflow: hidden;
    color: var(--text-secondary);
    white-space: pre-wrap;
    -webkit-box-orient: vertical;
    -webkit-line-clamp: 3;
  }

  p {
    margin: 2px 0 0;
    overflow-wrap: anywhere;
    white-space: pre-wrap;
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

function useFloatingPosition(end: QuotableSelection['end']) {
  const ref = useRef<HTMLDivElement>(null);
  useLayoutEffect(() => {
    const element = ref.current;
    if (!element) return;
    element.style.transform = '';
    const at = element.getBoundingClientRect();
    const below = end.bottom + SELECTION_GAP_PX;
    const top =
      below + at.height <= window.innerHeight - VIEWPORT_MARGIN_PX
        ? below
        : end.top - SELECTION_GAP_PX - at.height;
    const left = Math.max(
      VIEWPORT_MARGIN_PX,
      Math.min(end.right, window.innerWidth - VIEWPORT_MARGIN_PX - at.width)
    );
    element.style.transform = `translate(${left - at.left}px, ${Math.max(VIEWPORT_MARGIN_PX, top) - at.top}px)`;
  });
  return ref;
}

/** The 「返信に追加」 button beside a selection; the next Tab stop before the composer. */
function AddToReplyButton({
  selection,
  onPress,
}: {
  selection: QuotableSelection;
  onPress: () => void;
}) {
  const { t } = useI18n();
  const ref = useFloatingPosition(selection.end);
  return (
    <Floating ref={ref}>
      <FloatingButton
        type="button"
        // Pressing must not move focus or clear the selection before the click lands.
        onMouseDown={(event) => event.preventDefault()}
        onClick={onPress}
      >
        {t('overlay.composer.quote.add')}
      </FloatingButton>
    </Floating>
  );
}

/** The comment for the selected text: Enter adds the quote, Esc or leaving the field cancels. */
function QuoteCommentInput({
  selection,
  onAdd,
  onCancel,
}: {
  selection: QuotableSelection;
  onAdd: (comment: string) => void;
  onCancel: (returnFocus: boolean) => void;
}) {
  const { t } = useI18n();
  const ref = useFloatingPosition(selection.end);
  const inputRef = useRef<HTMLInputElement>(null);
  const [comment, setComment] = useState('');
  // Opened by pressing the button for it, which leaves the page as this opens.
  useEffect(() => inputRef.current?.focus(), []);
  return (
    <Floating ref={ref}>
      <input
        ref={inputRef}
        type="text"
        value={comment}
        aria-label={t('overlay.composer.quote.comment')}
        placeholder={t('overlay.composer.quote.placeholder')}
        onChange={(event) => setComment(event.target.value)}
        onBlur={() => onCancel(false)}
        onKeyDown={(event) => {
          if (event.key === 'Escape') {
            event.preventDefault();
            event.stopPropagation();
            onCancel(true);
            return;
          }
          if (event.key !== 'Enter') return;
          // Inside the composer's form, Enter would otherwise send the message.
          event.preventDefault();
          if (event.nativeEvent.isComposing || event.keyCode === IME_PROCESS_KEY_CODE) return;
          onAdd(comment);
        }}
      />
    </Floating>
  );
}

/**
 * A composer's quotes: the 「返信に追加」 button beside text selected in the conversation, the
 * comment field it opens, and a removable card for each quote above the message field. All of
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
        <QuoteCommentInput
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
