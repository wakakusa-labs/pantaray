const assert = require('assert');
const fs = require('fs');
const os = require('os');
const path = require('path');
const { test } = require('node:test');

const {
  ConversationOverlayStateError,
  createConversationOverlayOwner,
} = require('../electron/dist/windows/conversationOverlay.js');
const {
  GlobalShortcutRegistrationError,
  GlobalShortcutSettingsError,
  createGlobalShortcutController,
  createGlobalShortcutStore,
} = require('../electron/dist/main_runtime/globalShortcutController.js');

// The gate holds only with both permissions; either one missing closes it.
const GRANTED = { accessibility: true, screenRecording: true };
const NO_SCREEN_RECORDING = { accessibility: true, screenRecording: false };
const NO_ACCESSIBILITY = { accessibility: false, screenRecording: true };

function createOverlayOwnerHarness() {
  const state = {
    runtimeState: { status: 'unknown', message: null, owner: null },
    openCalls: [],
    destroyed: [],
    bindings: [],
    refreshed: [],
    refreshOutcome: Promise.resolve('refreshed'),
    revealed: [],
    associations: new Map(),
    openWindows: new Set(),
    openResult: 'created',
    permissions: GRANTED,
    introPresentedCount: 0,
  };
  let overlayIdSeq = 0;
  const closeOverlayWindow = (overlayId) => {
    state.openWindows.delete(overlayId);
    // The window layer releases the Action association when the window closes.
    for (const [actionId, mapped] of state.associations) {
      if (mapped === overlayId) state.associations.delete(actionId);
    }
  };
  const owner = createConversationOverlayOwner({
    getRuntimeState: () => state.runtimeState,
    createOverlayId: () => `overlay-${++overlayIdSeq}`,
    openOverlay: (overlayId, actionId) => {
      state.openCalls.push([overlayId, actionId]);
      state.openWindows.add(overlayId);
      return state.openResult;
    },
    destroyOverlay: (overlayId) => {
      state.destroyed.push(overlayId);
      closeOverlayWindow(overlayId);
    },
    bindActionToOverlay: (actionId, overlayId) => {
      state.bindings.push([actionId, overlayId]);
      state.associations.set(actionId, overlayId);
    },
    resolveOverlayIdForAction: (actionId) => state.associations.get(actionId) ?? null,
    hasOverlayWindow: (overlayId) => state.openWindows.has(overlayId),
    refreshAndResumeConversation: (actionId) => {
      state.refreshed.push(actionId);
      return state.refreshOutcome;
    },
    revealOverlay: (overlayId) => state.revealed.push(overlayId),
    readCapturePermissions: () => state.permissions,
    presentRecordingIntro: () => state.introPresentedCount++,
  });
  return { owner, state, closeOverlayWindow };
}

test('every new conversation request opens a fresh subject-bound Overlay identity', () => {
  const { owner, state } = createOverlayOwnerHarness();

  assert.equal(owner.openNewConversationOverlay(), 'initializing');
  assert.deepEqual(state.openCalls, []);
  state.runtimeState = { status: 'ready', message: null, owner: null };
  assert.equal(owner.openNewConversationOverlay(), 'initializing');
  state.runtimeState = { status: 'degraded', message: 'helper failed', owner: null };
  assert.throws(owner.openNewConversationOverlay, /Local runtime is unavailable/);

  state.runtimeState = { status: 'ready', message: null, owner: { id: 'user-a', kind: 'account' } };
  assert.equal(owner.openNewConversationOverlay(), 'created');
  assert.equal(owner.openNewConversationOverlay(), 'created');
  assert.deepEqual(state.openCalls, [
    ['standalone:overlay-1', null],
    ['standalone:overlay-2', null],
  ]);

  owner.onOwnerChanged('user-a');
  assert.deepEqual(state.destroyed, []);
  owner.onOwnerChanged('user-b');
  assert.deepEqual(state.destroyed, ['standalone:overlay-1', 'standalone:overlay-2']);

  state.runtimeState = { status: 'ready', message: null, owner: { id: 'user-b', kind: 'account' } };
  assert.equal(owner.openNewConversationOverlay(), 'created');
  owner.onOwnerChanged(null);
  assert.deepEqual(state.destroyed, [
    'standalone:overlay-1',
    'standalone:overlay-2',
    'standalone:overlay-3',
  ]);
});

test('history conversation overlay binds its Action before opening and refreshing', () => {
  const { owner, state } = createOverlayOwnerHarness();
  state.runtimeState = { status: 'ready', message: null, owner: { id: 'user-a', kind: 'account' } };

  assert.throws(() => owner.openActionConversationOverlay('  '), ConversationOverlayStateError);
  assert.equal(owner.openActionConversationOverlay(' action-1 '), 'created');
  assert.deepEqual(state.bindings, [['action-1', 'conversation:action-1']]);
  assert.deepEqual(state.openCalls, [['conversation:action-1', 'action-1']]);
  assert.deepEqual(state.refreshed, ['action-1']);

  state.openResult = 'focused';
  assert.equal(owner.openActionConversationOverlay('action-2'), 'focused');
  assert.equal(owner.openNewConversationOverlay(), 'focused');

  owner.onOwnerChanged('user-b');
  assert.deepEqual(state.destroyed, [
    'conversation:action-1',
    'conversation:action-2',
    'standalone:overlay-1',
  ]);

  state.runtimeState = { status: 'syncing', message: null, owner: null };
  assert.equal(owner.openActionConversationOverlay('action-3'), 'initializing');
  assert.deepEqual(state.refreshed, ['action-1', 'action-2']);
});

test('an Action conversation window is revealed by main only when its conversation was not read', async () => {
  const { owner, state } = createOverlayOwnerHarness();
  state.runtimeState = { status: 'ready', message: null, owner: { id: 'user-a', kind: 'account' } };
  const settle = () => new Promise((resolve) => setImmediate(resolve));

  // A read conversation reaches the window, which shows itself once it is laid out.
  owner.openActionConversationOverlay('action-1');
  await settle();
  assert.deepEqual(state.revealed, []);

  for (const outcome of [Promise.resolve('failed'), Promise.resolve('superseded')]) {
    state.refreshOutcome = outcome;
    owner.openActionConversationOverlay('action-2');
  }
  state.refreshOutcome = Promise.reject(new Error('read failed'));
  const logged = [];
  const originalError = console.error;
  console.error = (...args) => logged.push(args);
  try {
    owner.openActionConversationOverlay('action-3');
    await settle();
  } finally {
    console.error = originalError;
  }
  assert.deepEqual(state.revealed, [
    'conversation:action-2',
    'conversation:action-2',
    'conversation:action-3',
  ]);
  assert.equal(logged.length, 1);
});

test('a standalone Overlay that already holds the Action is reused instead of duplicated', () => {
  const { owner, state } = createOverlayOwnerHarness();
  state.runtimeState = { status: 'ready', message: null, owner: { id: 'user-a', kind: 'account' } };

  assert.equal(owner.openNewConversationOverlay(), 'created');
  // The submit handler binds the accepted Action to the standalone Overlay.
  state.associations.set('action-1', 'standalone:overlay-1');
  state.openResult = 'focused';

  assert.equal(owner.openActionConversationOverlay('action-1'), 'focused');
  assert.deepEqual(state.openCalls, [
    ['standalone:overlay-1', null],
    ['standalone:overlay-1', 'action-1'],
  ]);
  assert.deepEqual(state.refreshed, ['action-1']);

  // An Action displayed by an Overlay this owner does not own is focused where
  // it already is: rebinding it would redirect its live updates to a second
  // window and drop the association when that window closes.
  state.associations.set('action-2', 'suggestion-1');
  state.openWindows.add('suggestion-1');
  assert.equal(owner.openActionConversationOverlay('action-2'), 'focused');
  assert.deepEqual(state.openCalls.at(-1), ['suggestion-1', 'action-2']);
  assert.deepEqual(state.bindings, []);
  assert.deepEqual(state.refreshed, ['action-1', 'action-2']);

  // A stale association whose window is gone opens this owner's own window.
  state.associations.set('action-3', 'suggestion-2');
  assert.equal(owner.openActionConversationOverlay('action-3'), 'focused');
  assert.deepEqual(state.openCalls.at(-1), ['conversation:action-3', 'action-3']);
  assert.deepEqual(state.bindings, [['action-3', 'conversation:action-3']]);
});

test('a conversation without the macOS capture permissions opens the recording screen instead', () => {
  const { owner, state } = createOverlayOwnerHarness();
  state.runtimeState = { status: 'ready', message: null, owner: { id: 'user-a', kind: 'account' } };
  state.permissions = NO_SCREEN_RECORDING;

  // Without the permissions the recorder needs, the app can read nothing, so no
  // window is created at all: the main window is brought forward and what was
  // requested is remembered, which is what the recording screen reads back.
  assert.deepEqual(owner.readGateState(), {
    permissions: NO_SCREEN_RECORDING,
    introRequested: false,
    introDismissed: false,
  });
  assert.equal(owner.openNewConversationOverlay(), 'recording_intro');
  assert.equal(owner.openActionConversationOverlay('action-1'), 'recording_intro');
  assert.equal(state.introPresentedCount, 2);
  assert.equal(owner.readGateState().introRequested, true);
  assert.deepEqual(state.openCalls, []);
  assert.deepEqual(state.bindings, []);
  assert.deepEqual(state.refreshed, []);

  // Granted permissions are the only thing this gate asks for: recording being off
  // is not one of its inputs, so a conversation the user starts opens either way.
  state.permissions = GRANTED;
  assert.equal(owner.openNewConversationOverlay(), 'created');
  assert.deepEqual(state.openCalls, [['standalone:overlay-1', null]]);

  // A start that reaches 'started' proves the permissions, and opens the last request.
  owner.openPendingRequest();
  assert.deepEqual(state.openCalls.at(-1), ['conversation:action-1', 'action-1']);
  assert.deepEqual(state.refreshed, ['action-1']);

  // The request is consumed, so a later start does not reopen it and the recording
  // screen is no longer asked for.
  owner.openPendingRequest();
  assert.deepEqual(state.openCalls.at(-1), ['conversation:action-1', 'action-1']);
  assert.equal(state.openCalls.length, 2);
});

test('a deferred conversation opens as soon as the gate is read granted', () => {
  const { owner, state } = createOverlayOwnerHarness();
  state.runtimeState = { status: 'ready', message: null, owner: { id: 'user-a', kind: 'account' } };
  state.permissions = NO_SCREEN_RECORDING;

  assert.equal(owner.openActionConversationOverlay('action-1'), 'recording_intro');

  // The permission is granted in System Settings, outside the app, so this read is
  // where the app learns it arrived — and the recording screen closes on the same
  // read. A request left waiting for a start made from that screen would never open.
  state.permissions = GRANTED;
  assert.deepEqual(owner.readGateState(), {
    permissions: GRANTED,
    introRequested: false,
    introDismissed: false,
  });
  assert.deepEqual(state.openCalls, [['conversation:action-1', 'action-1']]);

  // Consumed by that read, so it cannot reappear in front of an unrelated start.
  owner.readGateState();
  owner.openPendingRequest();
  assert.equal(state.openCalls.length, 1);
});

test('a replay that finds the gate closed defers the conversation again', () => {
  // A recorder that is already running reports a start as 'started' without asking
  // macOS anything, so a start is never on its own proof that the gate is open.
  const { owner, state } = createOverlayOwnerHarness();
  state.runtimeState = { status: 'ready', message: null, owner: { id: 'user-a', kind: 'account' } };
  state.permissions = NO_SCREEN_RECORDING;

  assert.equal(owner.openActionConversationOverlay('action-1'), 'recording_intro');
  owner.openPendingRequest();
  assert.deepEqual(state.openCalls, []);
  assert.equal(state.introPresentedCount, 2);

  // Still deferred, so it opens once the permissions are actually there.
  state.permissions = GRANTED;
  owner.openPendingRequest();
  assert.deepEqual(state.openCalls, [['conversation:action-1', 'action-1']]);
});

test('answering "later" drops the deferred conversation and is remembered for the app run', () => {
  const { owner, state } = createOverlayOwnerHarness();
  state.runtimeState = { status: 'ready', message: null, owner: { id: 'user-a', kind: 'account' } };
  state.permissions = NO_ACCESSIBILITY;

  assert.equal(owner.openNewConversationOverlay(), 'recording_intro');
  // The answer outlives the window that gave it: the recording screen lives in the
  // main window, and reopening that window must not ask again.
  assert.deepEqual(owner.dismissIntro(), {
    permissions: NO_ACCESSIBILITY,
    introRequested: false,
    introDismissed: true,
  });
  state.permissions = GRANTED;
  owner.openPendingRequest();
  assert.deepEqual(state.openCalls, []);
});

test('a deferred conversation and an answered recording screen are dropped by a subject change', () => {
  const { owner, state } = createOverlayOwnerHarness();
  state.runtimeState = { status: 'ready', message: null, owner: { id: 'user-a', kind: 'account' } };
  state.permissions = NO_SCREEN_RECORDING;

  assert.equal(owner.openActionConversationOverlay('action-1'), 'recording_intro');
  owner.dismissIntro();
  owner.onOwnerChanged('user-b');
  // The next subject never answered this screen, so it is asked again — and never
  // inherits the request the previous one made.
  assert.deepEqual(owner.readGateState(), {
    permissions: NO_SCREEN_RECORDING,
    introRequested: false,
    introDismissed: false,
  });
  state.runtimeState = { status: 'ready', message: null, owner: { id: 'user-b', kind: 'account' } };
  state.permissions = GRANTED;
  owner.openPendingRequest();
  assert.deepEqual(state.openCalls, []);
});

test('a closed conversation Overlay is forgotten instead of destroyed on subject change', () => {
  const { owner, state, closeOverlayWindow } = createOverlayOwnerHarness();
  state.runtimeState = { status: 'ready', message: null, owner: { id: 'user-a', kind: 'account' } };

  assert.equal(owner.openActionConversationOverlay('action-1'), 'created');
  assert.equal(owner.openNewConversationOverlay(), 'created');
  closeOverlayWindow('conversation:action-1');

  // Reopening the closed conversation opens a new window instead of focusing a
  // destroyed one, and the owner no longer holds the closed id.
  assert.equal(owner.openActionConversationOverlay('action-1'), 'created');
  assert.deepEqual(state.openCalls.at(-1), ['conversation:action-1', 'action-1']);
  closeOverlayWindow('conversation:action-1');
  owner.onOwnerChanged('user-b');
  assert.deepEqual(state.destroyed, ['standalone:overlay-1']);
});

function createShortcutHarness({
  initial = null,
  initialAccelerator = 'Option+Space',
  loadError = null,
  registerResult = true,
  saveError = null,
} = {}) {
  const calls = [];
  const callbacks = new Map();
  let stored = initial;
  let canRegister = registerResult;
  const controller = createGlobalShortcutController({
    registry: {
      register(accelerator, callback) {
        calls.push(`register:${accelerator}`);
        if (canRegister) callbacks.set(accelerator, callback);
        return canRegister;
      },
      unregister(accelerator) {
        calls.push(`unregister:${accelerator}`);
        callbacks.delete(accelerator);
      },
      unregisterAll() {
        calls.push('unregisterAll');
        callbacks.clear();
      },
    },
    initialAccelerator,
    store: {
      load: () => {
        if (loadError) throw loadError;
        return stored;
      },
      save: (accelerator) => {
        calls.push(`save:${accelerator}`);
        if (saveError) throw saveError;
        stored = accelerator;
      },
    },
    onShortcut: () => calls.push('opened'),
  });
  return {
    callbacks,
    calls,
    controller,
    getStored: () => stored,
    setRegisterResult: (result) => {
      canRegister = result;
    },
  };
}

test('shortcut startup registers the stored accelerator or persists the macOS initial value', () => {
  const empty = createShortcutHarness();
  empty.controller.initialize();
  assert.deepEqual(empty.calls, ['unregisterAll', 'register:Option+Space', 'save:Option+Space']);
  assert.deepEqual(empty.controller.getState(), {
    accelerator: 'Option+Space',
    failure: null,
  });
  assert.equal(empty.getStored(), 'Option+Space');

  const noPlatformDefault = createShortcutHarness({ initialAccelerator: null });
  noPlatformDefault.controller.initialize();
  assert.deepEqual(noPlatformDefault.calls, ['unregisterAll']);
  assert.deepEqual(noPlatformDefault.controller.getState(), {
    accelerator: null,
    failure: null,
  });

  const stored = createShortcutHarness({ initial: 'CommandOrControl+Shift+Space' });
  stored.controller.initialize();
  assert.deepEqual(stored.calls, ['unregisterAll', 'register:CommandOrControl+Shift+Space']);
  stored.callbacks.get('CommandOrControl+Shift+Space')();
  assert.equal(stored.calls.at(-1), 'opened');

  const unavailable = createShortcutHarness({
    initial: 'CommandOrControl+1',
    registerResult: false,
  });
  assert.throws(() => unavailable.controller.initialize(), GlobalShortcutRegistrationError);
  assert.deepEqual(unavailable.calls, ['unregisterAll', 'register:CommandOrControl+1']);
  assert.deepEqual(unavailable.controller.getState(), {
    accelerator: 'CommandOrControl+1',
    failure: 'registration_unavailable',
  });

  const unreadable = createShortcutHarness({ loadError: new Error('permission denied') });
  assert.throws(() => unreadable.controller.initialize(), /permission denied/);
  assert.deepEqual(unreadable.controller.getState(), {
    accelerator: null,
    failure: 'settings_unreadable',
  });

  const writeError = new Error('disk full');
  const defaultUnwritable = createShortcutHarness({ saveError: writeError });
  assert.throws(() => defaultUnwritable.controller.initialize(), writeError);
  assert.deepEqual(defaultUnwritable.calls, [
    'unregisterAll',
    'register:Option+Space',
    'save:Option+Space',
    'unregister:Option+Space',
  ]);
  assert.deepEqual(defaultUnwritable.controller.getState(), {
    accelerator: 'Option+Space',
    failure: 'persistence_failed',
  });
});

test('shortcut changes register, persist, then release the previous accelerator', () => {
  const harness = createShortcutHarness({ initial: 'CommandOrControl+1' });
  harness.controller.initialize();
  harness.calls.length = 0;
  assert.deepEqual(harness.controller.changeShortcut('CommandOrControl+1'), {
    ok: true,
    state: { accelerator: 'CommandOrControl+1', failure: null },
  });
  assert.deepEqual(harness.calls, []);

  assert.deepEqual(harness.controller.changeShortcut('CommandOrControl+2'), {
    ok: true,
    state: { accelerator: 'CommandOrControl+2', failure: null },
  });
  assert.deepEqual(harness.calls, [
    'register:CommandOrControl+2',
    'save:CommandOrControl+2',
    'unregister:CommandOrControl+1',
  ]);
  assert.deepEqual(harness.controller.getState(), {
    accelerator: 'CommandOrControl+2',
    failure: null,
  });
  assert.equal(harness.getStored(), 'CommandOrControl+2');
});

test('shortcut registration or persistence failure preserves the previous shortcut', () => {
  const unavailable = createShortcutHarness({ initial: 'CommandOrControl+1' });
  unavailable.controller.initialize();
  unavailable.calls.length = 0;
  unavailable.setRegisterResult(false);
  assert.deepEqual(unavailable.controller.changeShortcut('CommandOrControl+2'), {
    ok: false,
    reason: 'registration_unavailable',
    state: { accelerator: 'CommandOrControl+1', failure: null },
  });
  assert.deepEqual(unavailable.calls, ['register:CommandOrControl+2']);
  assert.deepEqual(unavailable.controller.getState(), {
    accelerator: 'CommandOrControl+1',
    failure: null,
  });
  assert.equal(unavailable.callbacks.has('CommandOrControl+1'), true);
  assert.equal(unavailable.getStored(), 'CommandOrControl+1');

  const writeError = new Error('disk full');
  const unwritable = createShortcutHarness({
    initial: 'CommandOrControl+1',
    saveError: writeError,
  });
  unwritable.controller.initialize();
  unwritable.calls.length = 0;
  assert.deepEqual(unwritable.controller.changeShortcut('CommandOrControl+2'), {
    ok: false,
    reason: 'persistence_failed',
    state: { accelerator: 'CommandOrControl+1', failure: null },
  });
  assert.deepEqual(unwritable.calls, [
    'register:CommandOrControl+2',
    'save:CommandOrControl+2',
    'unregister:CommandOrControl+2',
  ]);
  assert.equal(unwritable.callbacks.has('CommandOrControl+1'), true);
  assert.deepEqual(unwritable.controller.getState(), {
    accelerator: 'CommandOrControl+1',
    failure: null,
  });
});

test('shortcut store atomically persists validated settings without exposing malformed content', (t) => {
  const userDataDir = fs.mkdtempSync(path.join(os.tmpdir(), 'pantaray-shortcut-'));
  t.after(() => fs.rmSync(userDataDir, { recursive: true, force: true }));
  const store = createGlobalShortcutStore(userDataDir);
  assert.equal(store.load(), null);
  store.save('CommandOrControl+Shift+Space');
  assert.equal(store.load(), 'CommandOrControl+Shift+Space');
  assert.equal(fs.statSync(path.join(userDataDir, 'global-shortcut.json')).mode & 0o777, 0o600);

  fs.writeFileSync(path.join(userDataDir, 'global-shortcut.json'), '{"secret":"do-not-leak"');
  assert.throws(
    () => store.load(),
    (error) =>
      error instanceof GlobalShortcutSettingsError && !error.message.includes('do-not-leak')
  );
});

test('guest and retained account owners can open conversations without a cloud auth gate', () => {
  for (const kind of ['guest', 'account']) {
    const { owner, state } = createOverlayOwnerHarness();
    state.runtimeState = { status: 'ready', message: null, owner: { id: kind, kind } };
    assert.equal(owner.openNewConversationOverlay(), 'created');
    owner.onOwnerChanged(kind);
    assert.deepEqual(state.destroyed, []);
    // Owner invalidation closes old content before any helper response; opening
    // it again during syncing or a failed helper apply must not bypass the gate.
    state.runtimeState = { status: 'ready', message: null, owner: null };
    owner.onOwnerChanged(null);
    assert.deepEqual(state.destroyed, ['standalone:overlay-1']);
    assert.equal(owner.openActionConversationOverlay('action-1'), 'initializing');
    state.runtimeState = { status: 'degraded', message: 'helper failed', owner: null };
    assert.throws(() => owner.openActionConversationOverlay('action-1'), /Local runtime is unavailable/);
    assert.equal(state.openCalls.length, 1);
  }
});

for (const kind of ['new', 'action']) {
  test(`a pending ${kind} conversation survives permission completion during same-owner syncing`, () => {
    const { owner, state } = createOverlayOwnerHarness();
    const ready = { status: 'ready', message: null, owner: { id: 'alice', kind: 'account' } };
    state.runtimeState = ready;
    state.permissions = NO_SCREEN_RECORDING;
    assert.equal(kind === 'new' ? owner.openNewConversationOverlay()
      : owner.openActionConversationOverlay('action-1'), 'recording_intro');
    state.runtimeState = { status: 'syncing', message: null, owner: null };
    state.permissions = GRANTED;
    assert.equal(owner.readGateState().introRequested, true);
    owner.openPendingRequest();
    assert.deepEqual(state.openCalls, []);
    state.runtimeState = ready;
    assert.equal(owner.readGateState().introRequested, false);
    assert.deepEqual(state.openCalls, [kind === 'new'
      ? ['standalone:overlay-1', null] : ['conversation:action-1', 'action-1']]);
    owner.readGateState();
    assert.equal(state.openCalls.length, 1);
  });
}
