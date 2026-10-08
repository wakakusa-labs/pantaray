import { randomUUID } from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';

import {
  OVERLAY_PLACEMENT_KINDS,
  OverlayCellSchema,
  type OverlayPlacements,
  type OverlayPlacementUpdate,
} from '../ipc/schemas/overlayPlacement';

const SETTINGS_FILE_NAME = 'overlay-placement.json';

/** Today's placement: Suggestions stack from the top-right, tasks open centered. */
export const DEFAULT_OVERLAY_PLACEMENTS: OverlayPlacements = {
  suggestion: { row: 0, column: 4 },
  started: { row: 1, column: 2 },
  history: { row: 1, column: 2 },
};

export type OverlayPlacementStore = {
  get: () => OverlayPlacements;
  set: (update: OverlayPlacementUpdate) => OverlayPlacements;
};

/**
 * Each kind is read on its own: a missing kind keeps its default, and an unreadable file or
 * value is reported and replaced by the default, so an overlay always has somewhere to open.
 */
function parseStoredPlacements(
  raw: string,
  reportInvalid: (reason: string) => void
): OverlayPlacements {
  let value: unknown;
  try {
    value = JSON.parse(raw);
  } catch {
    reportInvalid('malformed_json');
    return DEFAULT_OVERLAY_PLACEMENTS;
  }
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    reportInvalid('not_an_object');
    return DEFAULT_OVERLAY_PLACEMENTS;
  }
  const stored = value as Partial<Record<string, unknown>>;
  const placements = { ...DEFAULT_OVERLAY_PLACEMENTS };
  for (const kind of OVERLAY_PLACEMENT_KINDS) {
    if (stored[kind] === undefined) continue;
    const cell = OverlayCellSchema.safeParse(stored[kind]);
    if (cell.success) placements[kind] = cell.data;
    else reportInvalid(`invalid_${kind}`);
  }
  return placements;
}

// Created while the app starts, so a file it cannot read must not stop the app.
function readStoredPlacements(
  settingsPath: string,
  reportInvalid: (reason: string) => void
): OverlayPlacements {
  let raw: string;
  try {
    raw = fs.readFileSync(settingsPath, 'utf8');
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== 'ENOENT') reportInvalid('unreadable_file');
    return DEFAULT_OVERLAY_PLACEMENTS;
  }
  return parseStoredPlacements(raw, reportInvalid);
}

export function createOverlayPlacementStore(params: {
  userDataDir: string;
  reportInvalid: (reason: string) => void;
}): OverlayPlacementStore {
  const settingsPath = path.join(params.userDataDir, SETTINGS_FILE_NAME);
  let current = readStoredPlacements(settingsPath, params.reportInvalid);

  const save = (next: OverlayPlacements): void => {
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
      const next = { ...current, [update.kind]: update.cell };
      save(next);
      current = next;
      return current;
    },
  };
}
