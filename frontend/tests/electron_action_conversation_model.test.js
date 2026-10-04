const assert = require('assert');
const { test } = require('node:test');

const {
  applyActionConversationPage,
  mergeActionConversationLivePage,
  projectActionConversationView,
} = require('../electron/dist/actions/actionConversationModel.js');
const {
  ActionWireContractError,
  compareActionTimelinePosition,
  parseActionConversationPage,
} = require('../electron/dist/actions/actionContracts.js');
const {
  reduceActionLiveSnapshot,
  routeActionLiveEvent,
} = require('../electron/dist/actions/actionLiveCore.js');

const START_CURRENT = '2026-08-30T03:00:00.000000Z';

/** The run outcomes (final answers and terminal sentences) in display order. */
function outcomes(view) {
  return view.items.flatMap((item) =>
    item.kind === 'run'
      ? item.lines.flatMap((line) =>
          line.kind === 'final_output' || line.kind === 'terminal_outcome'
            ? [[line.status, line.text]]
            : []
        )
      : []
  );
}

function user(stepId, messageId, acceptedSequence, status = 'adopted') {
  return {
    step_kind: 'user',
    approved_suggestion: null,
    step_id: stepId,
    step_number: status === 'adopted' ? acceptedSequence : null,
    message_id: messageId,
    accepted_sequence: acceptedSequence,
    content: `USER ${messageId ?? stepId}`,
    images: [],
    project_refs: [],
    status,
  };
}

function tool(stepId, stepNumber, status = 'success') {
  return {
    step_kind: 'tool',
    step_id: stepId,
    step_number: stepNumber,
    label: 'bash',
    status,
    output_available: status !== 'processing',
  };
}

function transient(runId, stepId, stepNumber, status, processId = 'physical-process') {
  return {
    processId,
    runId,
    entry: { ...tool(stepId, stepNumber, status), output_available: false },
  };
}

test('conversation wireはUSER位置と同順位step_idのcanonical順を検証する', () => {
  const newer = tool('step-z', 2);
  const older = user('step-a', 'message-a', 2);

  page({ runs: [activeRun([newer, older])] });
  assert.throws(() => page({ runs: [activeRun([older, newer])] }), ActionWireContractError);
  for (const invalid of [
    { ...older, step_number: null },
    { ...user('step-pending', 'message-pending', 3, 'pending'), step_number: 3 },
    user('step-pending', 'message-pending', 3, 'pending'),
  ]) {
    assert.throws(() => page({ runs: [activeRun([invalid])] }), ActionWireContractError);
  }
});

function activeRun(entries, status = 'running') {
  return {
    run_id: 'run-current',
    status,
    started_at: START_CURRENT,
    completed_at: null,
    completion_event_id: null,
    entries,
    final_output: null,
    error: null,
  };
}

test('USER entry parses with or without the files it was sent with', () => {
  const files = [{ name: 'plan.pdf', byte_size: 42 }];
  const withFiles = page({ runs: [activeRun([{ ...user('step-a', 'message-a', 1), files }])] });
  assert.deepStrictEqual(withFiles.runs[0].entries[0].files, files);

  const withoutFiles = page({ runs: [activeRun([user('step-a', 'message-a', 1)])] });
  assert.equal(withoutFiles.runs[0].entries[0].files, undefined);

  for (const invalid of [[{ name: 'plan.pdf', byte_size: 0 }], [{ ...files[0], path: '/x' }]]) {
    assert.throws(
      () => page({ runs: [activeRun([{ ...user('step-a', 'message-a', 1), files: invalid }])] }),
      ActionWireContractError
    );
  }
});

test('未取得のrunに属する承認提案をActionメタデータに保持し、別の提案IDを拒否する', () => {
  const approvedSuggestion = { suggestion_id: 'suggestion-1', content: 'Review the changes?' };
  const parsed = page({ suggestionId: 'suggestion-1', approvedSuggestion });
  assert.deepStrictEqual(parsed.action.approved_suggestion, approvedSuggestion);
  for (const suggestionId of [null, 'another-suggestion']) {
    assert.throws(() => page({ suggestionId, approvedSuggestion }), ActionWireContractError);
  }
});

test('承認提案は追加コメントと別に保持し、通常発言の本文欠落や非公開情報を拒否する', () => {
  const proposal = { suggestion_id: 'suggestion-1', content: 'Review the changes?' };
  const entry = user('user-approval', 'message-approval', 1);
  for (const content of [null, 'Include the risks.']) {
    const parsed = page({
      suggestionId: proposal.suggestion_id,
      runs: [activeRun([{ ...entry, content, approved_suggestion: proposal }])],
    });
    assert.strictEqual(parsed.runs[0].entries[0].content, content);
    assert.deepStrictEqual(parsed.runs[0].entries[0].approved_suggestion, proposal);
  }
  for (const invalid of [
    { ...entry, content: null },
    { ...entry, approved_suggestion: { ...proposal, suggestion_id: '' } },
    { ...entry, approved_suggestion: { ...proposal, content: ' ' } },
    { ...entry, approved_suggestion: { ...proposal, organization_name: 'private provenance' } },
  ]) {
    assert.throws(
      () => page({ suggestionId: proposal.suggestion_id, runs: [activeRun([invalid])] }),
      ActionWireContractError
    );
  }
});

test('承認提案とActionの提案IDが異なるページを採用済み・未採用とも拒否する', () => {
  for (const status of ['adopted', 'not_executed']) {
    const entry = {
      ...user('approval', 'message-approval', 1, status),
      content: null,
      approved_suggestion: { suggestion_id: 'suggestion-1', content: 'Proposal' },
    };
    const placement = {
      runs: [activeRun(status === 'adopted' ? [entry] : [])],
      unadopted: status === 'adopted' ? [] : [entry],
    };
    page({ ...placement, suggestionId: 'suggestion-1' });
    for (const suggestionId of [null, 'another-suggestion']) {
      assert.throws(() => page({ ...placement, suggestionId }), ActionWireContractError);
    }
  }
});

test('assistant発言をユーザーの前に表示し、再取得でも重複や実行ログ化をしない', () => {
  const assistant = {
    step_kind: 'assistant',
    step_id: 'assistant-first',
    step_number: 1,
    content: '**Earlier observation**',
  };
  const current = page({ runs: [activeRun([tool('work', 3), user('reply', 'm', 2), assistant])] });
  const refreshed = mergeActionConversationLivePage([current], current);
  const view = projectActionConversationView(refreshed);
  assert.deepStrictEqual(
    view.items[0].lines.map((line) => line.kind),
    ['assistant', 'user', 'tool']
  );
  assert.strictEqual(view.items[0].lines[0].text, assistant.content);
  assert.strictEqual(view.items[0].lines[0].visibility, 'always');
});

function terminalRun({ runId, status, startedAt, completedAt, entries, text }) {
  return {
    run_id: runId,
    status,
    started_at: startedAt,
    completed_at: completedAt,
    completion_event_id: `event-${runId}`,
    entries,
    final_output: status === 'success' ? text : null,
    error: status === 'success' ? null : { code: `${status}_public`, message: text },
  };
}

function page({
  actionId = 'action-1',
  suggestionId = null,
  approvedSuggestion = null,
  actionStatus = 'processing',
  latestRunId = 'run-current',
  runs = [],
  unadopted = [],
  nextCursor = null,
}) {
  return parseActionConversationPage({
    action: {
      action_id: actionId,
      suggestion_id: suggestionId,
      approved_suggestion: approvedSuggestion,
      status: actionStatus,
      latest_run_id: latestRunId,
      resumable: false,
    },
    runs,
    unadopted_messages: unadopted,
    next_cursor: nextCursor,
  });
}

function buildPagedConversation() {
  const latest = page({
    runs: [
      activeRun([
        tool('step-4', 4, 'processing'),
        user('step-3', 'message-3', 3),
        user('step-2', 'message-2', 2),
      ]),
    ],
    nextCursor: 'cursor-unadopted',
  });
  const unadopted = page({
    unadopted: [
      user('step-7', 'message-7', 7, 'pending'),
      user('step-5', 'message-5', 5, 'pending'),
    ],
    nextCursor: 'cursor-older',
  });
  const older = page({
    runs: [
      terminalRun({
        runId: 'run-older',
        status: 'success',
        startedAt: '2026-08-30T01:00:00.000000Z',
        completedAt: '2026-08-30T02:00:00.000000Z',
        entries: [user('step-1', null, 1)],
        text: 'Older answer',
      }),
    ],
  });

  let chain = applyActionConversationPage(null, null, latest);
  chain = applyActionConversationPage(chain, 'cursor-unadopted', unadopted);
  return applyActionConversationPage(chain, 'cursor-older', older);
}

test('paged conversation はexact tailだけを追記しrun/entry identityを安定してmergeする', () => {
  const chain = buildPagedConversation();
  const view = projectActionConversationView(chain);

  assert.equal(chain.length, 3);
  // 未採用のUSER（step-5 / step-7）は実行中の run の開始後に送られたので、run 群の後ろへ古い順で並ぶ。
  assert.deepStrictEqual(
    view.items.map((item) => (item.kind === 'run' ? item.runId : item.entry.step_id)),
    ['run-older', 'run-current', 'step-5', 'step-7']
  );
  const current = view.items[1];
  assert.equal(current.kind, 'run');
  assert.deepStrictEqual(
    current.lines.flatMap((line) => ('entry' in line ? [line.entry.step_id] : [])),
    ['step-2', 'step-3', 'step-4']
  );
  assert.equal(view.nextCursor, null);
  assert.deepStrictEqual(outcomes(view), [['success', 'Older answer']]);
});

test('paged conversation はinvalid extensionを拒否しAction切替で表示を置換する', () => {
  const latest = page({ runs: [activeRun([user('step-1', 'message-1', 1)])], nextCursor: 'next' });
  const chain = applyActionConversationPage(null, null, latest);
  assert.throws(() => applyActionConversationPage(null, 'next', latest));
  assert.throws(() => applyActionConversationPage(chain, 'wrong', latest));
  assert.throws(
    () =>
      applyActionConversationPage(
        chain,
        'next',
        page({ actionId: 'action-2', runs: [activeRun([])] })
      ),
    /different Actions/
  );

  const replacement = page({
    actionStatus: 'canceled',
    runs: [
      terminalRun({
        runId: 'run-current',
        status: 'canceled',
        startedAt: START_CURRENT,
        completedAt: '2026-08-30T04:00:00.000000Z',
        entries: [user('step-adopted', 'message-pending', 9)],
        text: 'Stopped',
      }),
    ],
    unadopted: [user('step-not-executed', 'message-not-executed', 10, 'not_executed')],
  });
  const replaced = applyActionConversationPage(buildPagedConversation(), null, replacement);
  const view = projectActionConversationView(replaced);
  assert.equal(replaced.length, 1);
  assert.deepStrictEqual(
    view.items.map((item) => (item.kind === 'run' ? item.runId : item.entry.status)),
    ['run-current', 'not_executed']
  );
  assert.deepStrictEqual(outcomes(view), [['canceled', 'Stopped']]);

  const paged = buildPagedConversation();
  const switched = mergeActionConversationLivePage(paged, page({ actionId: 'action-2' }));
  assert.equal(projectActionConversationView(switched).action.action_id, 'action-2');
  assert.deepStrictEqual(outcomes(projectActionConversationView(switched)), []);
});

test('optimistic USER はcanonical message identityでdedupし会話の末尾へ投影する', () => {
  const chain = buildPagedConversation();
  const canonicalDuplicate = {
    request: {
      target: { kind: 'existing', action_id: 'action-1', expected_process_id: 'run-current' },
      message: { version: 1, message_id: 'message-5', content: 'Already durable', images: [] },
    },
    state: 'awaiting_refresh',
  };
  const failed = {
    request: {
      target: { kind: 'existing', action_id: 'action-1', expected_process_id: 'run-current' },
      message: { version: 1, message_id: 'message-6', content: 'Try again', images: [] },
    },
    state: 'failed',
  };
  const view = projectActionConversationView(chain, [canonicalDuplicate, failed, failed]);

  assert.deepStrictEqual(
    view.items.map((item) =>
      item.kind === 'run'
        ? item.runId
        : item.source === 'canonical'
          ? item.entry.message_id
          : item.submission.request.message.message_id
    ),
    ['run-older', 'run-current', 'message-5', 'message-7', 'message-6']
  );
  const optimistic = view.items[4];
  assert.equal(optimistic.kind, 'user');
  assert.equal(optimistic.source, 'optimistic');
  assert.deepStrictEqual(optimistic.submission, failed);

  const newConversation = projectActionConversationView(null, [
    {
      request: {
        target: { kind: 'new' },
        message: { version: 1, message_id: 'message-new', content: 'Start', images: [] },
      },
      state: 'submitting',
    },
  ]);
  assert.equal(newConversation.action, null);
  assert.equal(newConversation.items[0].kind, 'user');
});

test('view はUSER/outcomeを常時表示しterminal agent workとoutcomeを一意に投影する', () => {
  const runs = [
    terminalRun({
      runId: 'run-current',
      status: 'success',
      startedAt: '2026-08-30T05:00:00.000000Z',
      completedAt: '2026-08-30T06:00:00.000000Z',
      entries: [tool('tool-3', 6), user('user-3', 'message-3', 3)],
      text: 'Latest answer',
    }),
    terminalRun({
      runId: 'run-error',
      status: 'error',
      startedAt: '2026-08-30T03:00:00.000000Z',
      completedAt: '2026-08-30T04:00:00.000000Z',
      entries: [tool('tool-2', 4), user('user-2', 'message-2', 2)],
      text: 'Older failure',
    }),
    terminalRun({
      runId: 'run-canceled',
      status: 'canceled',
      startedAt: '2026-08-30T01:00:00.000000Z',
      completedAt: '2026-08-30T02:00:00.000000Z',
      entries: [tool('tool-1', 2), user('user-1', 'message-1', 1)],
      text: 'Canceled publicly',
    }),
  ];
  const terminal = projectActionConversationView(
    applyActionConversationPage(
      null,
      null,
      page({ actionStatus: 'success', latestRunId: 'run-current', runs })
    )
  );

  for (const run of terminal.items) {
    assert.equal(run.kind, 'run');
    const outcome = run.lines[run.lines.length - 1];
    assert.ok(outcome.kind === 'final_output' || outcome.kind === 'terminal_outcome');
    assert.equal(outcome.visibility, 'always');
    assert.equal(run.lines.find((line) => line.kind === 'user').visibility, 'always');
    assert.equal(run.lines.find((line) => line.kind === 'tool').visibility, 'agent_work');
  }
  assert.deepStrictEqual(outcomes(terminal), [
    ['canceled', 'Canceled publicly'],
    ['error', 'Older failure'],
    ['success', 'Latest answer'],
  ]);

  const active = projectActionConversationView(
    applyActionConversationPage(
      null,
      null,
      page({ runs: [activeRun([tool('live', 1, 'processing')])] })
    )
  );
  assert.deepStrictEqual(outcomes(active), []);
});

test('transient Tool はshared precedenceでcanonical位置だけを更新する', () => {
  const chain = applyActionConversationPage(
    null,
    null,
    page({
      runs: [
        activeRun([
          tool('canonical-terminal', 4),
          tool('canonical-processing', 3, 'processing'),
          user('user-2', 'message-2', 2),
        ]),
      ],
    })
  );
  const view = projectActionConversationView(
    chain,
    [],
    [
      transient('run-current', 'canonical-terminal', 4, 'error'),
      transient('run-current', 'canonical-processing', 3, 'timeout'),
    ]
  );
  const run = view.items[0];

  assert.equal(run.kind, 'run');
  assert.deepStrictEqual(
    run.lines.map((line) =>
      'entry' in line ? [line.entry.step_id, line.entry.status ?? null] : [line.kind, line.status]
    ),
    [
      ['user-2', 'adopted'],
      ['canonical-processing', 'timeout'],
      ['canonical-terminal', 'success'],
    ]
  );
});

test('transient Tool はexact run内だけでcanonical timelineへ挿入される', () => {
  const chain = applyActionConversationPage(
    null,
    null,
    page({
      runs: [activeRun([user('user-5', 'message-5', 5), tool('tool-3', 3)])],
    })
  );
  const visible = transient('run-current', 'tool-4', 4, 'processing');
  const wrongRun = transient('run-other', 'tool-hidden', 6, 'processing', 'run-current');
  const view = projectActionConversationView(chain, [], [visible, wrongRun]);
  const run = view.items[0];

  assert.equal(run.kind, 'run');
  assert.deepStrictEqual(
    run.lines.flatMap((line) => ('entry' in line ? [line.entry.step_id] : [])),
    ['tool-3', 'tool-4', 'user-5']
  );
  assert.deepStrictEqual(projectActionConversationView(null, [], [visible]).items, []);
});

// --- 1 THINK が複数 tool を並列に呼ぶバッチの投影順序 ---------------------------
// バックエンドは 1 THINK の K 件を宣言順に step_number n..n+K-1 で採番する。
// desktop 側は step_id で upsert し (step_number, step_id) で挿入するだけなので、
// live イベントの到着順・terminal 化の順序に関わらず step_number 順に描画されねばならない。

function toolStepEvent(stepId, stepNumber, status, runId = 'run-current') {
  const meta = {
    kind: 'action',
    action_id: 'action-1',
    process_id: 'process-1',
    suggestion_id: 'suggestion-1',
    command_id: 'command-1',
    logical_run_id: runId,
  };
  return {
    event: 'action_step',
    meta,
    data: {
      action_id: meta.action_id,
      process_id: meta.process_id,
      step_kind: 'tool',
      step_id: stepId,
      step_number: stepNumber,
      tool_id: 'read',
      label: `read ${stepNumber}`,
      status,
      started_at: '2026-09-07T01:00:00.000000Z',
      completed_at: status === 'processing' ? null : '2026-09-07T01:00:05.000000Z',
    },
  };
}

function transientStepsFromEvents(events) {
  let snapshot = null;
  for (const event of events) {
    snapshot = reduceActionLiveSnapshot(
      snapshot,
      'action-1',
      routeActionLiveEvent(event).transientEffect
    );
  }
  return snapshot.transientToolSteps;
}

function lineStates(run) {
  assert.equal(run.kind, 'run');
  return run.lines.map((line) =>
    'entry' in line ? [line.entry.step_id, line.entry.status] : [line.kind, line.status]
  );
}

const PARALLEL_BATCH = [
  ['S-5-TOOL', 5],
  ['S-6-TOOL', 6],
  ['S-7-TOOL', 7],
];

test('1 THINK の並列 tool step は同時 processing のまま step_number 順に投影される', () => {
  const chain = applyActionConversationPage(
    null,
    null,
    page({ runs: [activeRun([user('user-4', 'message-4', 4)])] })
  );
  const declared = PARALLEL_BATCH.map(([stepId, stepNumber]) =>
    toolStepEvent(stepId, stepNumber, 'processing')
  );

  const processing = transientStepsFromEvents(declared);
  assert.deepStrictEqual(
    lineStates(projectActionConversationView(chain, [], processing).items[0]),
    [
      ['user-4', 'adopted'],
      ['S-5-TOOL', 'processing'],
      ['S-6-TOOL', 'processing'],
      ['S-7-TOOL', 'processing'],
    ]
  );

  // terminal は 3 → 1 → 2 の順に届く。step_id upsert なので行位置も status も入れ替わらない。
  const outOfOrderTerminals = [
    toolStepEvent('S-7-TOOL', 7, 'success'),
    toolStepEvent('S-5-TOOL', 5, 'error'),
    toolStepEvent('S-6-TOOL', 6, 'timeout'),
  ];
  const settled = transientStepsFromEvents([...declared, ...outOfOrderTerminals]);
  assert.deepStrictEqual(
    settled.map((step) => step.entry.step_id),
    ['S-5-TOOL', 'S-6-TOOL', 'S-7-TOOL']
  );
  const expected = [
    ['user-4', 'adopted'],
    ['S-5-TOOL', 'error'],
    ['S-6-TOOL', 'timeout'],
    ['S-7-TOOL', 'success'],
  ];
  assert.deepStrictEqual(
    lineStates(projectActionConversationView(chain, [], settled).items[0]),
    expected
  );

  // 宣言順に届く保証すらない場合でも、投影は step_number だけで決まる。
  const shuffled = transientStepsFromEvents([
    declared[1],
    declared[2],
    declared[0],
    ...outOfOrderTerminals,
  ]);
  assert.deepStrictEqual(
    shuffled.map((step) => step.entry.step_id),
    ['S-6-TOOL', 'S-7-TOOL', 'S-5-TOOL']
  );
  assert.deepStrictEqual(
    lineStates(projectActionConversationView(chain, [], shuffled).items[0]),
    expected
  );
});

function permutations(values) {
  if (values.length <= 1) return [values];
  return values.flatMap((value, index) =>
    permutations([...values.slice(0, index), ...values.slice(index + 1)]).map((rest) => [
      value,
      ...rest,
    ])
  );
}

test('compareActionTimelinePosition は (step_number, step_id) 上の狭義全順序である', () => {
  const positions = [
    { step_number: 9, step_id: 'S-9-TOOL' },
    { step_number: 10, step_id: 'S-10-TOOL' },
    { step_number: 10, step_id: 'S-10-TOOL-B' },
    { step_number: 10, step_id: 'S-10-TOOL-A' },
    { step_number: 11, step_id: 'S-11-TOOL' },
  ];
  // 新しい順。step_number が同値のときだけ step_id の降順へ落ちる。
  const canonical = [
    [11, 'S-11-TOOL'],
    [10, 'S-10-TOOL-B'],
    [10, 'S-10-TOOL-A'],
    [10, 'S-10-TOOL'],
    [9, 'S-9-TOOL'],
  ];
  const orders = permutations(positions).map((shuffled) =>
    [...shuffled]
      .sort(compareActionTimelinePosition)
      .map((position) => [position.step_number, position.step_id])
  );

  assert.equal(orders.length, 120);
  for (const order of orders) assert.deepStrictEqual(order, canonical);
  // step_number が step_id の辞書順に優先する（S-10-TOOL < S-9-TOOL でも 10 が先）。
  assert.ok(compareActionTimelinePosition(positions[1], positions[0]) < 0);

  for (const left of positions) {
    for (const right of positions) {
      const forward = compareActionTimelinePosition(left, right);
      assert.equal(forward, -compareActionTimelinePosition(right, left));
      assert.equal(forward === 0, left === right);
      for (const middle of positions) {
        if (forward < 0 && compareActionTimelinePosition(right, middle) < 0) {
          assert.ok(compareActionTimelinePosition(left, middle) < 0);
        }
      }
    }
  }
});

test('live run replacement retains all loaded answers and removes adopted pending copies', () => {
  const old = terminalRun({
    runId: 'run-old',
    status: 'success',
    startedAt: '2026-08-30T00:00:00.000000Z',
    completedAt: '2026-08-30T00:01:00.000000Z',
    entries: [],
    text: 'old answer',
  });
  const prior = page({ actionStatus: 'success', latestRunId: 'run-old', runs: [old] });
  const pending = user('step-pending', 'message-pending', 3, 'pending');
  const cached = page({ unadopted: [pending] });
  const live = page({
    runs: [activeRun([user('step-pending', 'message-pending', 3)])],
    nextCursor: 'new-cursor',
  });
  const merged = mergeActionConversationLivePage([prior, cached], live);
  assert.deepStrictEqual(outcomes(projectActionConversationView(merged)), [
    ['success', 'old answer'],
  ]);
  assert.deepEqual(
    merged.flatMap((item) => item.unadopted_messages),
    []
  );
  assert.deepEqual(
    merged.flatMap((item) => item.runs.map((run) => run.run_id)),
    ['run-current', 'run-old']
  );
});

test('terminal live pages retain unadopted message text as not executed before history reload', () => {
  const pending = user('step-pending', 'message-pending', 3, 'pending');
  for (const status of ['success', 'error', 'canceled']) {
    const current = page({ runs: [activeRun([])], unadopted: [pending] });
    const terminal = page({
      actionStatus: status,
      runs: [
        terminalRun({
          runId: 'run-current',
          status,
          startedAt: START_CURRENT,
          completedAt: '2026-08-30T03:01:00.000000Z',
          entries: [],
          text: 'finished',
        }),
      ],
    });
    const retained = mergeActionConversationLivePage([current], terminal).flatMap(
      (item) => item.unadopted_messages
    );
    assert.deepEqual(retained, [{ ...pending, status: 'not_executed' }]);
    assert.equal(current.unadopted_messages[0].status, 'pending');
  }
});
