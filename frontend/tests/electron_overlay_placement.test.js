const assert = require('assert');
const fs = require('fs');
const os = require('os');
const path = require('path');
const { test } = require('node:test');

const {
  DEFAULT_OVERLAY_PLACEMENTS,
  createOverlayPlacementStore,
} = require('../electron/dist/settings/overlayPlacement.js');
const { resolveOverlayPlacement } = require('../electron/dist/windows/overlayPlacement.js');

function tempDir(t) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'pantaray-overlay-placement-'));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  return dir;
}

function openStore(dir) {
  const reports = [];
  const store = createOverlayPlacementStore({
    userDataDir: dir,
    reportInvalid: (reason) => reports.push(reason),
  });
  return { store, reports };
}

const settingsFile = (dir) => path.join(dir, 'overlay-placement.json');

test('a missing settings file gives today’s placement for every kind', (t) => {
  const { store, reports } = openStore(tempDir(t));
  assert.deepEqual(store.get(), {
    suggestion: { row: 0, column: 4 },
    started: { row: 1, column: 2 },
    history: { row: 1, column: 2 },
  });
  assert.deepEqual(reports, []);
});

test('a saved cell is kept for its kind only and read back after a restart', (t) => {
  const dir = tempDir(t);
  const { store } = openStore(dir);
  assert.deepEqual(store.set({ kind: 'started', cell: { row: 2, column: 0 } }), {
    ...DEFAULT_OVERLAY_PLACEMENTS,
    started: { row: 2, column: 0 },
  });
  store.set({ kind: 'history', cell: { row: 0, column: 1 } });

  const reopened = openStore(dir);
  assert.deepEqual(reopened.store.get(), {
    suggestion: { row: 0, column: 4 },
    started: { row: 2, column: 0 },
    history: { row: 0, column: 1 },
  });
  assert.deepEqual(reopened.reports, []);
  assert.deepEqual(
    fs.readdirSync(dir).filter((name) => name.includes('.tmp-')),
    []
  );
});

test('an unreadable stored value is reported and that kind falls back to its default', (t) => {
  const dir = tempDir(t);
  fs.writeFileSync(
    settingsFile(dir),
    JSON.stringify({
      suggestion: { row: 3, column: 0 },
      started: { row: 2, column: 4 },
      history: { row: 1, column: 1.5 },
      unknown: { row: 0, column: 0 },
    })
  );
  const { store, reports } = openStore(dir);
  assert.deepEqual(store.get(), {
    suggestion: { row: 0, column: 4 },
    started: { row: 2, column: 4 },
    history: { row: 1, column: 2 },
  });
  assert.deepEqual(reports, ['invalid_suggestion', 'invalid_history']);

  for (const [contents, reason] of [
    ['{', 'malformed_json'],
    ['[]', 'not_an_object'],
    ['null', 'not_an_object'],
  ]) {
    fs.writeFileSync(settingsFile(dir), contents);
    const reopened = openStore(dir);
    assert.deepEqual(reopened.store.get(), DEFAULT_OVERLAY_PLACEMENTS);
    assert.deepEqual(reopened.reports, [reason]);
  }
});

test('a failed save keeps the previous placement', (t) => {
  const dir = tempDir(t);
  const { store } = openStore(dir);
  fs.mkdirSync(settingsFile(dir));
  assert.throws(() => store.set({ kind: 'suggestion', cell: { row: 2, column: 4 } }));
  assert.deepEqual(store.get(), DEFAULT_OVERLAY_PLACEMENTS);
});

// A macOS work area: below a 33 px menu bar, above a 70 px Dock.
const MAC_WORK_AREA = { x: 0, y: 33, width: 1512, height: 879 };

test('the default cells reproduce the placement before this setting existed', () => {
  // Before: centered on the work area, and top-right at a 20 px margin stacking by 132 px.
  const centered = {
    x: Math.round(MAC_WORK_AREA.x + (MAC_WORK_AREA.width - 520) / 2),
    y: Math.round(MAC_WORK_AREA.y + (MAC_WORK_AREA.height - 120) / 2),
  };
  for (const kind of ['started', 'history']) {
    assert.deepEqual(resolveOverlayPlacement(MAC_WORK_AREA, DEFAULT_OVERLAY_PLACEMENTS[kind], 0), {
      ...centered,
      anchor: 'center',
    });
  }
  const workArea = { x: 0, y: 0, width: 1440, height: 900 };
  const maxRows = Math.floor((900 - 40) / 132);
  for (const index of [0, 1, 2, maxRows - 1, maxRows, 40]) {
    assert.deepEqual(resolveOverlayPlacement(workArea, DEFAULT_OVERLAY_PLACEMENTS.suggestion, index), {
      x: 1440 - 520 - 20,
      y: 20 + Math.min(index, maxRows - 1) * 132,
      anchor: 'top',
    });
  }
});

test('each cell aligns to its margin at the edges and centers on its column inside', () => {
  const at = (row, column, index = 0) =>
    resolveOverlayPlacement(MAC_WORK_AREA, { row, column }, index);
  const xs = [0, 1, 2, 3, 4].map((column) => at(0, column).x);
  // Columns 1 and 3 center on 1512 * 3/10 and 1512 * 7/10.
  assert.deepEqual(xs, [20, 194, 496, 798, 972]);
  assert.deepEqual(
    [0, 1, 2].map((row) => at(row, 0)),
    [
      { x: 20, y: 53, anchor: 'top' },
      { x: 20, y: 413, anchor: 'center' },
      { x: 20, y: 772, anchor: 'bottom' },
    ]
  );

  // Suggestions stack upward from the bottom row and stop at the top margin.
  assert.equal(at(2, 4, 1).y, 772 - 132);
  const bottomStack = [0, 1, 2, 3, 4, 5, 6, 7, 20].map((index) => at(2, 4, index).y);
  assert.ok(bottomStack.every((y) => y >= 53 && y + 120 <= 33 + 879 - 20));
  assert.equal(bottomStack.at(-1), bottomStack.at(-2));
  // From the middle row they stack downward and stay on screen.
  const middleStack = [0, 1, 2, 3, 20].map((index) => at(1, 4, index).y);
  assert.deepEqual(middleStack.slice(0, 3), [413, 545, 677]);
  assert.ok(middleStack.every((y) => y + 120 <= 33 + 879 - 20));
});

test('a narrow work area keeps the inner columns inside its margins', () => {
  const narrow = { x: 100, y: 0, width: 700, height: 500 };
  for (const column of [0, 1, 2, 3, 4]) {
    const { x } = resolveOverlayPlacement(narrow, { row: 1, column }, 0);
    assert.ok(x >= 120 && x + 520 <= 780, `column ${column} at ${x}`);
  }
});
