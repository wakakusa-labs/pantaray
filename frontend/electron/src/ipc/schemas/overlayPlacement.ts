import { z } from 'zod';

/** The miniature screen in Settings is this grid; each kind of overlay sits in one cell. */
export const OVERLAY_GRID_ROWS = 3;
export const OVERLAY_GRID_COLUMNS = 5;

export const OVERLAY_PLACEMENT_KINDS = ['suggestion', 'started', 'history'] as const;
/**
 * `suggestion`: arrives on its own. `started`: a new task the user opens (button, shortcut,
 * tray). `history`: a task or Suggestion reopened from History.
 */
export type OverlayPlacementKind = (typeof OVERLAY_PLACEMENT_KINDS)[number];

export const OverlayCellSchema = z
  .object({
    row: z
      .number()
      .int()
      .min(0)
      .max(OVERLAY_GRID_ROWS - 1),
    column: z
      .number()
      .int()
      .min(0)
      .max(OVERLAY_GRID_COLUMNS - 1),
  })
  .strict();

export type OverlayCell = Readonly<z.infer<typeof OverlayCellSchema>>;
export type OverlayPlacements = Readonly<Record<OverlayPlacementKind, OverlayCell>>;

export const OverlayPlacementUpdateSchema = z
  .object({ kind: z.enum(OVERLAY_PLACEMENT_KINDS), cell: OverlayCellSchema })
  .strict();

export type OverlayPlacementUpdate = z.infer<typeof OverlayPlacementUpdateSchema>;
