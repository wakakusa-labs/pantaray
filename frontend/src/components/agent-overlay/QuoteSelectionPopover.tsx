import { useEffect, useId, useLayoutEffect, useRef, useState } from 'react';
import styled from 'styled-components';

import { useI18n } from '@/context/useI18n';
import type { QuotableSelection } from './useQuotableSelection';

// Chromium reports keyCode 229 for the Enter that confirms an IME conversion.
const IME_PROCESS_KEY_CODE = 229;
const EDGE_MARGIN_PX = 8;
const SELECTION_GAP_PX = 6;
const POPOVER_MAX_WIDTH_PX = 420;

// Below the selection, or above it where the window has no room below, so the selected lines stay
// visible; sideways within the conversation and the window. The containing block differs per
// host, so the element is moved by the distance between where it is and where it should be.
function useBesideSelection(selection: QuotableSelection, x: number, fitWidth: boolean) {
  const ref = useRef<HTMLDivElement>(null);
  useLayoutEffect(() => {
    const element = ref.current;
    if (!element) return;
    const paneLeft = Math.max(selection.paneLeft, 0) + EDGE_MARGIN_PX;
    const paneRight = Math.min(selection.paneRight, window.innerWidth) - EDGE_MARGIN_PX;
    element.style.transform = '';
    if (fitWidth) {
      element.style.width = `${Math.min(POPOVER_MAX_WIDTH_PX, paneRight - paneLeft)}px`;
    }
    const at = element.getBoundingClientRect();
    const below = selection.bottom + SELECTION_GAP_PX;
    const top =
      below + at.height <= window.innerHeight - EDGE_MARGIN_PX
        ? below
        : Math.max(EDGE_MARGIN_PX, selection.top - SELECTION_GAP_PX - at.height);
    const left = Math.max(paneLeft, Math.min(x, paneRight - at.width));
    element.style.transform = `translate(${left - at.left}px, ${top - at.top}px)`;
  });
  return ref;
}

// The tones of the composer it opens from, which sets them; the blur keeps it readable over text.
const Surface = styled.div`
  position: absolute;
  top: 0;
  left: 0;
  z-index: 20;
  box-sizing: border-box;
  border: 1px solid var(--composer-border);
  border-radius: 10px;
  background: var(--composer-surface);
  box-shadow: 0 8px 32px rgba(0, 0, 0, 0.4);
  backdrop-filter: blur(24px);
`;

/** 「返信に追加」 on its own surface, or the popover's filled 「追加」. */
const PopoverButton = styled.button<{ $primary?: boolean }>`
  padding: 5px 12px;
  border: 0;
  border-radius: 8px;
  color: ${({ $primary }) => ($primary ? '#12161c' : 'var(--fog-text)')};
  background: ${({ $primary }) => ($primary ? 'rgba(255, 255, 255, 0.92)' : 'transparent')};
  font: inherit;
  font-size: 12px;
  font-weight: ${({ $primary }) => ($primary ? 600 : 400)};
  cursor: pointer;

  &:focus-visible {
    outline: none;
    box-shadow: var(--focus-ring);
  }
`;

const Popover = styled(Surface)`
  display: grid;
  gap: 8px;
  padding: 10px;

  blockquote {
    display: -webkit-box;
    margin: 0;
    padding-left: 8px;
    overflow: hidden;
    border-left: 3px solid var(--border-color-hover);
    color: var(--fog-text-3);
    font-size: var(--text-meta-size);
    overflow-wrap: anywhere;
    white-space: pre-wrap;
    -webkit-box-orient: vertical;
    -webkit-line-clamp: 2;
  }

  input {
    padding: 8px 10px;
    border: 1px solid var(--composer-border);
    border-radius: 9px;
    color: var(--text-primary);
    background: var(--composer-surface);
    font: inherit;
  }

  input:focus-visible {
    outline: none;
    box-shadow: var(--focus-ring);
  }

  > div {
    display: flex;
    align-items: center;
    justify-content: space-between;
    color: var(--fog-text-3);
    font-size: var(--text-meta-size);
  }
`;

/** The 「返信に追加」 button below a selection; the next Tab stop before the composer. */
export function AddToReplyButton({
  selection,
  onPress,
}: {
  selection: QuotableSelection;
  onPress: () => void;
}) {
  const { t } = useI18n();
  const ref = useBesideSelection(selection, selection.endRight, false);
  return (
    <Surface ref={ref}>
      <PopoverButton
        type="button"
        // Pressing must not move focus or clear the selection before the click lands.
        onMouseDown={(event) => event.preventDefault()}
        onClick={onPress}
      >
        {t('overlay.composer.quote.add')}
      </PopoverButton>
    </Surface>
  );
}

// What is being quoted and an optional comment on it. Enter or 「追加」 adds the quote; Esc, or
// focus leaving the popover, cancels.
export function QuoteCommentPopover({
  selection,
  onAdd,
  onCancel,
}: {
  selection: QuotableSelection;
  onAdd: (comment: string) => void;
  onCancel: (returnFocus: boolean) => void;
}) {
  const { t } = useI18n();
  const ref = useBesideSelection(selection, selection.left, true);
  const inputRef = useRef<HTMLInputElement>(null);
  const quoteId = useId();
  const [comment, setComment] = useState('');
  // Opened by pressing the button for it, which leaves the page as this opens.
  useEffect(() => inputRef.current?.focus(), []);
  return (
    <Popover
      ref={ref}
      // A press anywhere but the field keeps focus in it, so the popover stays open.
      onMouseDown={(event) => {
        if (event.target !== inputRef.current) event.preventDefault();
      }}
      onBlur={(event) => {
        if (!event.currentTarget.contains(event.relatedTarget)) onCancel(false);
      }}
    >
      <blockquote id={quoteId}>{selection.text}</blockquote>
      <input
        ref={inputRef}
        type="text"
        value={comment}
        aria-label={t('overlay.composer.quote.comment')}
        aria-describedby={quoteId}
        placeholder={t('overlay.composer.quote.placeholder')}
        onChange={(event) => setComment(event.target.value)}
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
      <div>
        <span>{t('overlay.composer.quote.cancelHint')}</span>
        <PopoverButton type="button" $primary onClick={() => onAdd(comment)}>
          {t('overlay.composer.quote.confirm')}
        </PopoverButton>
      </div>
    </Popover>
  );
}
