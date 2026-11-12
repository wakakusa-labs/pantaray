import {
  useLayoutEffect,
  useRef,
  useState,
  type CSSProperties,
  type KeyboardEvent,
  type PointerEvent,
} from 'react';

/**
 * Which side of the TODO screen's rule keeps a width of its own: beside a conversation the pane
 * does and the list takes the rest; once a file opens beside the conversation the list does, and
 * the pane (the file and the conversation's column) takes the rest.
 */
export type TodoPaneLayout = 'conversation' | 'split';

/** The pane splits once a task pane opens a file; the same test as todoScreen.css's. */
const SPLIT_SELECTOR = ':has(.action-task--split, .suggestion-task--split)';

/** The narrowest each side gets, in px. A split pane holds the conversation's 400px column. */
const LIMITS: Record<TodoPaneLayout, { list: number; pane: number }> = {
  conversation: { list: 320, pane: 360 },
  split: { list: 320, pane: 560 },
};
/** The rule's own width between the two, in px. */
const RULE = 1;
/** How far one arrow key moves the rule, in px. */
const STEP = 24;

const STORAGE_PREFIX = 'pantaray.todo-pane-width:';

/** Where the rule may sit: the list's width, from the list's least to the pane's least. */
export function boundaryRange(layout: TodoPaneLayout, width: number) {
  const { list, pane } = LIMITS[layout];
  // A window too narrow for both keeps the list's least, as the CSS did before.
  return { min: list, max: Math.max(list, width - RULE - pane) };
}

const clampBoundary = (layout: TodoPaneLayout, width: number, boundary: number) => {
  const { min, max } = boundaryRange(layout, width);
  return Math.min(Math.max(boundary, min), max);
};

/** The fixed side's width with the rule at `boundary` (the list's width), kept in range. */
export function fixedWidthAt(layout: TodoPaneLayout, width: number, boundary: number): number {
  const list = clampBoundary(layout, width, boundary);
  return layout === 'conversation' ? width - RULE - list : list;
}

/** The list's width with the fixed side at `fixed`. */
const boundaryOf = (layout: TodoPaneLayout, width: number, fixed: number) =>
  layout === 'conversation' ? width - RULE - fixed : fixed;

/** A remembered width as the screen shows it: the window it is shown in may be narrower now. */
export function shownFixedWidth(layout: TodoPaneLayout, width: number, saved: number): number {
  return fixedWidthAt(layout, width, boundaryOf(layout, width, saved));
}

/** The rule's place after `key`, or null for a key that does not move it. */
export function keyedBoundary(
  key: string,
  layout: TodoPaneLayout,
  width: number,
  boundary: number
): number | null {
  const { min, max } = boundaryRange(layout, width);
  if (key === 'Home') return min;
  if (key === 'End') return max;
  if (key === 'ArrowLeft') return clampBoundary(layout, width, boundary - STEP);
  if (key === 'ArrowRight') return clampBoundary(layout, width, boundary + STEP);
  return null;
}

/**
 * The width this viewer last gave the layout's fixed side, or null for the default. A per-viewer
 * convenience, so it lives in this window's storage, as the chat's read position does.
 */
export function readSavedWidth(layout: TodoPaneLayout): number | null {
  try {
    const raw = localStorage.getItem(STORAGE_PREFIX + layout);
    const width = raw === null ? NaN : Number(raw);
    return Number.isFinite(width) && width > 0 ? width : null;
  } catch (error) {
    if (!(error instanceof DOMException)) throw error;
    console.warn('The TODO pane width could not be read.');
    return null;
  }
}

/** Remembers the fixed side's width; null goes back to the default. */
export function saveWidth(layout: TodoPaneLayout, width: number | null): void {
  try {
    if (width === null) localStorage.removeItem(STORAGE_PREFIX + layout);
    else localStorage.setItem(STORAGE_PREFIX + layout, String(Math.round(width)));
  } catch (error) {
    if (!(error instanceof DOMException)) throw error;
    console.warn('The TODO pane width could not be saved.');
  }
}

type Measure = { layout: TodoPaneLayout; width: number; boundary: number };
type Drag = { pointerId: number; layout: TodoPaneLayout; offset: number; width: number | null };

const layoutOf = (screen: HTMLElement): TodoPaneLayout =>
  screen.matches(SPLIT_SELECTOR) ? 'split' : 'conversation';

/**
 * The rule between the TODO list and the pane beside it, which the pointer drags and the arrow
 * keys move. Each layout remembers its own width, in px of its fixed side: a window made wider
 * or narrower then changes the side that flexes, as before, and the side the viewer sized keeps
 * its size while it fits. Until the viewer moves the rule, the CSS's defaults stand.
 */
export function useTodoPaneWidth(screen: HTMLElement | null, list: HTMLElement | null) {
  const [saved, setSaved] = useState<Record<TodoPaneLayout, number | null>>(() => ({
    conversation: readSavedWidth('conversation'),
    split: readSavedWidth('split'),
  }));
  // What was last set, before it is drawn: a held arrow key steps on from it.
  const savedRef = useRef(saved);
  const [measure, setMeasure] = useState<Measure | null>(null);
  const dragRef = useRef<Drag | null>(null);

  // The window's and the list's sizes, read before paint so a remembered width shows at once.
  useLayoutEffect(() => {
    if (!screen || !list) return;
    const read = () =>
      setMeasure({
        layout: layoutOf(screen),
        width: screen.clientWidth,
        boundary: list.getBoundingClientRect().width,
      });
    read();
    const observer = new ResizeObserver(read);
    observer.observe(screen);
    observer.observe(list);
    return () => observer.disconnect();
  }, [screen, list]);

  const varFor = (layout: TodoPaneLayout) => {
    const width = saved[layout];
    return width === null || measure === null
      ? undefined
      : `${shownFixedWidth(layout, measure.width, width)}px`;
  };
  const style = {
    '--todo-pane-width': varFor('conversation'),
    '--todo-split-list-width': varFor('split'),
  } as CSSProperties;

  const setWidth = (layout: TodoPaneLayout, width: number | null) => {
    savedRef.current = { ...savedRef.current, [layout]: width };
    setSaved(savedRef.current);
  };

  const onPointerDown = (event: PointerEvent<HTMLElement>) => {
    if (event.button !== 0 || !screen || !list) return;
    event.preventDefault();
    event.currentTarget.setPointerCapture(event.pointerId);
    const left = screen.getBoundingClientRect().left;
    dragRef.current = {
      pointerId: event.pointerId,
      layout: layoutOf(screen),
      // Where on the rule's hit area it was taken, so the rule does not jump to the pointer.
      offset: event.clientX - left - list.getBoundingClientRect().width,
      width: null,
    };
  };
  const onPointerMove = (event: PointerEvent<HTMLElement>) => {
    const drag = dragRef.current;
    if (!drag || drag.pointerId !== event.pointerId || !screen) return;
    const rect = screen.getBoundingClientRect();
    drag.width = fixedWidthAt(drag.layout, rect.width, event.clientX - rect.left - drag.offset);
    setWidth(drag.layout, drag.width);
  };
  // Pointer up and cancel both release the capture; the last width is kept.
  const onLostPointerCapture = () => {
    const drag = dragRef.current;
    dragRef.current = null;
    if (drag?.width != null) saveWidth(drag.layout, drag.width);
  };
  const onDoubleClick = () => {
    if (!screen) return;
    const layout = layoutOf(screen);
    setWidth(layout, null);
    saveWidth(layout, null);
  };
  const onKeyDown = (event: KeyboardEvent<HTMLElement>) => {
    if (!screen || !list) return;
    const layout = layoutOf(screen);
    const width = screen.clientWidth;
    const fixed = savedRef.current[layout];
    const boundary =
      fixed === null
        ? list.getBoundingClientRect().width
        : boundaryOf(layout, width, shownFixedWidth(layout, width, fixed));
    const next = keyedBoundary(event.key, layout, width, boundary);
    if (next === null) return;
    event.preventDefault();
    const nextWidth = fixedWidthAt(layout, width, next);
    setWidth(layout, nextWidth);
    saveWidth(layout, nextWidth);
  };

  const range = measure ? boundaryRange(measure.layout, measure.width) : null;
  return {
    style,
    separatorProps: {
      role: 'separator',
      tabIndex: 0,
      'aria-orientation': 'vertical',
      'aria-valuenow': measure ? Math.round(measure.boundary) : undefined,
      'aria-valuemin': range?.min,
      'aria-valuemax': range ? Math.round(range.max) : undefined,
      onPointerDown,
      onPointerMove,
      onLostPointerCapture,
      onDoubleClick,
      onKeyDown,
    },
  } as const;
}
