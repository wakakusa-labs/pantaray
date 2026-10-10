import type { BrowserWindow } from 'electron';

import type { ExecuteActionClientEvent, NotificationWindowApi, OverlaySnapshot } from './contracts';
import { isReplayableActionPhase } from './contracts';
import { toOverlaySnapshotPayload } from './overlayState';

export type SuggestionRecord = {
  snapshot: OverlaySnapshot;
  executeEnvelope: ExecuteActionClientEvent | null;
};

// A record that never streamed its suggestion (accepted from a panel that read its bootstrap
// itself) has no body or contract; a persisted read supplies them without touching its state.
function fillFromPersisted(live: OverlaySnapshot, persisted: OverlaySnapshot): OverlaySnapshot {
  if (live.suggestionText && live.interactionContract !== null) return live;
  return {
    ...live,
    suggestionText: live.suggestionText || persisted.suggestionText,
    interactionContract: live.interactionContract ?? persisted.interactionContract,
  };
}

/**
 * Main's record of each suggestion for the current owner: the snapshot both the Overlay panels
 * and the main window show, and the execute command an accept has not had acknowledged yet.
 * Every change reaches the panel and the main window in the same payload.
 */
export function createSuggestionRecords(params: {
  notificationWindow: Pick<NotificationWindowApi, 'setOverlaySnapshot'>;
  getMainWindow: () => BrowserWindow | null;
  onRecordsChanged: () => void;
}) {
  const records = new Map<string, SuggestionRecord>();
  // Dismissed suggestions for this owner: neither a read that left before the dismissal nor an
  // event resent after it may answer the suggestion again.
  const dismissed = new Map<string, OverlaySnapshot>();

  function sendToMain(snapshot: OverlaySnapshot): void {
    const mainWin = params.getMainWindow();
    if (!mainWin || mainWin.isDestroyed()) return;
    try {
      mainWin.webContents.send('suggestion:snapshot', toOverlaySnapshotPayload(snapshot));
    } catch (error) {
      console.error('Failed to deliver suggestion snapshot to main window:', error);
    }
  }

  function store(record: SuggestionRecord): OverlaySnapshot {
    const dismissedSnapshot = dismissed.get(String(record.snapshot.suggestionId));
    if (dismissedSnapshot) return dismissedSnapshot;
    const normalized = {
      ...record.snapshot,
      updatedAt: record.snapshot.updatedAt ?? new Date().toISOString(),
    };
    records.set(String(normalized.suggestionId), {
      snapshot: normalized,
      executeEnvelope: record.executeEnvelope,
    });
    sendToMain(normalized);
    try {
      params.notificationWindow.setOverlaySnapshot(
        String(normalized.suggestionId),
        toOverlaySnapshotPayload(normalized)
      );
    } catch {
      // no-op
    }
    params.onRecordsChanged();
    return normalized;
  }

  function storeSnapshot(snapshot: OverlaySnapshot): OverlaySnapshot {
    const current = records.get(String(snapshot.suggestionId));
    return store({
      snapshot,
      executeEnvelope:
        snapshot.actionPhase !== 'terminal' && current?.snapshot.commandId === snapshot.commandId
          ? current.executeEnvelope
          : null,
    });
  }

  function getRecord(suggestionId: string): SuggestionRecord | null {
    return records.get(String(suggestionId)) ?? null;
  }

  function clear(suggestionId: string): void {
    records.delete(String(suggestionId));
    params.onRecordsChanged();
  }

  // The main window sees the dismissal before the record it reads from is gone.
  function dismiss(rejected: OverlaySnapshot): void {
    const suggestionId = String(rejected.suggestionId);
    if (records.has(suggestionId)) sendToMain(rejected);
    dismissed.set(suggestionId, rejected);
    clear(suggestionId);
  }

  // A persisted read seeds the record the live stream keeps current. A read no newer than the
  // record only fills the body it lacks (Action events never carry one), and a command the backend
  // may not have recorded yet stays with its envelope: dropping it would stop its replay and offer
  // the accept again.
  function adoptPersisted(persisted: OverlaySnapshot): OverlaySnapshot {
    const suggestionId = String(persisted.suggestionId);
    const dismissedSnapshot = dismissed.get(suggestionId);
    if (dismissedSnapshot) return fillFromPersisted(dismissedSnapshot, persisted);
    const current = records.get(suggestionId);
    if (current && current.snapshot.lastSequence >= persisted.lastSequence) {
      const filled = fillFromPersisted(current.snapshot, persisted);
      if (filled === current.snapshot) return filled;
      return store({ snapshot: filled, executeEnvelope: current.executeEnvelope });
    }
    if (
      current?.executeEnvelope &&
      isReplayableActionPhase(current.snapshot.actionPhase) &&
      persisted.commandId !== current.snapshot.commandId
    ) {
      const { commandId, actionPhase, isLive } = current.snapshot;
      return store({
        snapshot: { ...persisted, commandId, actionPhase, isLive },
        executeEnvelope: current.executeEnvelope,
      });
    }
    return storeSnapshot(persisted);
  }

  return {
    store,
    storeSnapshot,
    getRecord,
    getSnapshot: (suggestionId: string) => getRecord(suggestionId)?.snapshot ?? null,
    clear,
    dismiss,
    adoptPersisted,
    values: () => records.values(),
    // The owner changed: nothing held for the previous owner applies.
    reset: () => {
      records.clear();
      dismissed.clear();
    },
  };
}
