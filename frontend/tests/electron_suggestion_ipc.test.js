const assert = require('assert');
const { test } = require('node:test');

const { registerSuggestionHandlers } = require('../electron/dist/ipc/handlers/suggestion.js');

const SNAPSHOT = { suggestionId: 'sug-1', suggestionText: 'Draft the reply', lastSequence: 4 };

function register({ subject, resolveOverlayBootstrap }) {
  const handlers = new Map();
  const adopted = [];
  registerSuggestionHandlers(
    {
      actions: { getCurrentSubjectId: () => subject.id },
      overlay: {
        resolveOverlayBootstrap,
        adoptSuggestionSnapshot: (snapshot) => {
          adopted.push(snapshot);
          return { ...snapshot, adopted: true };
        },
      },
    },
    { handle: (channel, handler) => handlers.set(channel, handler) }
  );
  return {
    adopted,
    read: (payload) => handlers.get('suggestion:read')({ sender: { id: 1 } }, payload),
  };
}

test('suggestion:read stores the persisted snapshot for the same owner and returns the record', async () => {
  const requested = [];
  const { adopted, read } = register({
    subject: { id: 'user-1' },
    resolveOverlayBootstrap: async (suggestionId) => {
      requested.push(suggestionId);
      return { suggestionId, snapshot: SNAPSHOT, lastSequence: 4, liveResume: { kind: 'none' } };
    },
  });

  assert.deepEqual(await read({ suggestionId: ' sug-1 ' }), { ...SNAPSHOT, adopted: true });
  assert.deepEqual(requested, ['sug-1']);
  assert.deepEqual(adopted, [SNAPSHOT]);
  for (const invalid of [{ suggestionId: '' }, { suggestionId: 'sug-1', extra: true }, null]) {
    await assert.rejects(read(invalid), (error) => error?.name === 'IpcValidationError');
  }
  assert.equal(requested.length, 1);
});

test('suggestion:read stores nothing when the owner changed during the read', async () => {
  const subject = { id: 'user-1' };
  let resolveRead;
  const { adopted, read } = register({
    subject,
    resolveOverlayBootstrap: () =>
      new Promise((resolve) => {
        resolveRead = resolve;
      }),
  });
  const result = read({ suggestionId: 'sug-1' });
  subject.id = 'user-2';
  resolveRead({ suggestionId: 'sug-1', snapshot: SNAPSHOT, lastSequence: 4 });

  await assert.rejects(result, /Local owner changed/);
  assert.deepEqual(adopted, []);
});
