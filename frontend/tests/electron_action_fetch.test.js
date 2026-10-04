const assert = require('assert');
const { test } = require('node:test');

const {
  ActionWireContractError,
  codePointSpanInTrimmedText,
} = require('../electron/dist/actions/actionContracts.js');
const {
  ACTION_CONVERSATION_REQUEST_TIMEOUT_MS,
  createActionFetcher,
} = require('../electron/dist/actions/actionFetch.js');
const { LocalBackendRequestError } = require('../electron/dist/localBackend/client.js');

const MESSAGE_REQUEST = {
  target: {
    kind: 'existing',
    action_id: 'action-1',
    expected_process_id: null,
  },
  message: {
    version: 1,
    message_id: 'message-1',
    content: 'Continue the Action',
    images: [],
    language: 'ja',
  },
};
const MESSAGE_RESPONSE = {
  action_id: 'action-1',
  message_id: 'message-1',
  step_id: 'step-user-3',
  action_status: 'queued',
  disposition: 'started',
  process_id: 'run-current',
};
const USER_ENTRY = {
  step_kind: 'user',
  approved_suggestion: null,
  step_id: 'step-user-2',
  step_number: 2,
  message_id: 'message-2',
  accepted_sequence: 2,
  content: 'Continue',
  images: [
    { kind: 'image', storage_path: 'captures/image.png' },
    { kind: 'image', storage_path: 'captures/screen.png' },
  ],
  project_refs: [],
  status: 'adopted',
};
const TOOL_ENTRY = {
  step_kind: 'tool',
  step_id: 'step-tool-2',
  step_number: 2,
  label: 'bash',
  status: 'processing',
  output_available: false,
};
const CONVERSATION_PAGE = {
  action: {
    action_id: 'action-1',
    suggestion_id: null,
    status: 'processing',
    latest_run_id: 'run-current',
    approved_suggestion: null,
    resumable: false,
  },
  runs: [
    {
      run_id: 'run-current',
      status: 'running',
      started_at: '2026-08-30T01:00:00.000000Z',
      completed_at: null,
      completion_event_id: null,
      entries: [USER_ENTRY, TOOL_ENTRY],
      final_output: null,
      error: null,
    },
    {
      run_id: 'run-older',
      status: 'success',
      started_at: '2026-08-30T00:00:00.000000Z',
      completed_at: '2026-08-30T00:30:00.000000Z',
      completion_event_id: 'event-older',
      entries: [
        {
          ...USER_ENTRY,
          step_id: 'step-user-1',
          step_number: 1,
          message_id: null,
          accepted_sequence: 1,
          images: [],
        },
      ],
      final_output: 'Completed the older run',
      error: null,
    },
  ],
  unadopted_messages: [
    {
      ...USER_ENTRY,
      step_id: 'step-user-3',
      step_number: null,
      message_id: 'message-3',
      accepted_sequence: 3,
      images: [],
      status: 'pending',
    },
  ],
  next_cursor: 'conversation-cursor',
};
const TOOL_OUTPUT_PAGE = {
  content: 'first page 😀',
  next_cursor: 'tool-cursor-next',
  truncated: false,
  unavailable_reason: null,
};

function createFetcher(requestJson, getUserId = () => 'user/1') {
  return createActionFetcher({ requestJson, getUserId });
}

test('Action fetcher は authenticated user path と3本のpaged wireをそのまま使う', async () => {
  const requests = [];
  const fetcher = createFetcher(async (request) => {
    requests.push(request);
    if (request.method === 'POST') return MESSAGE_RESPONSE;
    if (request.path.endsWith('/state')) {
      return {
        ...CONVERSATION_PAGE,
        action: { ...CONVERSATION_PAGE.action, action_id: 'action/1' },
      };
    }
    return TOOL_OUTPUT_PAGE;
  });

  const message = await fetcher.submitMessage(MESSAGE_REQUEST);
  const conversation = await fetcher.readConversationPage({
    actionId: 'action/1',
    cursor: null,
    limit: 25,
  });
  const output = await fetcher.readToolOutputPage({
    actionId: 'action/1',
    stepId: 'step/1',
    cursor: 'tool-cursor',
    limitBytes: 16_384,
  });

  assert.equal(message.process_id, 'run-current');
  assert.deepStrictEqual(
    conversation.runs.map((run) => run.run_id),
    ['run-current', 'run-older']
  );
  assert.equal(output.next_cursor, 'tool-cursor-next');
  assert.deepStrictEqual(requests, [
    {
      path: '/v1/agents/users/user%2F1/actions/messages',
      method: 'POST',
      body: MESSAGE_REQUEST,
      timeoutMs: ACTION_CONVERSATION_REQUEST_TIMEOUT_MS,
    },
    {
      path: '/v1/agents/users/user%2F1/actions/action%2F1/state',
      method: 'GET',
      query: { cursor: null, limit: 25 },
      timeoutMs: 30_000,
    },
    {
      path: '/v1/agents/users/user%2F1/actions/action%2F1/steps/step%2F1/output',
      method: 'GET',
      query: { cursor: 'tool-cursor', limit_bytes: 16_384 },
      timeoutMs: 30_000,
    },
  ]);
});

test('Action fetcher は authenticated user 不在時に送信せず timeout error を変換しない', async () => {
  let requests = 0;
  const noAuth = createFetcher(
    async () => {
      requests += 1;
      return CONVERSATION_PAGE;
    },
    () => null
  );
  await assert.rejects(
    () => noAuth.readConversationPage({ actionId: 'action-1', cursor: null, limit: 25 }),
    /Missing authenticated user id/
  );
  assert.equal(requests, 0);

  const timeout = new LocalBackendRequestError('timed out', null, 'LOCAL_BACKEND_REQUEST_TIMEOUT');
  const timed = createFetcher(async () => {
    requests += 1;
    throw timeout;
  });
  await assert.rejects(
    () => timed.readConversationPage({ actionId: 'action-1', cursor: null, limit: 25 }),
    (error) => error === timeout
  );
  assert.equal(requests, 1);
});

test('Action message request は nullable process fence を必須にし private fields を拒否する', async () => {
  let requests = 0;
  const fetcher = createFetcher(async () => {
    requests += 1;
    return MESSAGE_RESPONSE;
  });
  const invalidRequests = [
    {
      ...MESSAGE_REQUEST,
      target: { kind: 'existing', action_id: 'action-1' },
    },
    {
      ...MESSAGE_REQUEST,
      target: { kind: 'new', suggestion_id: 'suggestion-private' },
    },
    {
      ...MESSAGE_REQUEST,
      target: { kind: 'new', approval_mode: 'invalid' },
    },
    {
      ...MESSAGE_REQUEST,
      target: {
        kind: 'existing',
        action_id: 'action-1',
        expected_process_id: null,
        approval_mode: 'always_allow',
      },
    },
    {
      ...MESSAGE_REQUEST,
      message: { ...MESSAGE_REQUEST.message, supplement: 'private supplement' },
    },
  ];

  for (const request of invalidRequests) {
    await assert.rejects(() => fetcher.submitMessage(request));
  }
  assert.equal(requests, 0);
});

test('Action message request は画像参照を32枚まで通し不正な形は送信前に落とす', async () => {
  const requests = [];
  const fetcher = createFetcher(async (request) => {
    requests.push(request);
    return MESSAGE_RESPONSE;
  });
  const image = { kind: 'image', storage_path: 'user-1/2026-09-08/image.png' };
  const second = { kind: 'image', storage_path: 'user-1/2026-09-08/screen.png' };

  await fetcher.submitMessage({
    ...MESSAGE_REQUEST,
    message: { ...MESSAGE_REQUEST.message, images: [image, second] },
  });

  assert.deepEqual(requests[0].body.message.images, [image, second]);

  const invalidRequests = [
    {
      ...MESSAGE_REQUEST,
      message: {
        ...MESSAGE_REQUEST.message,
        images: Array.from({ length: 33 }, () => image),
      },
    },
    {
      ...MESSAGE_REQUEST,
      message: { ...MESSAGE_REQUEST.message, images: [{ ...image, kind: 'video' }] },
    },
    {
      ...MESSAGE_REQUEST,
      message: { ...MESSAGE_REQUEST.message, images: [{ ...image, storage_path: '  ' }] },
    },
    {
      ...MESSAGE_REQUEST,
      message: { ...MESSAGE_REQUEST.message, images: [{ ...image, private: 'field' }] },
    },
  ];
  for (const request of invalidRequests) {
    await assert.rejects(() => fetcher.submitMessage(request));
  }
  assert.equal(requests.length, 1);
});

test('Action message request はtrim後のUnicode code-point上限をserverと共有する', async () => {
  const requests = [];
  const fetcher = createFetcher(async (request) => {
    requests.push(request);
    return {
      ...MESSAGE_RESPONSE,
      action_id: request.body.target.action_id,
      message_id: request.body.message.message_id,
    };
  });
  const idAtLimit = '😀'.repeat(128);
  const contentAtLimit = '😀'.repeat(32_000);
  const bounded = {
    target: {
      kind: 'existing',
      action_id: ` ${idAtLimit} `,
      expected_process_id: ` ${idAtLimit} `,
    },
    message: {
      ...MESSAGE_REQUEST.message,
      message_id: ` ${idAtLimit} `,
      content: ` ${contentAtLimit} `,
    },
  };

  await fetcher.submitMessage(bounded);
  assert.equal(requests[0].body.target.action_id, idAtLimit);
  assert.equal(requests[0].body.target.expected_process_id, idAtLimit);
  assert.equal(requests[0].body.message.message_id, idAtLimit);
  assert.equal(requests[0].body.message.content, contentAtLimit);

  const idOverLimit = '😀'.repeat(129);
  const invalidRequests = [
    { ...bounded, target: { ...bounded.target, action_id: idOverLimit } },
    { ...bounded, target: { ...bounded.target, expected_process_id: idOverLimit } },
    { ...bounded, message: { ...bounded.message, message_id: idOverLimit } },
    { ...bounded, message: { ...bounded.message, content: '😀'.repeat(32_001) } },
  ];
  for (const request of invalidRequests) {
    await assert.rejects(() => fetcher.submitMessage(request));
  }
  assert.equal(requests.length, 1);
});

test('Composer の UTF-16 範囲は trim 後の本文の code-point 範囲になる', () => {
  // 🚀 is two UTF-16 units and one code point; the leading spaces are trimmed away.
  const draft = '  🚀 Check Demo App 😀 and Docs ';
  const utf16Start = draft.indexOf('Docs');
  const span = codePointSpanInTrimmedText(draft, utf16Start, utf16Start + 'Docs'.length);

  assert.deepEqual(span, { start: 23, end: 27 });
  assert.equal(Array.from(draft.trim()).slice(span.start, span.end).join(''), 'Docs');
  const emoji = draft.indexOf('😀');
  assert.deepEqual(codePointSpanInTrimmedText(draft, emoji, emoji + 2), { start: 17, end: 18 });
});

test('Action message request は project_refs を通し、上限超過と未知の欄を送信前に落とす', async () => {
  const requests = [];
  const fetcher = createFetcher(async (request) => {
    requests.push(request);
    return MESSAGE_RESPONSE;
  });
  const ref = {
    project_id: 'project-1',
    display_name: 'Demo App',
    paths: ['/workspace/demo-app'],
    start: 0,
    end: 8,
  };

  await fetcher.submitMessage({
    ...MESSAGE_REQUEST,
    message: { ...MESSAGE_REQUEST.message, content: 'Demo App', project_refs: [ref] },
  });

  assert.deepEqual(requests[0].body.message.project_refs, [ref]);
  for (const projectRefs of [
    Array.from({ length: 33 }, () => ref),
    [{ ...ref, paths: Array.from({ length: 33 }, () => '/workspace') }],
    [{ ...ref, start: -1 }],
    [{ ...ref, folder_ids: [] }],
  ]) {
    await assert.rejects(() =>
      fetcher.submitMessage({
        ...MESSAGE_REQUEST,
        message: { ...MESSAGE_REQUEST.message, project_refs: projectRefs },
      })
    );
  }
  assert.equal(requests.length, 1);
});

test('Action fetcher はrequestとresponseのconversation identity不一致を拒否する', async () => {
  const mismatches = [
    {
      response: { ...MESSAGE_RESPONSE, message_id: 'other-message' },
      call: (fetcher) => fetcher.submitMessage(MESSAGE_REQUEST),
    },
    {
      response: { ...MESSAGE_RESPONSE, action_id: 'other-action' },
      call: (fetcher) => fetcher.submitMessage(MESSAGE_REQUEST),
    },
    {
      response: {
        ...CONVERSATION_PAGE,
        action: { ...CONVERSATION_PAGE.action, action_id: 'other-action' },
      },
      call: (fetcher) =>
        fetcher.readConversationPage({ actionId: 'action-1', cursor: null, limit: 25 }),
    },
  ];

  for (const { response, call } of mismatches) {
    await assert.rejects(() => call(createFetcher(async () => response)), ActionWireContractError);
  }
});

test('tool entry の images は省略時に空で、載っていれば storage_path をそのまま通す', async () => {
  const withImages = structuredClone(CONVERSATION_PAGE);
  withImages.runs[0].entries[1].images = [
    { kind: 'image', storage_path: 'user-1/2026-09-08/11111111-1111-4111-8111-111111111111.png' },
  ];
  const legacy = await createFetcher(async () => CONVERSATION_PAGE).readConversationPage({
    actionId: 'action-1',
    cursor: null,
    limit: 25,
  });
  const captured = await createFetcher(async () => withImages).readConversationPage({
    actionId: 'action-1',
    cursor: null,
    limit: 25,
  });

  assert.deepStrictEqual(legacy.runs[0].entries[1].images, []);
  assert.deepStrictEqual(captured.runs[0].entries[1].images, [
    { kind: 'image', storage_path: 'user-1/2026-09-08/11111111-1111-4111-8111-111111111111.png' },
  ]);

  const unnamed = structuredClone(withImages);
  unnamed.runs[0].entries[1].images = [{ kind: 'image', storage_path: '   ' }];
  await assert.rejects(
    () =>
      createFetcher(async () => unnamed).readConversationPage({
        actionId: 'action-1',
        cursor: null,
        limit: 25,
      }),
    ActionWireContractError
  );
});

test('tool entry の outcome は省略時 completed で、denied/unavailable/preparing はそのまま通る', async () => {
  const legacy = await createFetcher(async () => CONVERSATION_PAGE).readConversationPage({
    actionId: 'action-1',
    cursor: null,
    limit: 25,
  });
  assert.strictEqual(legacy.runs[0].entries[1].outcome, 'completed');

  for (const outcome of ['denied', 'unavailable', 'preparing']) {
    const page = structuredClone(CONVERSATION_PAGE);
    page.runs[0].entries[1].outcome = outcome;
    const parsed = await createFetcher(async () => page).readConversationPage({
      actionId: 'action-1',
      cursor: null,
      limit: 25,
    });
    assert.strictEqual(parsed.runs[0].entries[1].outcome, outcome);
  }

  const unknown = structuredClone(CONVERSATION_PAGE);
  unknown.runs[0].entries[1].outcome = 'skipped';
  await assert.rejects(
    () =>
      createFetcher(async () => unknown).readConversationPage({
        actionId: 'action-1',
        cursor: null,
        limit: 25,
      }),
    ActionWireContractError
  );
});

test('Action fetcher は各response階層のunknown/private fieldを外へ返さない', async () => {
  const stateWithPrivateTool = structuredClone(CONVERSATION_PAGE);
  stateWithPrivateTool.runs[0].entries[1].tool_input = 'private-value';
  const cases = [
    {
      payload: { ...MESSAGE_RESPONSE, job_id: 'private-value' },
      call: (fetcher) => fetcher.submitMessage(MESSAGE_REQUEST),
    },
    {
      payload: stateWithPrivateTool,
      call: (fetcher) =>
        fetcher.readConversationPage({ actionId: 'action-1', cursor: null, limit: 25 }),
    },
    {
      payload: { ...TOOL_OUTPUT_PAGE, storage_path: 'private-value' },
      call: (fetcher) =>
        fetcher.readToolOutputPage({
          actionId: 'action-1',
          stepId: 'step-1',
          cursor: null,
          limitBytes: 16_384,
        }),
    },
  ];

  for (const { payload, call } of cases) {
    const fetcher = createFetcher(async () => payload);
    await assert.rejects(
      () => call(fetcher),
      (error) =>
        error instanceof ActionWireContractError && !error.message.includes('private-value')
    );
  }
});

test('Action fetcher はdisposition・run outcome・tool availabilityの破損を拒否する', async () => {
  const malformedConversation = structuredClone(CONVERSATION_PAGE);
  malformedConversation.runs[1].final_output = null;
  const reversedConversation = structuredClone(CONVERSATION_PAGE);
  reversedConversation.runs[1].completed_at = '2026-08-29T23:59:59.999999Z';
  const cases = [
    {
      payload: { ...MESSAGE_RESPONSE, disposition: 'pending' },
      call: (fetcher) => fetcher.submitMessage(MESSAGE_REQUEST),
    },
    {
      payload: malformedConversation,
      call: (fetcher) =>
        fetcher.readConversationPage({ actionId: 'action-1', cursor: null, limit: 25 }),
    },
    {
      payload: reversedConversation,
      call: (fetcher) =>
        fetcher.readConversationPage({ actionId: 'action-1', cursor: null, limit: 25 }),
    },
    {
      payload: {
        content: 'must be empty',
        next_cursor: null,
        truncated: false,
        unavailable_reason: 'binary',
      },
      call: (fetcher) =>
        fetcher.readToolOutputPage({
          actionId: 'action-1',
          stepId: 'step-1',
          cursor: null,
          limitBytes: 16_384,
        }),
    },
  ];

  for (const { payload, call } of cases) {
    await assert.rejects(() => call(createFetcher(async () => payload)), ActionWireContractError);
  }
});

test('Action conversation page はserver-owned placement・identity・run相関の破損を拒否する', async (t) => {
  const terminalizeLatestRun = (page, status = 'success') => {
    page.action.status = status;
    page.runs[0].status = status;
    page.runs[0].completed_at = '2026-08-30T01:30:00.000000Z';
    page.runs[0].completion_event_id = 'event-current';
    page.runs[0].final_output = status === 'success' ? 'Completed' : null;
    page.runs[0].error = status === 'success' ? null : { code: status, message: 'Stopped' };
  };
  const cases = [
    ['run USER is not adopted', (page) => (page.runs[0].entries[0].status = 'pending')],
    ['unadopted USER is adopted', (page) => (page.unadopted_messages[0].status = 'adopted')],
    ['terminal Action has pending USER', (page) => terminalizeLatestRun(page)],
    ['step_id is duplicated', (page) => (page.unadopted_messages[0].step_id = USER_ENTRY.step_id)],
    [
      'non-null message_id is duplicated',
      (page) => (page.unadopted_messages[0].message_id = USER_ENTRY.message_id),
    ],
    [
      'accepted_sequence is duplicated',
      (page) => (page.unadopted_messages[0].accepted_sequence = USER_ENTRY.accepted_sequence),
    ],
    [
      'active run has completion identity',
      (page) => (page.runs[0].completion_event_id = 'event-unexpected'),
    ],
    [
      'completed run lacks completion identity',
      (page) => (page.runs[1].completion_event_id = null),
    ],
    [
      'completion identity is not canonical',
      (page) => (page.runs[1].completion_event_id = ' event-old'),
    ],
    ['run_id is duplicated', (page) => (page.runs[1].run_id = page.runs[0].run_id)],
    [
      'nonlatest run is active',
      (page) => {
        page.runs[1].status = 'running';
        page.runs[1].completed_at = null;
        page.runs[1].completion_event_id = null;
        page.runs[1].final_output = null;
      },
    ],
    [
      'runs overlap without a visible latest run',
      (page) => {
        terminalizeLatestRun(page);
        page.action.latest_run_id = 'run-hidden';
        page.runs[1].completed_at = '2026-08-30T01:10:00.000000Z';
        page.unadopted_messages[0].status = 'not_executed';
      },
    ],
    [
      'approval-pending latest run belongs to a queued Action',
      (page) => {
        page.action.status = 'queued';
        page.runs[0].status = 'approval_pending';
      },
    ],
    [
      'terminal Action has an active latest run',
      (page) => {
        page.action.status = 'success';
        page.unadopted_messages[0].status = 'not_executed';
      },
    ],
    [
      'terminal statuses disagree',
      (page) => {
        terminalizeLatestRun(page, 'error');
        page.action.status = 'success';
        page.unadopted_messages[0].status = 'not_executed';
      },
    ],
  ];

  for (const [name, mutate] of cases) {
    await t.test(name, async () => {
      const page = structuredClone(CONVERSATION_PAGE);
      mutate(page);
      const fetcher = createFetcher(async () => page);
      await assert.rejects(
        () => fetcher.readConversationPage({ actionId: 'action-1', cursor: null, limit: 25 }),
        ActionWireContractError
      );
    });
  }
});

test('Action tool output は unavailable と read ceiling terminal page を区別する', async () => {
  const payloads = [
    { content: '', next_cursor: null, truncated: false, unavailable_reason: 'no_output' },
    { content: '', next_cursor: null, truncated: true, unavailable_reason: null },
  ];
  const fetcher = createFetcher(async () => payloads.shift());
  const request = {
    actionId: 'action-1',
    stepId: 'step-1',
    cursor: null,
    limitBytes: 16_384,
  };

  const unavailable = await fetcher.readToolOutputPage(request);
  const truncated = await fetcher.readToolOutputPage(request);
  assert.equal(unavailable.unavailable_reason, 'no_output');
  assert.equal(truncated.truncated, true);
});

test('conversation wire は canceled Action だけを resumable にする', async () => {
  const page = (status, resumable) => ({
    ...CONVERSATION_PAGE,
    action: { ...CONVERSATION_PAGE.action, status, resumable },
    runs: [
      {
        ...CONVERSATION_PAGE.runs[0],
        status: status === 'canceled' ? 'canceled' : 'running',
        completed_at: status === 'canceled' ? '2026-08-30T01:30:00.000000Z' : null,
        completion_event_id: status === 'canceled' ? 'event-canceled' : null,
        error: status === 'canceled' ? { code: 'ACTION_CANCELED', message: 'stopped' } : null,
      },
      CONVERSATION_PAGE.runs[1],
    ],
    unadopted_messages: [],
  });
  const read = (payload) =>
    createFetcher(async () => payload).readConversationPage({
      actionId: 'action-1',
      cursor: null,
      limit: 25,
    });

  assert.equal((await read(page('canceled', true))).action.resumable, true);
  await assert.rejects(
    () => read(page('processing', true)),
    (error) => error instanceof ActionWireContractError
  );
  const { resumable: _omitted, ...actionWithoutResumable } = CONVERSATION_PAGE.action;
  await assert.rejects(
    () => read({ ...CONVERSATION_PAGE, action: actionWithoutResumable }),
    (error) => error instanceof ActionWireContractError
  );
});

test('new Action request forwards the selected initial approval mode', async () => {
  const requests = [];
  const fetcher = createFetcher(async (request) => {
    requests.push(request);
    return MESSAGE_RESPONSE;
  });
  for (const mode of ['prompt_each_time', 'always_allow']) {
    await fetcher.submitMessage({
      ...MESSAGE_REQUEST,
      target: { kind: 'new', approval_mode: mode },
    });
  }
  assert.deepEqual(
    requests.map((request) => request.body.target),
    [
      { kind: 'new', approval_mode: 'prompt_each_time' },
      { kind: 'new', approval_mode: 'always_allow' },
    ]
  );
});
