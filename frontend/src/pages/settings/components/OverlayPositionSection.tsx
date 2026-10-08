import {
  useEffect,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
  type PointerEvent as ReactPointerEvent,
} from 'react';

import type { MessageKey } from '@/i18n/types';
import type { Translate } from '../types';

type OverlayPlacementApi = NonNullable<NonNullable<Window['electron']>['overlayPlacement']>;
type Placements = Awaited<ReturnType<OverlayPlacementApi['get']>>;
type PlacementKind = keyof Placements;
type Cell = Placements[PlacementKind];

// The main process validates cells against the same grid (ipc/schemas/overlayPlacement.ts).
const ROW_KEYS = [
  'settings.overlayPosition.row.0',
  'settings.overlayPosition.row.1',
  'settings.overlayPosition.row.2',
] as const satisfies readonly MessageKey[];
const COLUMN_KEYS = [
  'settings.overlayPosition.column.0',
  'settings.overlayPosition.column.1',
  'settings.overlayPosition.column.2',
  'settings.overlayPosition.column.3',
  'settings.overlayPosition.column.4',
] as const satisfies readonly MessageKey[];
const ROWS = ROW_KEYS.length;
const COLUMNS = COLUMN_KEYS.length;

const KINDS: readonly { kind: PlacementKind; title: MessageKey; description: MessageKey }[] = [
  {
    kind: 'suggestion',
    title: 'settings.overlayPosition.kind.suggestion',
    description: 'settings.overlayPosition.kind.suggestion.description',
  },
  {
    kind: 'started',
    title: 'settings.overlayPosition.kind.started',
    description: 'settings.overlayPosition.kind.started.description',
  },
  {
    kind: 'history',
    title: 'settings.overlayPosition.kind.history',
    description: 'settings.overlayPosition.kind.history.description',
  },
];

const ARROW_STEPS: Readonly<Record<string, Readonly<{ row: number; column: number }>>> = {
  ArrowUp: { row: -1, column: 0 },
  ArrowDown: { row: 1, column: 0 },
  ArrowLeft: { row: 0, column: -1 },
  ArrowRight: { row: 0, column: 1 },
};

function clampIndex(value: number, count: number): number {
  return Math.max(0, Math.min(value, count - 1));
}

function sameCell(a: Cell, b: Cell): boolean {
  return a.row === b.row && a.column === b.column;
}

function OverlayPositionGrid({
  cell,
  label,
  t,
  onMove,
}: {
  cell: Cell;
  label: string;
  t: Translate;
  onMove: (cell: Cell) => void;
}) {
  const blockRef = useRef<HTMLDivElement>(null);
  // While the pointer is down the block snaps to the cell under it; the drop saves it.
  const [dragCell, setDragCell] = useState<Cell | null>(null);
  const shown = dragCell ?? cell;

  const cellAt = (event: ReactPointerEvent<HTMLDivElement>): Cell => {
    const rect = event.currentTarget.getBoundingClientRect();
    return {
      row: clampIndex(Math.floor(((event.clientY - rect.top) / rect.height) * ROWS), ROWS),
      column: clampIndex(Math.floor(((event.clientX - rect.left) / rect.width) * COLUMNS), COLUMNS),
    };
  };

  const handleKeyDown = (event: ReactKeyboardEvent<HTMLDivElement>) => {
    const step = ARROW_STEPS[event.key];
    if (!step) return;
    event.preventDefault();
    const next = {
      row: clampIndex(cell.row + step.row, ROWS),
      column: clampIndex(cell.column + step.column, COLUMNS),
    };
    if (!sameCell(next, cell)) onMove(next);
  };

  return (
    <div
      className="overlay-position-screen"
      onPointerDown={(event) => {
        if (event.button !== 0) return;
        event.preventDefault();
        event.currentTarget.setPointerCapture(event.pointerId);
        blockRef.current?.focus();
        setDragCell(cellAt(event));
      }}
      onPointerMove={(event) => {
        if (dragCell !== null) setDragCell(cellAt(event));
      }}
      onPointerUp={(event) => {
        if (dragCell === null) return;
        const target = cellAt(event);
        setDragCell(null);
        if (!sameCell(target, cell)) onMove(target);
      }}
      onPointerCancel={() => setDragCell(null)}
    >
      {ROW_KEYS.map((_, row) =>
        COLUMN_KEYS.map((__, column) => (
          <div
            key={`${row}-${column}`}
            aria-hidden="true"
            className="overlay-position-cell"
            style={{ gridRow: row + 1, gridColumn: column + 1 }}
          />
        ))
      )}
      <div
        ref={blockRef}
        role="slider"
        tabIndex={0}
        aria-label={label}
        aria-valuemin={1}
        aria-valuemax={ROWS * COLUMNS}
        aria-valuenow={shown.row * COLUMNS + shown.column + 1}
        aria-valuetext={t('settings.overlayPosition.cell', {
          row: t(ROW_KEYS[shown.row]),
          column: t(COLUMN_KEYS[shown.column]),
        })}
        className={[
          'overlay-position-block',
          dragCell === null ? null : 'overlay-position-block--dragging',
        ]
          .filter(Boolean)
          .join(' ')}
        style={{ gridRow: shown.row + 1, gridColumn: shown.column + 1 }}
        onKeyDown={handleKeyDown}
      />
    </div>
  );
}

export function OverlayPositionSection({ t }: { t: Translate }) {
  const [placements, setPlacements] = useState<Placements | null>(null);
  const [status, setStatus] = useState<'loading' | 'load_failed' | 'save_failed' | null>('loading');
  // The last placements the main process confirmed; a failed save returns to them.
  const savedRef = useRef<Placements | null>(null);

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const api = window.electron?.overlayPlacement;
        if (!api) throw new Error('Overlay placement bridge is unavailable.');
        const loaded = await api.get();
        if (cancelled) return;
        savedRef.current = loaded;
        setPlacements(loaded);
        setStatus(null);
      } catch {
        if (!cancelled) setStatus('load_failed');
      }
    };
    void load();
    return () => {
      cancelled = true;
    };
  }, []);

  const move = async (kind: PlacementKind, cell: Cell) => {
    setPlacements((current) => current && { ...current, [kind]: cell });
    setStatus(null);
    try {
      const api = window.electron?.overlayPlacement;
      if (!api) throw new Error('Overlay placement bridge is unavailable.');
      savedRef.current = await api.set({ kind, cell });
    } catch {
      setPlacements(savedRef.current);
      setStatus('save_failed');
    }
  };

  return (
    <div className="dashboard-section">
      <h3 className="dashboard-section-title">{t('settings.overlayPosition.title')}</h3>
      <p className="dashboard-section-description">{t('settings.overlayPosition.description')}</p>
      {placements ? (
        <div className="overlay-position-list">
          {KINDS.map(({ kind, title, description }) => (
            <div key={kind} className="settings-control-row overlay-position-row">
              <div className="settings-control-copy">
                <div className="settings-control-title">{t(title)}</div>
                <p className="dashboard-section-description settings-control-description settings-control-description--compact">
                  {t(description)}
                </p>
              </div>
              <OverlayPositionGrid
                cell={placements[kind]}
                label={t('settings.overlayPosition.gridLabel', { kind: t(title) })}
                t={t}
                onMove={(cell) => void move(kind, cell)}
              />
            </div>
          ))}
        </div>
      ) : null}
      {status === 'loading' ? (
        <p className="settings-section-status">{t('settings.loadingStatus')}</p>
      ) : null}
      {status === 'load_failed' || status === 'save_failed' ? (
        <p className="settings-section-status settings-section-error" role="alert">
          {t(
            status === 'load_failed'
              ? 'settings.overlayPosition.loadFailed'
              : 'settings.overlayPosition.saveFailed'
          )}
        </p>
      ) : null}
    </div>
  );
}
