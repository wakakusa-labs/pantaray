const assert = require('assert');
const { test } = require('node:test');

const {
  createActionLatestPageReader,
} = require('../electron/dist/actions/actionLatestPageReader.js');

function conversationPage(actionId, latestRunId) {
  return {
    action: {
      action_id: actionId,
      suggestion_id: null,
      status: 'processing',
      latest_run_id: latestRunId,
      approved_suggestion: null,
      resumable: false,
    },
    runs: [],
    unadopted_messages: [],
    next_cursor: null,
  };
}

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((onResolve, onReject) => {
    resolve = onResolve;
    reject = onReject;
  });
  return { promise, reject, resolve };
}

const flush = () => new Promise(setImmediate);

test('read中に来たcallerは後続の1回のreadを共有し、最初のcallerは自分のreadで返る', async () => {
  const reads = [];
  const reader = createActionLatestPageReader(() => {
    const pending = deferred();
    reads.push(pending);
    return pending.promise;
  });

  const firstCaller = reader.read('action-a');
  const joinedCaller = reader.read('action-a');
  const secondJoinedCaller = reader.read('action-a');
  assert.notEqual(firstCaller, joinedCaller);
  assert.equal(joinedCaller, secondJoinedCaller);
  await flush();
  assert.equal(reads.length, 1);

  const older = conversationPage('action-a', 'run-old');
  reads[0].resolve(older);
  assert.deepStrictEqual(await firstCaller, older);
  await flush();
  assert.equal(reads.length, 2);
  const latest = conversationPage('action-a', 'run-new');
  reads[1].resolve(latest);

  assert.deepStrictEqual(await Promise.all([joinedCaller, secondJoinedCaller]), [latest, latest]);
  await flush();
  assert.equal(reads.length, 2);
});

test('callerは更新が続いても高々2回のreadで返る', async () => {
  const reads = [];
  const reader = createActionLatestPageReader(() => {
    const pending = deferred();
    reads.push(pending);
    return pending.promise;
  });
  const page = (index) => conversationPage('action-a', `run-${index}`);

  reader.read('action-a');
  // Every read is joined before it lands, as while a running Action keeps reporting.
  for (let inFlight = 0; inFlight < 40; inFlight += 1) {
    const caller = reader.read('action-a');
    reads[inFlight].resolve(page(inFlight));
    await flush();
    assert.equal(reads.length, inFlight + 2);
    reads[inFlight + 1].resolve(page(inFlight + 1));
    assert.deepStrictEqual(await caller, page(inFlight + 1));
    reader.read('action-a');
    await flush();
    inFlight += 1;
  }
});

test('read失敗は最初のcallerに返り、read中に来たcallerは次のreadを受け取る', async () => {
  const reads = [];
  const reader = createActionLatestPageReader(() => {
    const pending = deferred();
    reads.push(pending);
    return pending.promise;
  });

  const firstCaller = reader.read('action-a');
  const joinedCaller = reader.read('action-a');
  const failure = new Error('first read failed');
  reads[0].reject(failure);
  await assert.rejects(firstCaller, (error) => error === failure);
  await flush();
  assert.equal(reads.length, 2);

  const recovered = conversationPage('action-a', 'run-recovered');
  reads[1].resolve(recovered);
  assert.deepStrictEqual(await joinedCaller, recovered);
});

test('dirtyなしの失敗はsurfaceし、後続readを新しいflightで開始する', async () => {
  const failure = new Error('backend unavailable');
  let attempts = 0;
  const recovered = conversationPage('action-a', 'run-recovered');
  const reader = createActionLatestPageReader(async () => {
    attempts += 1;
    if (attempts === 1) throw failure;
    return recovered;
  });

  await assert.rejects(reader.read('action-a'), (error) => error === failure);
  assert.deepStrictEqual(await reader.read('action-a'), recovered);
  assert.equal(attempts, 2);
});

test('異なるActionのflightは互いを待たない', async () => {
  const reads = new Map();
  const reader = createActionLatestPageReader((actionId) => {
    const pending = deferred();
    reads.set(actionId, pending);
    return pending.promise;
  });

  const actionA = reader.read('action-a');
  const actionB = reader.read('action-b');
  await flush();
  const pageB = conversationPage('action-b', 'run-b');
  reads.get('action-b').resolve(pageB);
  assert.deepStrictEqual(await actionB, pageB);

  const pageA = conversationPage('action-a', 'run-a');
  reads.get('action-a').resolve(pageA);
  assert.deepStrictEqual(await actionA, pageA);
});

test('clearは旧flightを切り離し、旧完了処理は新flightを削除しない', async () => {
  const reads = [];
  const reader = createActionLatestPageReader(() => {
    const pending = deferred();
    reads.push(pending);
    return pending.promise;
  });

  const oldSubject = reader.read('action-a');
  const joinedOldSubject = reader.read('action-a');
  assert.equal(reads.length, 1);
  reader.clear();
  const newSubject = reader.read('action-a');
  const joinedNewSubject = reader.read('action-a');
  assert.equal(reads.length, 2);

  // The old subject's callers take its read; nothing is read again for it.
  const oldPage = conversationPage('action-a', 'run-old-subject');
  reads[0].resolve(oldPage);
  assert.deepStrictEqual(await Promise.all([oldSubject, joinedOldSubject]), [oldPage, oldPage]);
  await flush();
  assert.equal(reads.length, 2);

  const newPage = conversationPage('action-a', 'run-new-subject');
  reads[1].resolve(newPage);
  assert.deepStrictEqual(await newSubject, newPage);
  await flush();
  assert.equal(reads.length, 3);
  const latest = conversationPage('action-a', 'run-new-subject-latest');
  reads[2].resolve(latest);
  assert.deepStrictEqual(await joinedNewSubject, latest);
});
