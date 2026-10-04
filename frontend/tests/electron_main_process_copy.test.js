const assert = require('assert');
const { test } = require('node:test');

const {
  formatMacAccelerator,
  getTrayMenuCopy,
  getWelcomeSuggestionText,
} = require('../electron/dist/ui/mainProcessCopy.js');

function status(overrides) {
  return {
    kind: 'checking',
    screenshotsEnabled: true,
    activeWindow: { appName: null, title: null },
    browserUrl: null,
    lastCaptureAt: null,
    lastCaptureResult: null,
    reasonLabel: 'checking',
    ...overrides,
  };
}

test('tray copy labels the new conversation action in each language', () => {
  assert.equal(getTrayMenuCopy('ja').newConversation, '新しい会話');
  assert.equal(getTrayMenuCopy('en').newConversation, 'New conversation');
});

test('tray copy explains recording statuses that pause or block activity recording', () => {
  const ja = getTrayMenuCopy('ja');
  const en = getTrayMenuCopy('en');

  assert.deepStrictEqual(ja.captureStatus(status({ kind: 'editing_paused' })), {
    label: 'Pantaray: ルール編集中',
    detail: '操作の記録は一時停止中です',
  });
  assert.deepStrictEqual(
    en.captureStatus(
      status({
        kind: 'blocked_by_ide',
        activeWindow: { appName: 'Cursor', title: '.env' },
      })
    ),
    {
      label: 'Out of scope: Cursor',
      detail: 'Blocked by IDE file rules',
    }
  );
});

test('tray copy warns that a failed stop preference may restart recording', () => {
  const ja = getTrayMenuCopy('ja');

  assert.deepStrictEqual(
    ja.captureStatus(
      status({ kind: 'unavailable', reasonLabel: 'capture_preference_write_failed' })
    ),
    {
      label: 'Pantaray: 停止設定を保存できません',
      detail: '次回起動時に操作の記録が再開する可能性があります',
    }
  );
});

test('tray copy keeps recorder component names out of the degraded status', () => {
  // The recorder reports degraded collectors by internal name (ax, chrome, ...).
  const degraded = status({ kind: 'degraded', reasonLabel: 'ax, content_snapshot' });

  for (const lang of ['ja', 'en']) {
    const { label, detail } = getTrayMenuCopy(lang).captureStatus(degraded);
    assert.ok(detail);
    assert.doesNotMatch(`${label}\n${detail}`, /\bax\b|content_snapshot|zanei/i);
  }
});

test('tray copy keeps recorder failure text out of the unavailable status', () => {
  for (const screenshotsEnabled of [true, false]) {
    const recorderFailure = status({
      kind: 'unavailable',
      screenshotsEnabled,
      reasonLabel: 'Zanei recorder stopped unexpectedly.',
    });

    for (const lang of ['ja', 'en']) {
      const { label, detail } = getTrayMenuCopy(lang).captureStatus(recorderFailure);
      assert.doesNotMatch(`${label}\n${detail}`, /zanei/i);
      assert.ok(detail);
      // Without an active recorder the menu has no Pause item to follow.
      if (!screenshotsEnabled) assert.doesNotMatch(detail, /一時停止|pause/i);
    }
  }
});

test('accelerators read as macOS draws them, modifiers in ⌃⌥⇧⌘ order', () => {
  assert.equal(formatMacAccelerator('Option+Space'), '⌥Space');
  assert.equal(formatMacAccelerator('Command+Shift+Alt+K'), '⌥⇧⌘K');
  assert.equal(formatMacAccelerator('Hyper+Space'), 'HyperSpace');
});

test('the welcome names the shortcut the user has, or only the button', () => {
  assert.equal(
    getWelcomeSuggestionText('ja', 'Option+Space'),
    'まずはあなたの仕事を理解するところから始めます。お役に立てそうなことが見つかったら、こちらから提案します。\n\n' +
      'それまでも、任せたい仕事があればいつでも ⌥Space か新しい会話ボタンで声をかけてください。'
  );
  assert.match(getWelcomeSuggestionText('ja', null), /いつでも新しい会話ボタンで声をかけてください。$/);
  assert.match(
    getWelcomeSuggestionText('en', 'Option+Space'),
    /just press ⌥Space or use the New conversation button\.$/
  );
  assert.match(getWelcomeSuggestionText('en', null), /just use the New conversation button\.$/);
});
