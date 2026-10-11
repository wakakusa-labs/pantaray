const assert = require('assert');
const { test } = require('node:test');

const {
  ActionLiveIdentityError,
  createActionLiveCoordinator,
  isActionTransientToolStepSuperseded,
  reduceActionLiveSnapshot,
  routeActionLiveEvent,
} = require('../electron/dist/actions/actionLiveCore.js');
const {
  createActionLatestPageReader,
} = require('../electron/dist/actions/actionLatestPageReader.js');

function actionMeta(actionId = 'action-a', processId = 'process-a') {
  return {
    kind: 'action',
    action_id: actionId,
    process_id: processId,
    suggestion_id: 'suggestion-1',
    command_id: 'command-1',
  };
}

function approvalBlocker(actionId, id, processId = 'process-a') {
  return {
    process_id: processId,
    action_id: actionId,
    approval_session_id: `approval-${id}`,
    tool_request_id: `tool-request-${id}`,
    tool_id: 'tool',
    intent_class: 'test',
    command_summary: {},
  };
}

function processEvent(event, actionId = 'action-a', processId = 'process-a') {
  const meta = actionMeta(actionId, processId);
  if (event === 'process_started') {
    return {
      event,
      meta,
      data: {
        ...meta,
        accepted_at: '2026-08-30T01:00:00.000000Z',
        started_at: '2026-08-30T01:00:01.000000Z',
      },
    };
  }
  if (event === 'process_paused') {
    return {
      event,
      meta,
      data: {
        ...meta,
        status: 'processing',
        reason: 'approval_pending',
        completed_at: '2026-08-30T01:01:00.000000Z',
        approval_blockers: [
          approvalBlocker(actionId, 1, processId),
          approvalBlocker(actionId, 2, processId),
        ],
      },
    };
  }
  return {
    event,
    meta,
    data: {
      ...meta,
      status: 'success',
    },
  };
}

function actionStep(
  status,
  processId = 'process-a',
  stepId = 'step-1',
  runId = 'run-1',
  stepNumber = 2
) {
  const meta = { ...actionMeta('action-a', processId), logical_run_id: runId };
  return {
    event: 'action_step',
    meta,
    data: {
      action_id: meta.action_id,
      process_id: meta.process_id,
      step_kind: 'tool',
      step_id: stepId,
      step_number: stepNumber,
      tool_id: 'shell',
      label: 'shell',
      subject: 'git status --short --branch',
      status,
      started_at: '2026-08-30T01:00:02.000000Z',
      completed_at: status === 'processing' ? null : '2026-08-30T01:00:03.000000Z',
    },
  };
}

function actionError(stage, actionId) {
  const meta = { ...actionMeta(), stage, error_code: 'ACTION_FAILED' };
  if (actionId === null) {
    delete meta.action_id;
    delete meta.process_id;
  } else {
    meta.action_id = actionId;
  }
  return {
    event: 'error',
    meta,
    data: {
      error_type: 'action_failed',
      error_code: 'ACTION_FAILED',
      error_message: 'private failure',
      severity: 'error',
    },
  };
}

test('assistant event refreshes the durable message once per page without a tool row', async () => {
  const event = actionStep('success');
  event.data = {
    action_id: 'action-a', process_id: 'process-a', step_kind: 'assistant',
    step_id: 'message-1', step_number: 2, status: 'success',
  };
  const route = routeActionLiveEvent(event);
  assert.equal(route.refresh, true);
  assert.equal(route.legacyDisposition, 'suppress');
  assert.equal(route.transientEffect, null);
  const entry = {
    step_kind: 'assistant', step_id: 'message-1', step_number: 2,
    content: '原因を特定しました。依存する設定を確認します。',
  };
  const coordinator = createActionLiveCoordinator({
    readLatestPage: async () => conversationPage('action-a', { entry }),
    publish: () => {},
    surfaceRefreshError: (_id, error) => { throw error; },
  });
  await coordinator.handleEvent(event).refreshDone;
  await coordinator.handleEvent(event).refreshDone;
  const snapshot = coordinator.getSnapshot('action-a');
  assert.deepStrictEqual(snapshot.page.runs[0].entries, [entry]);
  assert.deepStrictEqual(snapshot.transientToolSteps, []);
  assert.equal(snapshot.page.runs[0].status, 'running');
  const mismatched = { ...event, data: { ...event.data, process_id: 'other' } };
  assert.throws(() => routeActionLiveEvent(mismatched), ActionLiveIdentityError);
});

function conversationPage(
  actionId,
  canonicalTools = null,
  runId = 'run-1',
  actionStatus = 'processing'
) {
  // 1 THINK が複数 tool を呼ぶバッチでは、page が一部の step だけを canonical 化しうる。
  const canonical = canonicalTools === null ? [] : [].concat(canonicalTools);
  return {
    action: {
      action_id: actionId,
      suggestion_id: null,
      status: actionStatus,
      latest_run_id: runId,
      approved_suggestion: null,
      resumable: false,
    },
    runs:
      canonical.length === 0
        ? []
        : [
            {
              run_id: runId,
              status: 'running',
              started_at: '2026-08-30T01:00:01.000000Z',
              completed_at: null,
              completion_event_id: null,
              entries: canonical.map((step) => step.entry),
              final_output: null,
              error: null,
            },
          ],
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

test('Action event router はDB refresh/control pass/legacy body suppressを一箇所で分類する', () => {
  const started = routeActionLiveEvent(processEvent('process_started'));
  const paused = routeActionLiveEvent(processEvent('process_paused'));
  const completed = routeActionLiveEvent(processEvent('process_completed'));
  const bodyOnlyEvents = [
    { event: 'completion_chunk', meta: actionMeta(), data: { content: 'private chunk' } },
  ].map(routeActionLiveEvent);

  assert.deepStrictEqual(
    [started, paused, completed].map((route) => [
      route.belongsToActionConversation,
      route.legacyDisposition,
      route.refresh,
      route.transientEffect?.kind ?? null,
    ]),
    [
      [true, 'suppress', true, null],
      [true, 'suppress', true, null],
      [true, 'suppress', true, null],
    ]
  );
  const subagentPause = processEvent('process_paused');
  subagentPause.data.approval_blockers[1].process_id = 'subagent-process';
  assert.deepStrictEqual(
    routeActionLiveEvent(subagentPause).approvalEffect.blockers.map(
      (blocker) => blocker.processId
    ),
    ['process-a', 'subagent-process']
  );
  for (const invalidProcessId of ['', ' subagent-process ']) {
    const invalidPause = processEvent('process_paused');
    invalidPause.data.approval_blockers[0].process_id = invalidProcessId;
    assert.throws(() => routeActionLiveEvent(invalidPause), ActionLiveIdentityError);
  }
  assert.ok(
    bodyOnlyEvents.every(
      (route) =>
        route.belongsToActionConversation &&
        route.legacyDisposition === 'suppress' &&
        !route.refresh &&
        route.transientEffect === null
    )
  );
  assert.doesNotMatch(JSON.stringify([completed, ...bodyOnlyEvents]), /private/);
  const errorRoutes = [
    'preflight_rejected',
    'start_failed',
    'running_failed',
    'persist_final_state_failed',
  ].map((stage) => routeActionLiveEvent(actionError(stage, 'action-a')));
  assert.deepStrictEqual(
    errorRoutes.map((route) => [route.legacyDisposition, route.refresh, route.actionId]),
    [
      ['pass', true, 'action-a'],
      ['pass', true, 'action-a'],
      ['suppress', true, 'action-a'],
      ['pass', true, 'action-a'],
    ]
  );
  const noActionError = routeActionLiveEvent(actionError('preflight_rejected', null));
  assert.equal(noActionError.belongsToActionConversation, false);
  assert.equal(noActionError.legacyDisposition, 'pass');
  assert.equal(noActionError.refresh, false);
  assert.doesNotMatch(JSON.stringify(errorRoutes), /private/);
  const suggestion = processEvent('process_started');
  suggestion.data.kind = 'suggestion';
  suggestion.meta = {
    kind: 'suggestion',
    process_id: 'suggestion-process',
    suggestion_id: 'suggestion-1',
  };
  assert.equal(routeActionLiveEvent(suggestion).belongsToActionConversation, false);
  const mismatched = actionStep('processing');
  mismatched.data.process_id = 'other-process';
  assert.throws(() => routeActionLiveEvent(mismatched), ActionLiveIdentityError);
  for (const logicalRunId of [undefined, '', ' run-1 ']) {
    const invalid = actionStep('processing');
    invalid.meta.logical_run_id = logicalRunId;
    assert.throws(() => routeActionLiveEvent(invalid), ActionLiveIdentityError);
  }
});

test('a finished Tool re-reads the durable page so its output opens mid-run', () => {
  // The transient row can never open its output; only the durable row can.
  assert.deepStrictEqual(
    ['processing', 'success', 'error'].map((status) => {
      const route = routeActionLiveEvent(actionStep(status));
      return [status, route.refresh, route.transientEffect?.kind ?? null];
    }),
    [
      ['processing', false, 'upsert'],
      ['success', true, 'upsert'],
      ['error', true, 'upsert'],
    ]
  );
});

test('transient Tool reducer は同logical run/stepの継続processを置換する', () => {
  const processing = routeActionLiveEvent(actionStep('processing')).transientEffect;
  const terminal = routeActionLiveEvent(actionStep('success', 'process-b')).transientEffect;
  let snapshot = reduceActionLiveSnapshot(null, 'action-a', processing);
  assert.equal(snapshot.transientToolSteps[0].entry.subject, 'git status --short --branch');
  snapshot = reduceActionLiveSnapshot(snapshot, 'action-a', terminal);
  assert.equal(snapshot.transientToolSteps[0].entry.subject, 'git status --short --branch');

  assert.deepStrictEqual(
    snapshot.transientToolSteps.map((step) => [step.runId, step.processId, step.entry.status]),
    [['run-1', 'process-b', 'success']]
  );
});

test('approval blocker はpauseだけが生成しcanonical statusとprocess decisionが縮退させる', async () => {
  let page = conversationPage('action-a', null, 'process-a');
  const updates = [];
  const coordinator = createActionLiveCoordinator({
    readLatestPage: async () => page,
    publish: (update) => updates.push(update),
    surfaceRefreshError: (_actionId, error) => assert.fail(error),
  });

  const pauseRefresh = coordinator.handleEvent(processEvent('process_paused')).refreshDone;
  await pauseRefresh;
  assert.equal(coordinator.getSnapshot('action-a').approvalBlockers.length, 2);

  coordinator.handleEvent(processEvent('process_started', 'action-a', 'other-process'));
  assert.equal(updates.at(-1).snapshot.approvalBlockers.length, 2);
  // canonical な Action 完了は root/子を問わず全 blocker を落とす。
  coordinator.handleEvent(processEvent('process_completed', 'action-a', 'other-process'));
  assert.deepStrictEqual(updates.at(-1).snapshot.approvalBlockers, []);
  await coordinator.handleEvent(processEvent('process_paused')).refreshDone;

  const canonicalStep = routeActionLiveEvent(actionStep('processing')).transientEffect.step;
  page = conversationPage('action-a', canonicalStep, 'logical-run-a');
  await coordinator.refresh('action-a');
  assert.equal(updates.at(-1).snapshot.approvalBlockers.length, 2);

  const settledIdentity = {
    actionId: 'action-a',
    processId: 'process-a',
    approvalSessionId: 'approval-1',
    toolRequestId: 'tool-request-1',
  };
  // 決定は process 全体を running へ戻すため、同一 process の残り blocker も同時に落ちる。
  coordinator.handleApprovalDecisionSettled(settledIdentity);
  assert.deepStrictEqual(updates.at(-1).snapshot.approvalBlockers, []);
  const freshPause = processEvent('process_paused');
  freshPause.data.approval_blockers = freshPause.data.approval_blockers.slice(1);
  await coordinator.handleEvent(freshPause).refreshDone;
  assert.equal(updates.at(-1).snapshot.approvalBlockers.length, 1);
  coordinator.handleApprovalDecisionSettled(settledIdentity);
  assert.equal(updates.at(-1).snapshot.approvalBlockers.length, 1);
});

test('並列approval blockerはnonterminal refreshを越えて残り決定ごとに1件だけ縮退する', async () => {
  const updates = [];
  const canonicalStep = routeActionLiveEvent(actionStep('processing')).transientEffect.step;
  // root process は running のまま subagent だけが承認待ちの状態を再現する。
  const page = conversationPage('action-a', canonicalStep, 'logical-run-a');
  const coordinator = createActionLiveCoordinator({
    readLatestPage: async () => page,
    publish: (update) => updates.push(update),
    surfaceRefreshError: (_actionId, error) => assert.fail(error),
  });

  const parallelPause = processEvent('process_paused');
  parallelPause.data.approval_blockers = [
    approvalBlocker('action-a', 1, 'process-a'),
    approvalBlocker('action-a', 2, 'subagent-x'),
    approvalBlocker('action-a', 3, 'subagent-x'),
    approvalBlocker('action-a', 4, 'subagent-y'),
  ];
  await coordinator.handleEvent(parallelPause).refreshDone;
  assert.equal(page.runs[0].status, 'running');
  assert.deepStrictEqual(
    updates.at(-1).snapshot.approvalBlockers.map((blocker) => blocker.processId),
    ['process-a', 'subagent-x', 'subagent-x', 'subagent-y']
  );

  // 決定された process の blocker だけが全て落ち、他 process の並列 blocker は残る。
  coordinator.handleApprovalDecisionSettled({
    actionId: 'action-a',
    processId: 'subagent-x',
    approvalSessionId: 'approval-2',
    toolRequestId: 'tool-request-2',
  });
  assert.deepStrictEqual(
    updates.at(-1).snapshot.approvalBlockers.map((blocker) => blocker.processId),
    ['process-a', 'subagent-y']
  );
  await coordinator.refresh('action-a');
  assert.deepStrictEqual(
    updates.at(-1).snapshot.approvalBlockers.map((blocker) => blocker.processId),
    ['process-a', 'subagent-y']
  );

  const emptySnapshot = processEvent('process_paused');
  emptySnapshot.data.approval_blockers = [];
  const publishedBeforeEmpty = updates.length;
  await coordinator.handleEvent(emptySnapshot).refreshDone;
  assert.ok(updates.length > publishedBeforeEmpty);
  assert.deepStrictEqual(updates.at(-1).snapshot.approvalBlockers, []);
});

test('root process_completed はrefresh失敗時でも子blockerごと同期的に消す', async () => {
  const updates = [];
  const errors = [];
  const coordinator = createActionLiveCoordinator({
    readLatestPage: async () => {
      throw new Error('conversation read failed');
    },
    publish: (update) => updates.push(update),
    surfaceRefreshError: (_actionId, error) => errors.push(error),
  });

  const pause = processEvent('process_paused');
  pause.data.approval_blockers = [
    approvalBlocker('action-a', 1, 'process-a'),
    approvalBlocker('action-a', 2, 'subagent-x'),
  ];
  await coordinator.handleEvent(pause).refreshDone;
  assert.equal(updates.at(-1).snapshot.approvalBlockers.length, 2);
  assert.equal(errors.length, 1);

  await coordinator.handleEvent(processEvent('process_completed')).refreshDone;
  assert.equal(errors.length, 2);
  assert.deepStrictEqual(updates.at(-1).snapshot.approvalBlockers, []);
  assert.deepStrictEqual(coordinator.getSnapshot('action-a').approvalBlockers, []);
});

test('決定settleはblockerの子processではなくroot transport process idを返す', async () => {
  const page = conversationPage('action-a', null, 'logical-run-a');
  const coordinator = createActionLiveCoordinator({
    readLatestPage: async () => page,
    publish: () => {},
    surfaceRefreshError: (_actionId, error) => assert.fail(error),
  });
  const identity = {
    actionId: 'action-a',
    processId: 'subagent-x',
    approvalSessionId: 'approval-2',
    toolRequestId: 'tool-request-2',
  };

  assert.equal(coordinator.handleApprovalDecisionSettled(identity), null);

  const pause = processEvent('process_paused');
  pause.data.approval_blockers = [approvalBlocker('action-a', 2, 'subagent-x')];
  await coordinator.handleEvent(pause).refreshDone;
  // authoritative な root は snapshot meta の process_id で、blocker の子 process ではない。
  assert.equal(coordinator.handleApprovalDecisionSettled(identity), 'process-a');
  // blocker が既に落ちた後でも transport は同じ root を指し続ける。
  assert.equal(coordinator.handleApprovalDecisionSettled(identity), 'process-a');

  coordinator.clearAll();
  assert.equal(coordinator.handleApprovalDecisionSettled(identity), null);
});

test('refresh coordinator はActionごとのflightをshared readerへjoinさせA-B-Aを独立処理する', async () => {
  const reads = [];
  const published = [];
  const latestPageReader = createActionLatestPageReader((actionId) => {
    const pending = deferred();
    reads.push({ actionId, pending });
    return pending.promise;
  });
  const coordinator = createActionLiveCoordinator({
    readLatestPage: latestPageReader.read,
    publish: (update) => published.push(update),
    surfaceRefreshError: (_actionId, error) => assert.fail(error),
  });

  const firstA = coordinator.refresh('action-a');
  const onlyB = coordinator.refresh('action-b');
  const laterA = coordinator.refresh('action-a');
  await new Promise(setImmediate);
  assert.deepStrictEqual(
    reads.map((read) => read.actionId),
    ['action-a', 'action-b']
  );

  // The first read of A is published as it lands; the later request reads A again.
  reads[0].pending.resolve(conversationPage('action-a'));
  await firstA;
  assert.deepStrictEqual(
    reads.map((read) => read.actionId),
    ['action-a', 'action-b', 'action-a']
  );
  reads[1].pending.resolve(conversationPage('action-b'));
  reads[2].pending.resolve(conversationPage('action-a'));
  await Promise.all([onlyB, laterA]);

  assert.deepStrictEqual(
    published.map((update) => update.snapshot.actionId),
    ['action-a', 'action-b', 'action-a']
  );
});

test('refresh failure はdirtyなしで終了し、後続triggerだけが再開してpage identityもbindする', async () => {
  const networkError = new Error('network unavailable');
  const errors = [];
  const published = [];
  let attempt = 0;
  const coordinator = createActionLiveCoordinator({
    readLatestPage: async () => {
      attempt += 1;
      if (attempt === 1) throw networkError;
      if (attempt === 2) return conversationPage('wrong-action');
      return conversationPage('action-a');
    },
    publish: (update) => published.push(update),
    surfaceRefreshError: (actionId, error) => errors.push({ actionId, error }),
  });

  await coordinator.refresh('action-a');
  assert.equal(attempt, 1);
  assert.equal(errors[0].error, networkError);

  await coordinator.refresh('action-a');
  assert.equal(attempt, 2);
  assert.ok(errors[1].error instanceof ActionLiveIdentityError);
  await coordinator.refresh('action-a');
  assert.equal(attempt, 3);
  assert.equal(published.at(-1).snapshot.page.action.action_id, 'action-a');
});

test('shared reader はreadごとにpublishし、read中に来たcallerは次の1回で揃う', async () => {
  const firstRead = deferred();
  const secondRead = deferred();
  const errors = [];
  const published = [];
  let reads = 0;
  const latestPageReader = createActionLatestPageReader(() => {
    reads += 1;
    return reads === 1 ? firstRead.promise : secondRead.promise;
  });
  const coordinator = createActionLiveCoordinator({
    readLatestPage: latestPageReader.read,
    publish: (update) => published.push(update),
    surfaceRefreshError: (_actionId, error) => errors.push(error),
  });

  const firstCaller = coordinator.refresh('action-a');
  const rendererCaller = latestPageReader.read('action-a');
  const joinedCaller = coordinator.handleEvent(processEvent('process_completed')).refreshDone;
  firstRead.resolve(conversationPage('action-a'));
  assert.equal(await firstCaller, 'refreshed');
  assert.equal(published.at(-1).snapshot.page.action.action_id, 'action-a');

  assert.equal(reads, 2);
  let joinedSettled = false;
  void Promise.all([rendererCaller, joinedCaller]).then(() => {
    joinedSettled = true;
  });
  await new Promise(setImmediate);
  assert.equal(joinedSettled, false);
  const latest = conversationPage('action-a', null, 'process-a', 'success');
  secondRead.resolve(latest);
  assert.deepStrictEqual(await rendererCaller, latest);
  assert.equal(await joinedCaller, 'refreshed');
  assert.equal(reads, 2);
  assert.equal(errors.length, 0);
  assert.equal(published.at(-1).snapshot.page, latest);
});

test('read中に更新が届き続けても、各readの結果が届いた順にpublishされる', async () => {
  // A running Action reports finished Tools faster than one read takes: every read is
  // joined before it lands. Each landed read is still published, so a window opened on the
  // running Action shows its conversation without waiting for the updates to stop.
  const reads = [];
  const published = [];
  const latestPageReader = createActionLatestPageReader(() => {
    const pending = deferred();
    reads.push(pending);
    return pending.promise;
  });
  const coordinator = createActionLiveCoordinator({
    readLatestPage: latestPageReader.read,
    publish: (update) => update.kind === 'action_updated' && update.snapshot.page && published.push(update),
    surfaceRefreshError: (_actionId, error) => assert.fail(error),
  });

  const opened = coordinator.refresh('action-a');
  let openedOutcome = null;
  void opened.then((outcome) => {
    openedOutcome = outcome;
  });
  for (let index = 0; index < 40; index += 1) {
    coordinator.refresh('action-a');
    await new Promise(setImmediate);
    reads[index].resolve(conversationPage('action-a', null, `run-${index}`, 'processing'));
    await new Promise(setImmediate);
    assert.equal(published.length, index + 1);
    assert.equal(published.at(-1).snapshot.page.action.latest_run_id, `run-${index}`);
  }
  assert.equal(openedOutcome, 'refreshed');
});

test('read中に届いた終了通知は、その前に始まったreadのpageで消えず、後続read失敗後も残る', async () => {
  const reads = [];
  const updates = [];
  const errors = [];
  const latestPageReader = createActionLatestPageReader(() => {
    const pending = deferred();
    reads.push(pending);
    return pending.promise;
  });
  const coordinator = createActionLiveCoordinator({
    readLatestPage: latestPageReader.read,
    publish: (update) => updates.push(update),
    surfaceRefreshError: (_actionId, error) => errors.push(error),
  });

  const stale = coordinator.refresh('action-a');
  await new Promise(setImmediate);
  const failed = processEvent('process_completed');
  failed.data.status = 'error';
  const completion = coordinator.handleEvent(failed).refreshDone;
  assert.deepStrictEqual(updates.at(-1).snapshot.lifecycle, {
    processId: 'process-a',
    status: 'error',
  });

  // The page read before the run ended still says processing; the end it missed stays.
  reads[0].resolve(conversationPage('action-a'));
  assert.equal(await stale, 'refreshed');
  assert.equal(updates.at(-1).snapshot.page.action.status, 'processing');
  assert.deepStrictEqual(updates.at(-1).snapshot.lifecycle, {
    processId: 'process-a',
    status: 'error',
  });

  await new Promise(setImmediate);
  reads[1].reject(new Error('backend unavailable'));
  assert.equal(await completion, 'failed');
  assert.equal(errors.length, 1);
  assert.deepStrictEqual(coordinator.getSnapshot('action-a').lifecycle, {
    processId: 'process-a',
    status: 'error',
  });

  // A read that began after the notification is authoritative again.
  const settled = coordinator.refresh('action-a');
  await new Promise(setImmediate);
  reads[2].resolve(conversationPage('action-a', null, 'run-1', 'error'));
  assert.equal(await settled, 'refreshed');
  assert.equal(updates.at(-1).snapshot.lifecycle, null);
  assert.equal(updates.at(-1).snapshot.page.action.status, 'error');
});

test('dirty 中の旧terminal pageは適用せず新runのtransientを保持する', async () => {
  const reads = [];
  const updates = [];
  const latestPageReader = createActionLatestPageReader(() => {
    const pending = deferred();
    reads.push(pending);
    return pending.promise;
  });
  const coordinator = createActionLiveCoordinator({
    readLatestPage: latestPageReader.read,
    publish: (update) => updates.push(update),
    surfaceRefreshError: (_actionId, error) => assert.fail(error),
  });

  const stale = coordinator.handleStreamBoundary({ kind: 'reconnect', actionId: 'action-a' });
  await new Promise(setImmediate);
  coordinator.handleEvent(actionStep('processing', 'new-process', 'new-step'));
  const joined = coordinator.refresh('action-a');
  reads[0].resolve(conversationPage('action-a', null, 'old-run', 'success'));
  await new Promise(setImmediate);

  assert.equal(reads.length, 2);
  assert.equal(
    updates.some((update) => update.snapshot?.page?.action.status === 'success'),
    false
  );
  assert.equal(updates.at(-1).snapshot.transientToolSteps[0].entry.step_id, 'new-step');

  reads[1].resolve(conversationPage('action-a', null, 'new-run'));
  await Promise.all([stale, joined]);
  assert.equal(updates.at(-1).snapshot.page.action.latest_run_id, 'new-run');
  assert.equal(updates.at(-1).snapshot.transientToolSteps[0].entry.step_id, 'new-step');
});

test('canonical Tool status precedence はlogical run内だけでtransientを照合する', async () => {
  const updates = [];
  const canonicalProcessing = routeActionLiveEvent(
    actionStep('processing', 'root-process', 'shared-step', 'logical-run-root')
  ).transientEffect.step;
  let page = conversationPage('action-a', canonicalProcessing, 'logical-run-root');
  const coordinator = createActionLiveCoordinator({
    readLatestPage: async () => page,
    publish: (update) => updates.push(update),
    surfaceRefreshError: (_actionId, error) => assert.fail(error),
  });

  await coordinator.refresh('action-a');
  const otherRun = routeActionLiveEvent(
    actionStep('processing', 'physical-process', 'shared-step', 'other-run')
  ).transientEffect.step;
  assert.equal(isActionTransientToolStepSuperseded(page, otherRun), false);
  const canonicalPublished = updates.length;
  coordinator.handleEvent(
    actionStep('processing', 'physical-process', 'shared-step', 'logical-run-root')
  );
  assert.equal(updates.length, canonicalPublished);

  // A finished Tool is durable before its event, so the read it triggers sees it.
  const terminalStep = routeActionLiveEvent(
    actionStep('success', 'physical-process', 'shared-step', 'logical-run-root')
  ).transientEffect.step;
  page = conversationPage('action-a', terminalStep, 'logical-run-root');
  const finished = coordinator.handleEvent(
    actionStep('success', 'physical-process', 'shared-step', 'logical-run-root')
  );
  // The canonical processing row does not hide the terminal transient before the read lands.
  const terminalTransient = updates.at(-1).snapshot.transientToolSteps[0];
  assert.deepStrictEqual(
    [terminalTransient.processId, terminalTransient.entry.status],
    ['physical-process', 'success']
  );

  await finished.refreshDone;
  assert.deepStrictEqual(updates.at(-1).snapshot.transientToolSteps, []);
  const terminalPublished = updates.length;
  coordinator.handleEvent(
    actionStep('success', 'physical-process', 'shared-step', 'logical-run-root')
  );
  assert.equal(updates.length, terminalPublished);
});

test('refreshはread中のtransientを保持し次の明示refreshでpage外terminalを除く', async () => {
  // A finished Tool that arrives during a read asks the reader again. The shared reader
  // queues that as the next read; this bare reader starts it at once and leaves it pending,
  // so each flight here is resolved through its own read.
  const reads = [];
  const updates = [];
  const coordinator = createActionLiveCoordinator({
    readLatestPage: () => {
      const pending = deferred();
      reads.push(pending);
      return pending.promise;
    },
    publish: (update) => updates.push(update),
    surfaceRefreshError: (_actionId, error) => assert.fail(error),
  });

  coordinator.handleEvent(actionStep('processing'));
  const beforeRead = coordinator.handleStreamBoundary({ kind: 'reconnect', actionId: 'action-a' });
  await new Promise(setImmediate);
  reads[0].resolve(conversationPage('action-a'));
  await beforeRead;
  assert.deepStrictEqual(
    updates.at(-1).snapshot.transientToolSteps.map((step) => step.entry.status),
    ['processing']
  );

  const duringRead = coordinator.refresh('action-a');
  await new Promise(setImmediate);
  coordinator.handleEvent(actionStep('success'));
  assert.equal(reads.length, 3);
  const canonicalProcessing = routeActionLiveEvent(actionStep('processing')).transientEffect.step;
  reads[1].resolve(conversationPage('action-a', canonicalProcessing));
  await duringRead;
  assert.equal(updates.at(-1).snapshot.transientToolSteps[0].entry.status, 'success');

  const terminalCleanup = coordinator.refresh('action-a');
  await new Promise(setImmediate);
  coordinator.handleEvent(actionStep('success'));
  assert.equal(reads.length, 5);
  reads[3].resolve(conversationPage('action-a'));
  await terminalCleanup;
  assert.deepStrictEqual(updates.at(-1).snapshot.transientToolSteps, []);

  coordinator.handleEvent(actionStep('processing', 'process-b', 'step-2'));
  const terminalBoundary = coordinator.handleStreamBoundary({
    kind: 'gap',
    actionId: 'action-a',
  });
  await new Promise(setImmediate);
  coordinator.handleEvent(actionStep('success', 'process-c', 'step-3'));
  assert.equal(reads.length, 7);
  // A terminal page read while a newer read was asked for is left to that newer read.
  reads[5].resolve(conversationPage('action-a', null, 'run-1', 'success'));
  reads[6].resolve(conversationPage('action-a', null, 'run-1', 'success'));
  await terminalBoundary;
  assert.deepStrictEqual(updates.at(-1).snapshot.transientToolSteps, []);
});

test('process terminalはrefresh成功までtransientを保持しresetは旧subject結果を破棄する', async () => {
  const terminalRead = deferred();
  const staleRead = deferred();
  const updates = [];
  const errors = [];
  let reads = 0;
  const coordinator = createActionLiveCoordinator({
    readLatestPage: (actionId) => {
      reads += 1;
      // The finished Tool starts the read and the completion joins that same read.
      if (reads <= 2) return terminalRead.promise;
      if (reads === 3) return Promise.resolve(conversationPage(actionId, null, 'run-1', 'success'));
      return staleRead.promise;
    },
    publish: (update) => updates.push(update),
    surfaceRefreshError: (_actionId, error) => errors.push(error),
  });

  coordinator.handleEvent(actionStep('success'));
  const terminal = coordinator.handleEvent(processEvent('process_completed'));
  await new Promise(setImmediate);
  terminalRead.reject(new Error('terminal refresh failed'));
  await terminal.refreshDone;
  assert.equal(errors.length, 1);
  assert.equal(updates.at(-1).snapshot.transientToolSteps[0].entry.status, 'success');

  await coordinator.refresh('action-a');
  assert.deepStrictEqual(updates.at(-1).snapshot.transientToolSteps, []);
  assert.equal(coordinator.getSnapshot('action-a'), null);

  const stale = coordinator.refresh('action-a');
  await new Promise(setImmediate);
  coordinator.clearAll();
  staleRead.resolve(conversationPage('action-a'));
  await stale;
  assert.equal(updates.at(-1).kind, 'reset');
  assert.equal(coordinator.getSnapshot('action-a'), null);
});

test('1 THINK の並列 tool step は step_id 単位でupsertされ部分canonical化でも兄弟が残る', async () => {
  const batch = [
    ['S-5-TOOL', 5],
    ['S-6-TOOL', 6],
    ['S-7-TOOL', 7],
  ];
  const read = deferred();
  const updates = [];
  const coordinator = createActionLiveCoordinator({
    readLatestPage: () => read.promise,
    publish: (update) => updates.push(update),
    surfaceRefreshError: (_actionId, error) => assert.fail(error),
  });

  for (const [stepId, stepNumber] of batch) {
    coordinator.handleEvent(actionStep('processing', 'process-a', stepId, 'run-1', stepNumber));
  }
  assert.deepStrictEqual(
    updates
      .at(-1)
      .snapshot.transientToolSteps.map((step) => [
        step.entry.step_id,
        step.entry.step_number,
        step.entry.status,
      ]),
    [
      ['S-5-TOOL', 5, 'processing'],
      ['S-6-TOOL', 6, 'processing'],
      ['S-7-TOOL', 7, 'processing'],
    ]
  );

  // terminal は宣言順に届かない。upsert キーは step_id なので行は入れ替わらない。
  const terminals = [
    routeActionLiveEvent(actionStep('success', 'process-a', 'S-7-TOOL', 'run-1', 7)).transientEffect
      .step,
    routeActionLiveEvent(actionStep('error', 'process-a', 'S-5-TOOL', 'run-1', 5)).transientEffect
      .step,
  ];
  coordinator.handleEvent(actionStep('success', 'process-a', 'S-7-TOOL', 'run-1', 7));
  coordinator.handleEvent(actionStep('error', 'process-a', 'S-5-TOOL', 'run-1', 5));
  assert.deepStrictEqual(
    updates
      .at(-1)
      .snapshot.transientToolSteps.map((step) => [step.entry.step_id, step.entry.status]),
    [
      ['S-5-TOOL', 'error'],
      ['S-6-TOOL', 'processing'],
      ['S-7-TOOL', 'success'],
    ]
  );

  // terminal な action_step は永続化済みの step を指すので page が引き取り、
  // まだ実行中の兄弟だけが transient として残る。
  const refreshed = coordinator.refresh('action-a');
  await new Promise(setImmediate);
  read.resolve(conversationPage('action-a', terminals));
  await refreshed;
  assert.deepStrictEqual(
    updates
      .at(-1)
      .snapshot.transientToolSteps.map((step) => [step.entry.step_id, step.entry.status]),
    [['S-6-TOOL', 'processing']]
  );
  assert.deepStrictEqual(
    updates.at(-1).snapshot.page.runs[0].entries.map((entry) => entry.step_id),
    ['S-7-TOOL', 'S-5-TOOL']
  );
});

test('canonical page version advances for identical reads but not transient publications', async () => {
  const page = conversationPage('action-a', null, 'run-1');
  const updates = [];
  const coordinator = createActionLiveCoordinator({
    readLatestPage: async () => page,
    publish: (update) => updates.push(update),
    surfaceRefreshError: (_actionId, error) => assert.fail(error),
  });
  await coordinator.refresh('action-a');
  const first = updates.at(-1).snapshot.pageVersion;
  coordinator.handleEvent(actionStep('processing'));
  assert.equal(updates.at(-1).snapshot.pageVersion, first);
  assert.ok(updates.at(-1).snapshot.transientToolSteps.length > 0);
  await coordinator.refresh('action-a');
  assert.ok(updates.at(-1).snapshot.pageVersion > first);
  assert.deepEqual(updates.at(-1).snapshot.page, page);
});

for (const status of ['error', 'timeout', 'canceled', 'success']) {
  test(`standalone ${status} reaches the live snapshot before a failed conversation read`, async () => {
    const pending = deferred();
    const updates = [];
    const errors = [];
    const page = conversationPage('action-a', null, 'process-a');
    let read = async () => page;
    const coordinator = createActionLiveCoordinator({
      readLatestPage: () => read(),
      publish: (update) => updates.push(update),
      surfaceRefreshError: (_actionId, error) => errors.push(error),
    });
    await coordinator.refresh('action-a');
    const version = updates.at(-1).snapshot.pageVersion;
    read = () => pending.promise;
    const event = processEvent('process_completed');
    event.meta.suggestion_id = null;
    event.data.suggestion_id = null;
    event.data.status = status;
    const completion = coordinator.handleEvent(event);
    const expected = { processId: 'process-a', status };
    assert.deepStrictEqual(updates.at(-1).snapshot.lifecycle, expected);
    assert.deepStrictEqual(updates.at(-1).snapshot.page, page);
    assert.equal(updates.at(-1).snapshot.pageVersion, version);
    pending.reject(new Error('network unavailable'));
    await completion.refreshDone;
    assert.equal(errors.length, 1);
    assert.deepStrictEqual(coordinator.getSnapshot('action-a').lifecycle, expected);
    const lateTool = coordinator.handleEvent(actionStep('success'));
    assert.deepStrictEqual(updates.at(-1).snapshot.lifecycle, expected);
    // The late Tool's own read fails too; the lifecycle stays authoritative.
    await lateTool.refreshDone;
    assert.equal(errors.length, 2);
    assert.deepStrictEqual(coordinator.getSnapshot('action-a').lifecycle, expected);
    const persistedStatus = status === 'timeout' ? 'error' : status;
    read = async () => conversationPage('action-a', null, 'process-a', persistedStatus);
    await coordinator.refresh('action-a');
    assert.equal(updates.at(-1).snapshot.lifecycle, null);
    assert.equal(updates.at(-1).snapshot.page.action.status, persistedStatus);
    read = async () => {
      throw new Error('network unavailable');
    };
    const started = coordinator.handleEvent(
      processEvent('process_started', 'action-a', 'process-b')
    );
    assert.deepStrictEqual(updates.at(-1).snapshot.lifecycle, {
      processId: 'process-b',
      status: 'processing',
    });
    await started.refreshDone;
  });
}
