const assert = require('node:assert/strict');
const { test } = require('node:test');

const { registerMeetingHandlers } = require('../electron/dist/ipc/handlers/meeting.js');

const CALL = 'call_01JDQ3ZK8W5N3S8C7V6B5X4Y3Z';
const OTHER = 'call_01JDQ3ZK8W5N3S8C7V6B5X4Y40';
const MAIN = { id: 1 };
const CHIP = { id: 2 };
const PANEL = { id: 3 };

/** The handlers over a call awaiting consent until `expiresInMs`, offered by the chip `CHIP`. */
function meetingIpc({ expiresInMs = 60_000 } = {}) {
  const handlers = new Map();
  const answered = [];
  const call = {
    kind: 'awaiting_consent',
    call_id: CALL,
    app: { name: 'zoom.us', bundle_id: 'us.zoom.xos' },
    site: null,
    expires_at: new Date(Date.now() + expiresInMs).toISOString(),
  };
  registerMeetingHandlers(
    {
      windows: { getMainWindow: () => ({ isDestroyed: () => false, webContents: MAIN }) },
      security: { auditMutation: () => {} },
      meeting: {
        getCall: () => call,
        isConsentChip: (sender, callId) => sender === CHIP && callId === CALL,
        record: async (callId) => {
          answered.push(['record', callId]);
          return 'requested';
        },
        decline: (callId) => answered.push(['decline', callId]),
      },
    },
    { handle: (channel, handler) => handlers.set(channel, handler) }
  );
  const send = (channel, sender, callId) => handlers.get(channel)({ sender }, callId);
  return { send, answered };
}

test('the consent chip answers for the call it shows while that call is asked about', async () => {
  const { send, answered } = meetingIpc();
  assert.equal(await send('meeting:record', CHIP, CALL), 'requested');
  send('meeting:decline', CHIP, CALL);
  assert.deepEqual(answered, [
    ['record', CALL],
    ['decline', CALL],
  ]);
});

test('no other panel, no other call and no expired consent can answer for a call', async () => {
  const { send, answered } = meetingIpc();
  for (const [sender, callId] of [
    [PANEL, CALL],
    [CHIP, OTHER],
  ]) {
    assert.throws(() => send('meeting:record', sender, callId), /not authorized/);
    assert.throws(() => send('meeting:decline', sender, callId), /not authorized/);
  }
  const expired = meetingIpc({ expiresInMs: -1 });
  assert.throws(() => expired.send('meeting:record', CHIP, CALL), /not authorized/);
  assert.deepEqual([...answered, ...expired.answered], []);
});

test('the main window still starts a call the user let pass', async () => {
  const { send, answered } = meetingIpc({ expiresInMs: -1 });
  assert.equal(await send('meeting:record', MAIN, CALL), 'requested');
  assert.deepEqual(answered, [['record', CALL]]);
});
