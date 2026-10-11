import { useEffect, useState, type RefObject } from 'react';

import { quotedPassage } from './messageQuotes';

/** Text selected inside the conversation, and where it and the conversation are on screen. */
export type QuotableSelection = Readonly<{
  text: string;
  /** The selection's box; `endRight` is where its last line ends. */
  top: number;
  bottom: number;
  left: number;
  endRight: number;
  /** The conversation's sides, which what is shown beside the selection stays within. */
  paneLeft: number;
  paneRight: number;
}>;

function readSelection(source: HTMLElement | null): QuotableSelection | null {
  const selection = document.getSelection();
  if (!source || !selection || selection.isCollapsed || selection.rangeCount === 0) return null;
  const range = selection.getRangeAt(0);
  if (!source.contains(range.commonAncestorContainer)) return null;
  const text = quotedPassage(selection.toString());
  if (text === null) return null;
  const box = range.getBoundingClientRect();
  const rects = range.getClientRects();
  const pane = source.getBoundingClientRect();
  return {
    text,
    top: box.top,
    bottom: box.bottom,
    left: box.left,
    endRight: rects.length > 0 ? rects[rects.length - 1].right : box.right,
    paneLeft: pane.left,
    paneRight: pane.right,
  };
}

/**
 * The text selected inside `source`, by mouse or keyboard, kept in step as it changes or scrolls.
 * Null while nothing there is selected, or while `active` is false.
 */
export function useQuotableSelection(
  source: RefObject<HTMLElement>,
  active: boolean
): QuotableSelection | null {
  const [selection, setSelection] = useState<QuotableSelection | null>(null);
  useEffect(() => {
    if (!active) return;
    const read = () => setSelection(readSelection(source.current));
    read();
    document.addEventListener('selectionchange', read);
    // The conversation scrolls inside its own element; capture hears that scroll too.
    window.addEventListener('scroll', read, true);
    window.addEventListener('resize', read);
    return () => {
      document.removeEventListener('selectionchange', read);
      window.removeEventListener('scroll', read, true);
      window.removeEventListener('resize', read);
    };
  }, [source, active]);
  return active ? selection : null;
}
