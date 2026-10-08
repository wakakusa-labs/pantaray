import { randomUUID } from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';

import {
  OVERLAY_PLACEMENT_KINDS,
  OverlayCellSchema,
  type OverlayCell,
  type OverlayPlacementKind,
  type OverlayPlacements,
  type OverlayPlacementUpdate,
} from '../ipc/schemas/overlayPlacement';

const SETTINGS_FILE_NAME = 'overlay-placement.json';

/**
 * Suggestions and windows reopened from History share the top-right, where they stack; a task
 * the user starts opens centered.
 */
export const DEFAULT_OVERLAY_PLACEMENTS: OverlayPlacements = {
  suggestion: { row: 0, column: 4 },
  started: { row: 1, column: 2 },
  history: { row: 0, column: 4 },
};

export type OverlayPlacementStore = {
  get: () => OverlayPlacements;
  set: (update: OverlayPlacementUpdate) => OverlayPlacements;
};

/** Only the kinds the user moved are stored, so the others follow later changes of default. */
type ChosenPlacements = Partial<Record<OverlayPlacementKind, OverlayCell>>;

/**
 * Each kind is read on its own: a missing kind keeps its default, and an unreadable file or
 * value is reported and replaced by the default, so an overlay always has somewhere to open.
 */
function parseStoredPlacements(
  raw: string,
  reportInvalid: (reason: string) => void
): ChosenPlacements {
  let value: unknown;
  try {
    value = JSON.parse(raw);
  } catch {
    reportInvalid('malformed_json');
    return {};
  }
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    reportInvalid('not_an_object');
    return {};
  }
  const stored = value as Partial<Record<string, unknown>>;
  const chosen: ChosenPlacements = {};
  for (const kind of OVERLAY_PLACEMENT_KINDS) {
    if (stored[kind] === undefined) continue;
    const cell = OverlayCellSchema.safeParse(stored[kind]);
    if (cell.success) chosen[kind] = cell.data;
    else reportInvalid(`invalid_${kind}`);
  }
  return chosen;
}

// Created while the app starts, so a file it cannot read must not stop the app.
function readStoredPlacements(
  settingsPath: string,
  reportInvalid: (reason: string) => void
): ChosenPlacements {
  let raw: string;
  try {
    raw = fs.readFileSync(settingsPath, 'utf8');
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== 'ENOENT') reportInvalid('unreadable_file');
    return {};
  }
  return parseStoredPlacements(raw, reportInvalid);
}

export function createOverlayPlacementStore(params: {
  userDataDir: string;
  reportInvalid: (reason: string) => void;
}): OverlayPlacementStore {
  const settingsPath = path.join(params.userDataDir, SETTINGS_FILE_NAME);
  let chosen = readStoredPlacements(settingsPath, params.reportInvalid);
  let current: OverlayPlacements = { ...DEFAULT_OVERLAY_PLACEMENTS, ...chosen };

  const save = (next: ChosenPlacements): void => {
    fs.mkdirSync(path.dirname(settingsPath), { recursive: true });
    const temporaryPath = `${settingsPath}.tmp-${randomUUID()}`;
    try {
      fs.writeFileSync(temporaryPath, `${JSON.stringify(next)}\n`, {
        encoding: 'utf8',
        flag: 'wx',
        mode: 0o600,
      });
      fs.renameSync(temporaryPath, settingsPath);
    } finally {
      if (fs.existsSync(temporaryPath)) fs.unlinkSync(temporaryPath);
    }
  };

  return {
    get: () => current,
    set: (update) => {
      const next = { ...chosen, [update.kind]: update.cell };
      save(next);
      chosen = next;
      current = { ...DEFAULT_OVERLAY_PLACEMENTS, ...chosen };
      return current;
    },
  };
}
