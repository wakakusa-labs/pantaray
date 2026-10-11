import { useLayoutEffect, useRef } from 'react';
import type { FocusEvent, RefObject } from 'react';
import styled from 'styled-components';

import { useI18n } from '@/context/useI18n';

// Between the pill and the line it stands above or below.
const GAP_PX = 4;

/**
 * A small labelled pill above the chip's end, or below it when the window has no room above. Fixed
 * to the window, so no clipping ancestor cuts it off, and away from the line, so it never covers
 * the chip's text or the caret.
 */
const Pill = styled.button`
  position: fixed;
  z-index: 10;
  padding: 2px 8px;
  border: 1px solid var(--border-color);
  border-radius: 999px;
  background: #2c2c30;
  color: var(--text-secondary);
  font: inherit;
  font-size: 11px;
  line-height: 16px;
  white-space: nowrap;
  cursor: pointer;

  &:hover,
  &:focus-visible {
    color: var(--text-primary);
  }

  /* White, as the Overlay's focus ring; the main window's composer shares this field. */
  &:focus-visible {
    outline: 2px solid var(--text-primary);
    outline-offset: 1px;
  }
`;

/**
 * 「テキストに戻す」 for the chip the pointer is over or the caret touches: it turns the chip back
 * into its link. It finds the chip `data-chip` names in the field's drawn text and stands at its
 * right end; `data-chip-toggle` tells the field the focus or pointer is on it.
 */
export function WorkChipToggle({
  chip,
  stackRef,
  onToggle,
  onBlur,
  onPointerEnter,
  onPointerLeave,
}: {
  chip: number;
  stackRef: RefObject<HTMLDivElement>;
  onToggle: () => void;
  onBlur: (event: FocusEvent<HTMLButtonElement>) => void;
  onPointerEnter: () => void;
  onPointerLeave: () => void;
}) {
  const { t } = useI18n();
  const pillRef = useRef<HTMLButtonElement>(null);
  // Placed on every render: the draft, its scroll and the field's width all move the chip.
  useLayoutEffect(() => {
    const pill = pillRef.current;
    const rects = stackRef.current?.querySelector(`[data-chip="${chip}"]`)?.getClientRects();
    const last = rects?.[rects.length - 1];
    if (!pill || !last) return;
    const height = pill.offsetHeight;
    const above = last.top - GAP_PX - height;
    pill.style.top = `${above >= 0 ? above : last.bottom + GAP_PX}px`;
    pill.style.left = `${Math.max(0, last.right - pill.offsetWidth)}px`;
    pill.style.visibility = 'visible';
  });
  return (
    <Pill
      ref={pillRef}
      type="button"
      data-chip-toggle=""
      style={{ visibility: 'hidden' }}
      // Pressing it leaves the caret in the field.
      onMouseDown={(event) => event.preventDefault()}
      onClick={onToggle}
      onBlur={onBlur}
      onPointerEnter={onPointerEnter}
      onPointerLeave={onPointerLeave}
    >
      {t('overlay.composer.showAsText')}
    </Pill>
  );
}
