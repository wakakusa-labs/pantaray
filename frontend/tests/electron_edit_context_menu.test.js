const assert = require('node:assert/strict');
const { EventEmitter } = require('node:events');
const { test } = require('node:test');

const { showEditContextMenus } = require('../electron/dist/ui/editContextMenu.js');

const ALL_FLAGS = { canCut: true, canCopy: true, canPaste: true, canSelectAll: true };
const NO_FLAGS = { canCut: false, canCopy: false, canPaste: false, canSelectAll: false };

/** Right-clicks a page with these params; returns the menu shown (or null) and the page's calls. */
function rightClick(params, language = 'ja') {
  const app = new EventEmitter();
  const shown = [];
  const window = { id: 'overlay' };
  showEditContextMenus({
    app,
    menu: {
      buildFromTemplate: (template) => ({
        popup: (options) => shown.push({ template, options }),
      }),
    },
    windowFor: () => window,
    getUiLanguage: () => language,
  });
  const calls = [];
  const contents = new EventEmitter();
  for (const method of ['cut', 'copy', 'paste', 'selectAll'])
    contents[method] = () => calls.push(method);
  app.emit('web-contents-created', {}, contents);
  contents.emit('context-menu', {}, params);
  assert.ok(shown.length <= 1);
  if (shown.length === 1) assert.equal(shown[0].options.window, window);
  return { menu: shown[0]?.template ?? null, calls };
}

test('selected text outside a field offers Copy, which copies from the clicked page', () => {
  const { menu, calls } = rightClick({
    isEditable: false,
    editFlags: { ...NO_FLAGS, canCopy: true },
  });
  assert.deepEqual(
    menu.map((item) => item.label),
    ['コピー']
  );
  menu[0].click();
  assert.deepEqual(calls, ['copy']);
});

test('a click outside a field with nothing selected opens no menu', () => {
  assert.equal(rightClick({ isEditable: false, editFlags: NO_FLAGS }).menu, null);
});

test('a text field offers Cut, Copy, Paste and Select All, enabled as the page reports', () => {
  const { menu, calls } = rightClick(
    { isEditable: true, editFlags: { ...ALL_FLAGS, canCut: false, canCopy: false } },
    'en'
  );
  assert.deepEqual(
    menu.map((item) => [item.type === 'separator' ? '-' : item.label, item.enabled]),
    [
      ['Cut', false],
      ['Copy', false],
      ['Paste', true],
      ['-', undefined],
      ['Select All', true],
    ]
  );
  for (const item of menu) item.click?.();
  assert.deepEqual(calls, ['cut', 'copy', 'paste', 'selectAll']);
});
