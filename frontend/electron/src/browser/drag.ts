/**
 * `drag`: presses on the center of one ref's element, moves to the center of another's, and lets
 * go there, for both kinds of drag a page can take:
 * - a pointer drag (a slider, a sortable list, a canvas) follows mouse events alone: a press, a
 *   few moves with the button held, a release;
 * - an HTML drag and drop (`draggable`, drop zones) is never started by synthetic mouse events
 *   in Chromium, so drags are intercepted while the button is held (`Input.setInterceptDrags`):
 *   once the page lets one start, the browser hands its data over (`Input.dragIntercepted`) and
 *   the drag goes on as drag events (`Input.dispatchDragEvent`), which end it with a drop.
 * Which one it is shows as the moves go: a drag the browser handed over, or a `dragstart` the
 * page did not cancel (watched from the engine's world, before the hand-over arrives), is an
 * HTML drag; neither by the last move is a pointer drag.
 *
 * Like a click, nothing is pressed where another element covers the source, nor on a list or
 * picker field. A drag that does not complete, because another element covers the target or
 * anything failed once the button was down, is undone before the operation fails, so the page
 * keeps no held button or open drag: a pointer drag goes back and is released where it began, an
 * HTML drag is cancelled. Only the page's own drag data is dropped, never files.
 */

import type { BrowserOperation } from './browserContract';
import { childSession, type CdpListener, type CdpPage, type CdpSession } from './cdpSession';
import {
  boxOf,
  centerOf,
  checkPoint,
  currentBox,
  emulateFocus,
  failed,
  type Point,
} from './interactions';
import { callOn, resolveRef } from './snapshot';

/** Mouse moves from the source to the target, the last at the target. */
const DRAG_STEPS = 5;
/** How long the browser may take to hand over a drag the page started. */
const DRAG_HANDOVER_MS = 1_000;

/** Notes, in the engine's world, the `dragstart` of the element's document. */
const WATCH_DRAG_START = `function () {
  const view = this.ownerDocument.defaultView;
  const watch = { event: null };
  watch.listen = (event) => { watch.event = event; };
  watch.stop = () => view.removeEventListener('dragstart', watch.listen, true);
  view.addEventListener('dragstart', watch.listen, true);
  globalThis.pantarayDragWatch = watch;
}`;
/** True once a drag started that the page did not cancel. */
const DRAG_STARTED = `function () {
  const { event } = globalThis.pantarayDragWatch;
  return event !== null && !event.defaultPrevented;
}`;
const STOP_DRAG_WATCH = `function () {
  globalThis.pantarayDragWatch?.stop();
  delete globalThis.pantarayDragWatch;
}`;

/** The fields of a CDP `Input.DragData` that are dropped: never `files`, paths on this Mac. */
type DragData = { items: unknown[]; dragOperationsMask: number };

/** The point `step` of `DRAG_STEPS` on the line from `from` to `to`. */
const along = (from: Point, to: Point, step: number): Point => ({
  x: from.x + ((to.x - from.x) * step) / DRAG_STEPS,
  y: from.y + ((to.y - from.y) * step) / DRAG_STEPS,
});

/** The two points, after both elements are scrolled into view; refused unless both are on screen. */
async function endPoints(cdp: CdpSession, source: number, target: number) {
  await boxOf(cdp, target);
  // The source last, so the press is in view; scrolling to it may move the target.
  const from = centerOf(await boxOf(cdp, source));
  const to = centerOf(await currentBox(cdp, target));
  const { cssLayoutViewport: viewport } = (await cdp.sendCommand('Page.getLayoutMetrics')) as {
    cssLayoutViewport: { clientWidth: number; clientHeight: number };
  };
  const shown = ({ x, y }: Point) =>
    x >= 0 && y >= 0 && x < viewport.clientWidth && y < viewport.clientHeight;
  if (!shown(from) || !shown(to)) {
    throw failed('The two elements do not fit on the screen together, so no drag reaches both.');
  }
  return { from, to };
}

/** A drag with no data yet, which a cancel still ends. */
const NO_DATA: DragData = { items: [], dragOperationsMask: 0 };

/**
 * Listens for the drag the browser hands over while interception is on: `arrived` says whether it
 * has, and `taken` resolves to its data, or to null when none came within `DRAG_HANDOVER_MS`.
 */
function handover(cdp: CdpSession) {
  let data: DragData | null = null;
  let take: (handed: DragData) => void = () => undefined;
  const handed = new Promise<DragData>((resolve) => (take = resolve));
  const listener: CdpListener = (_event, method, params, sessionId) => {
    // Interception is the page's own, whichever frame the drag starts in.
    if (method !== 'Input.dragIntercepted' || childSession(sessionId) !== undefined) return;
    const { items, dragOperationsMask } = (params as { data: DragData }).data;
    data = { items, dragOperationsMask };
    take(data);
  };
  cdp.on('message', listener);
  return {
    arrived: () => data !== null,
    taken: (): Promise<DragData | null> => {
      let timer: NodeJS.Timeout | undefined;
      const late = new Promise<null>(
        (resolve) => (timer = setTimeout(resolve, DRAG_HANDOVER_MS, null))
      );
      return Promise.race([handed, late]).finally(() => clearTimeout(timer));
    },
    stop: () => cdp.removeListener('message', listener),
  };
}

/** Where a drag stands once the button is down, and so what undoes it. */
type Held =
  | { kind: 'none' }
  | { kind: 'pointer' }
  // `data` is NO_DATA until the browser hands the drag over; `at` is its latest point.
  | { kind: 'html'; data: DragData; at: Point };

export async function drag(
  page: CdpPage,
  operation: Extract<BrowserOperation, { action: 'drag' }>
): Promise<void> {
  const { cdp } = page;
  const source = await resolveRef(page, operation.ref);
  const target = await resolveRef(page, operation.to_ref);
  const { from, to } = await endPoints(cdp, source, target);
  await checkPoint(cdp, [target], to, false);
  await checkPoint(cdp, [source], from, true);
  const mouse = (type: string, at: Point, buttons: number) =>
    cdp.sendCommand('Input.dispatchMouseEvent', {
      type,
      ...at,
      button: type === 'mouseMoved' && buttons === 0 ? 'none' : 'left',
      buttons,
      clickCount: 1,
    });
  const dragEvent = (type: string, at: Point, data: DragData) =>
    cdp.sendCommand('Input.dispatchDragEvent', { type, ...at, data });

  await emulateFocus(cdp);
  await mouse('mouseMoved', from, 0);
  // The hover may have shown something over the element.
  await checkPoint(cdp, [source], from, true);
  const handed = handover(cdp);
  let held: Held = { kind: 'none' };
  try {
    await cdp.sendCommand('Input.setInterceptDrags', { enabled: true });
    await callOn(cdp, source, WATCH_DRAG_START);
    await mouse('mousePressed', from, 1);
    held = { kind: 'pointer' };
    let step = 0;
    while (held.kind === 'pointer' && step < DRAG_STEPS) {
      step += 1;
      const at = along(from, to, step);
      await mouse('mouseMoved', at, 1);
      // The hand-over decides; the page's own capture listeners can hide its dragstart.
      if (!handed.arrived() && (await callOn(cdp, source, DRAG_STARTED)) !== true) continue;
      held = { kind: 'html', data: NO_DATA, at };
      const data = await handed.taken();
      if (data === null) throw failed('The page started a drag the browser did not hand over.');
      held.data = data;
      await dragEvent('dragEnter', at, data);
    }
    if (held.kind === 'pointer') {
      // The element under a pointer drag is often the dragged one itself.
      await checkPoint(cdp, [target, source], to, false);
      await mouse('mouseReleased', to, 0);
    } else {
      // At least one move over the target, as a person's drag has, before the drop.
      for (let over = Math.min(step + 1, DRAG_STEPS); over <= DRAG_STEPS; over += 1) {
        held.at = along(from, to, over);
        await dragEvent('dragOver', held.at, held.data);
      }
      await checkPoint(cdp, [target], to, false);
      // The drop ends the drag as a release would, so no mouse release follows.
      await dragEvent('drop', to, held.data);
    }
    held = { kind: 'none' };
  } catch (error) {
    // Undone once, at best: the operation's own failure is what it reports.
    if (held.kind === 'pointer') {
      await mouse('mouseMoved', from, 1).catch(() => undefined);
      await mouse('mouseReleased', from, 0).catch(() => undefined);
    } else if (held.kind === 'html') {
      await dragEvent('dragCancel', held.at, held.data).catch(() => undefined);
    }
    throw error;
  } finally {
    handed.stop();
    // Only a page or connection already gone refuses these, which its next operation reports.
    await callOn(cdp, source, STOP_DRAG_WATCH).catch(() => undefined);
    await cdp.sendCommand('Input.setInterceptDrags', { enabled: false }).catch(() => undefined);
  }
}
