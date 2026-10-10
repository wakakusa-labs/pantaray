const assert = require('assert');
const { EventEmitter } = require('events');
const { test } = require('node:test');

const { createTaskDraftStore } = require('../electron/dist/actions/taskDraftStore.js');
const { registerTaskDraftHandlers } = require('../electron/dist/ipc/handlers/taskDrafts.js');
const { IpcSenderRejectedError } = require('../electron/dist/ipc/senderTrust.js');
const { IpcValidationError } = require('../electron/dist/ipc/schemas/error.js');

const FILE = {
  kind: 'file',
  attachmentId: '11111111-1111-4111-8111-111111111111',
  name: '議事録.pdf',
  byteSize: 12,
};
const IMAGE = { kind: 'image', storagePath: 'user-1/2026-10-11/a.png' };
const draft = (text, attachments = []) => ({ text, mentions: [], attachments });

// A store behind the real handlers, with two windows: the main window (1) and a small one (2).
function harness() {
  const discarded = [];
  let owner = 'user-1';
  const taskDrafts = createTaskDraftStore({
    discardStagedFile: (ownerId, attachmentId) => discarded.push([ownerId, attachmentId]),
  });
  const handlers = new Map();
  registerTaskDraftHandlers(
    {
      taskDrafts,
      windows: { getMainWindow: () => null },
      actions: {
        getCurrentSubjectId: () => owner,
        resolveOverlayIdForSender: (sender) => (sender.registered === false ? null : 'overlay'),
      },
    },
    { handle: (channel, handler) => handlers.set(channel, handler) }
  );
  const windows = new Map();
  const windowOf = (id) => {
    if (!windows.has(id)) {
      const sender = Object.assign(new EventEmitter(), {
        id,
        received: [],
        isDestroyed: () => false,
        send: (channel, change) => sender.received.push([channel, change]),
      });
      windows.set(id, sender);
    }
    return windows.get(id);
  };
  const invoke = (channel, id, payload) => handlers.get(channel)({ sender: windowOf(id) }, payload);
  return {
    discarded,
    windowOf,
    setOwner: (next) => (owner = next),
    open: (id, work) => invoke('action:openDraft', id, { work }),
    update: (id, work, value) => invoke('action:updateDraft', id, { work, draft: value }),
    close: (id, work) => invoke('action:closeDraft', id, { work }),
    discard: (attachmentId) => taskDrafts.discard(owner, attachmentId),
    invoke,
  };
}

test('a change reaches the other windows of that task, never its sender', () => {
  const app = harness();
  assert.equal(app.open(1, 'action:a1'), null);
  app.open(2, 'action:a1');
  app.open(1, 'action:a2');

  app.update(2, 'action:a1', draft('見積を直して', [FILE, IMAGE]));

  assert.deepEqual(app.windowOf(1).received, [
    ['action:draftChanged', { work: 'action:a1', draft: draft('見積を直して', [FILE, IMAGE]) }],
  ]);
  assert.deepEqual(app.windowOf(2).received, []);
  // A window that opens the task later starts from the same draft; another task has its own.
  assert.deepEqual(app.open(3, 'action:a1'), draft('見積を直して', [FILE, IMAGE]));
  assert.equal(app.open(3, 'action:a2'), null);

  // Cleared, as a send clears it: everyone else hears null, and no staged file is discarded.
  app.update(1, 'action:a1', draft(''));
  assert.deepEqual(app.windowOf(2).received.at(-1), [
    'action:draftChanged',
    { work: 'action:a1', draft: null },
  ]);
  assert.equal(app.open(3, 'action:a1'), null);
  assert.deepEqual(app.discarded, []);
});

test('a removed file is discarded only once no draft lists it, and never comes back', () => {
  const app = harness();
  app.open(1, 'action:a1');
  app.open(2, 'action:a1');
  app.update(2, 'action:a1', draft('', [FILE]));

  // The small window removes it: its discard arrives before its debounced draft does.
  app.discard(FILE.attachmentId);
  assert.deepEqual(app.discarded, []);
  app.update(2, 'action:a1', draft(''));
  assert.deepEqual(app.discarded, [['user-1', FILE.attachmentId]]);

  // The main window, not yet told, writes the old list: the file stays out, and it hears so.
  app.windowOf(1).received.length = 0;
  app.update(1, 'action:a1', draft('続き', [FILE]));
  assert.deepEqual(app.open(3, 'action:a1'), draft('続き'));
  assert.deepEqual(app.windowOf(1).received, [
    ['action:draftChanged', { work: 'action:a1', draft: draft('続き') }],
  ]);
  assert.equal(app.discarded.length, 1);

  // A file no draft lists, such as one the chat composer removed, goes at once.
  const other = '22222222-2222-4222-8222-222222222222';
  app.discard(other);
  assert.deepEqual(app.discarded.at(-1), ['user-1', other]);
});

test('drafts belong to one owner', () => {
  const app = harness();
  app.open(1, 'action:a1');
  app.update(1, 'action:a1', draft('private', [FILE]));

  app.setOwner('user-2');
  assert.equal(app.open(1, 'action:a1'), null);
  // The previous owner's unsent file goes with its drafts.
  assert.deepEqual(app.discarded, [['user-1', FILE.attachmentId]]);
});

test('a closed composer or a destroyed window hears nothing more', () => {
  const app = harness();
  app.open(1, 'action:a1');
  app.open(2, 'action:a1');
  app.open(3, 'action:a1');
  app.close(2, 'action:a1');
  app.windowOf(3).emit('destroyed');

  app.update(1, 'action:a1', draft('x'));

  assert.deepEqual(app.windowOf(2).received, []);
  assert.deepEqual(app.windowOf(3).received, []);
});

test('draft requests are checked at the boundary', async () => {
  const app = harness();
  for (const payload of [
    { work: 'action: a1' },
    { work: 'conversation:a1' },
    { work: 'action:' },
    { work: 'action:a1', extra: 1 },
  ]) {
    assert.throws(() => app.invoke('action:openDraft', 1, payload), IpcValidationError);
  }
  for (const value of [
    { text: 'x', mentions: [] },
    {
      ...draft('x'),
      attachments: [{ kind: 'file', attachmentId: 'not-a-uuid', name: 'a', byteSize: 1 }],
    },
    { ...draft('x'), attachments: [{ kind: 'image', storagePath: 'a', bytes: 'AAAA' }] },
  ]) {
    assert.throws(() => app.update(1, 'action:a1', value), IpcValidationError);
  }
  // A small window no longer registered as a conversation is not a composer.
  const stale = app.windowOf(9);
  stale.registered = false;
  assert.throws(() => app.open(9, 'action:a1'), IpcSenderRejectedError);
});
