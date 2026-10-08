import {
  OVERLAY_GRID_COLUMNS,
  OVERLAY_GRID_ROWS,
  type OverlayCell,
} from '../ipc/schemas/overlayPlacement';

export const OVERLAY_WIDTH_PX = 520;
export const OVERLAY_INITIAL_HEIGHT_PX = 120;
const SCREEN_MARGIN_PX = 20;
// Stacked Suggestions are offset by their initial height plus this gap.
const STACK_GAP_PX = 12;
const STACK_STEP_PX = OVERLAY_INITIAL_HEIGHT_PX + STACK_GAP_PX;

type Rect = Readonly<{ x: number; y: number; width: number; height: number }>;

/**
 * Which edge the window keeps while its content grows: the top (top row), its center until
 * the user's first key or click (middle row), or its bottom so it stays on screen (bottom row).
 */
export type OverlayVerticalAnchor = 'top' | 'center' | 'bottom';

export type OverlayPlacement = Readonly<{ x: number; y: number; anchor: OverlayVerticalAnchor }>;

function clamp(value: number, minimum: number, maximum: number): number {
  return Math.max(minimum, Math.min(value, maximum));
}

/**
 * Places a new overlay in its cell of the work area. The outer columns and rows align the
 * overlay to the screen margin on that side; the inner ones center it on the cell's center.
 * `stackIndex` offsets a Suggestion from the ones already shown: downward, or upward from the
 * bottom row, stopping at the last offset that still fits.
 */
export function resolveOverlayPlacement(
  workArea: Rect,
  cell: OverlayCell,
  stackIndex: number
): OverlayPlacement {
  const minimumX = workArea.x + SCREEN_MARGIN_PX;
  const maximumX = workArea.x + workArea.width - SCREEN_MARGIN_PX - OVERLAY_WIDTH_PX;
  const minimumY = workArea.y + SCREEN_MARGIN_PX;
  const maximumY = workArea.y + workArea.height - SCREEN_MARGIN_PX - OVERLAY_INITIAL_HEIGHT_PX;

  const columnCenterX =
    workArea.x + ((2 * cell.column + 1) * workArea.width) / (2 * OVERLAY_GRID_COLUMNS);
  const x =
    cell.column === 0
      ? minimumX
      : cell.column === OVERLAY_GRID_COLUMNS - 1
        ? maximumX
        : clamp(Math.round(columnCenterX - OVERLAY_WIDTH_PX / 2), minimumX, maximumX);

  const anchor: OverlayVerticalAnchor =
    cell.row === 0 ? 'top' : cell.row === OVERLAY_GRID_ROWS - 1 ? 'bottom' : 'center';
  const baseY =
    anchor === 'top'
      ? minimumY
      : anchor === 'bottom'
        ? maximumY
        : Math.round(workArea.y + (workArea.height - OVERLAY_INITIAL_HEIGHT_PX) / 2);
  const stacksUp = anchor === 'bottom';
  const room = stacksUp
    ? baseY + OVERLAY_INITIAL_HEIGHT_PX - minimumY
    : workArea.y + workArea.height - SCREEN_MARGIN_PX - baseY;
  const offsets = Math.min(stackIndex, Math.max(1, Math.floor(room / STACK_STEP_PX)) - 1);
  const y = baseY + (stacksUp ? -1 : 1) * offsets * STACK_STEP_PX;

  return { x, y: clamp(y, minimumY, Math.max(minimumY, maximumY)), anchor };
}
