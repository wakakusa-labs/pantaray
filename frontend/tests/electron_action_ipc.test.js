const assert = require('assert');
const { test } = require('node:test');

const { registerActionHandlers } = require('../electron/dist/ipc/handlers/actions.js');
const { LocalBackendRequestError } = require('../electron/dist/localBackend/client.js');
const { IpcSenderRejectedError } = require('../electron/dist/ipc/senderTrust.js');

const MESSAGE_RESPONSE = {
  action_id: 'action-1',
  message_id: 'message-1',
  step_id: 'step-1',
  action_status: 'queued',
  disposition: 'started',
  process_id: 'process-1',
};

function register(overrides, overlayOverrides) {
  const handlers = new Map();
  const actions = {
    getCurrentSubjectId: () => 'user-1',
    resolveOverlayIdForSender: () => 'overlay-1',
    registerActionAssociation: () => {},
    refreshActionConversation: () => {},
    ...overrides,
  };
  const overlay = { resumeLiveProcess: () => {}, ...overlayOverrides };
  registerActionHandlers(
    { actions, overlay },
    { handle: (channel, handler) => handlers.set(channel, handler) }
  );
  return (channel, payload, sender = { id: 1 }) => handlers.get(channel)({ sender }, payload);
}

function spy(result) {
  const calls = [];
  const fn = async (value) => {
    calls.push(value);
    return result;
  };
  fn.calls = calls;
  return fn;
}

test('Action IPC validates each renderer payload before calling the main fetcher', async () => {
  const submitMessage = spy(MESSAGE_RESPONSE);
  const readConversationPage = spy({ runs: [] });
  const readToolOutputPage = spy({ content: 'done' });
  const invoke = register({ submitMessage, readConversationPage, readToolOutputPage });

  await assert.rejects(
    invoke('action:submitMessage', {
      target: { kind: 'new' },
      message: {
        version: 1,
        message_id: 'message-1',
        content: 'hello',
        images: Array.from({ length: 33 }, () => ({
          kind: 'image',
          storage_path: 'user-1/2026-09-08/input.png',
        })),
      },
    }),
    (error) => error?.name === 'IpcValidationError'
  );
  await assert.rejects(
    invoke('action:readConversationPage', { actionId: ' action-1 ', cursor: null, limit: 101 }),
    (error) => error?.name === 'IpcValidationError'
  );
  await assert.rejects(
    invoke('action:readToolOutputPage', {
      actionId: 'action-1',
      stepId: 'step-1',
      cursor: null,
      limitBytes: 65_537,
    }),
    (error) => error?.name === 'IpcValidationError'
  );

  assert.equal(submitMessage.calls.length, 0);
  assert.equal(readConversationPage.calls.length, 0);
  assert.equal(readToolOutputPage.calls.length, 0);

  const submit = {
    target: { kind: 'existing', action_id: 'action-1', expected_process_id: null },
    message: {
      version: 1,
      message_id: ' message-1 ',
      content: ' hello ',
      images: [],
      language: 'ja',
      files: [
        { attachment_id: '22222222-2222-4222-8222-222222222222', name: 'plan.pdf', byte_size: 42 },
      ],
    },
  };
  const page = { actionId: 'action-1', cursor: null, limit: 25 };
  const output = {
    actionId: 'action-1',
    stepId: 'step-1',
    cursor: 'cursor-1',
    limitBytes: 16_384,
  };
  await invoke('action:submitMessage', submit);
  await invoke('action:readConversationPage', page);
  await invoke('action:readToolOutputPage', output);

  assert.equal(submitMessage.calls[0].message.message_id, 'message-1');
  assert.equal(submitMessage.calls[0].message.content, 'hello');
  assert.deepEqual(submitMessage.calls[0].message.files, submit.message.files);
  assert.deepEqual(readConversationPage.calls, [page]);
  assert.deepEqual(readToolOutputPage.calls, [output]);
});

test('Action conversation IPC maps only a stale pagination cursor to an explicit result', async () => {
  const success = { action: { action_id: 'action-1' }, runs: [] };
  const invoke = register({
    readConversationPage: async ({ cursor }) => {
      if (cursor === 'stale') throw new LocalBackendRequestError('stale', 409);
      if (cursor === 'failed') throw new LocalBackendRequestError('failed', 500);
      if (cursor === null) throw new LocalBackendRequestError('initial conflict', 409);
      return success;
    },
  });

  const request = { actionId: 'action-1', limit: 25 };
  assert.equal(await invoke('action:readConversationPage', { ...request, cursor: 'ok' }), success);
  assert.deepEqual(await invoke('action:readConversationPage', { ...request, cursor: 'stale' }), {
    kind: 'stale_cursor',
  });
  await assert.rejects(
    invoke('action:readConversationPage', { ...request, cursor: null }),
    (error) => error instanceof LocalBackendRequestError && error.status === 409
  );
  await assert.rejects(
    invoke('action:readConversationPage', { ...request, cursor: 'failed' }),
    (error) => error instanceof LocalBackendRequestError && error.status === 500
  );
});

test('Action submit binds the same Overlay before starting canonical refresh', async () => {
  const calls = [];
  const response = { ...MESSAGE_RESPONSE, action_id: 'action-new' };
  const sender = { id: 7 };
  const invoke = register(
    {
      getCurrentSubjectId: () => {
        calls.push('subject');
        return 'user-1';
      },
      resolveOverlayIdForSender: (value) => {
        calls.push(value === sender ? 'resolve_sender' : 'resolve_other');
        return 'overlay-1';
      },
      submitMessage: async () => {
        calls.push('submit');
        return response;
      },
      registerActionAssociation: (actionId, overlayId) =>
        calls.push(`bind:${actionId}:${overlayId}`),
      refreshActionConversation: (actionId) => calls.push(`refresh:${actionId}`),
    },
    {
      resumeLiveProcess: (request) =>
        calls.push(
          `resume:${request.kind}:${request.processId}:${request.actionId}:${request.fromStart}`
        ),
    }
  );

  assert.deepEqual(await invoke('action:submitMessage', validSubmit(), sender), {
    kind: 'submitted',
    response,
  });
  assert.deepEqual(calls, [
    'resolve_sender',
    'subject',
    'submit',
    'subject',
    'resolve_sender',
    'bind:action-new:overlay-1',
    'refresh:action-new',
    'resume:action:process-1:action-new:true',
  ]);
});

test('Deferred Action submit never requests a live relay attach', async () => {
  const resumed = [];
  const response = {
    ...MESSAGE_RESPONSE,
    disposition: 'pending',
    process_id: null,
  };
  const invoke = register(
    { submitMessage: async () => response },
    { resumeLiveProcess: (request) => resumed.push(request) }
  );

  assert.deepEqual(await invoke('action:submitMessage', validSubmit()), {
    kind: 'submitted',
    response,
  });
  assert.deepEqual(resumed, []);
});

test('Committed Action submit skips local delivery when subject or sender changed', async () => {
  for (const afterSubmit of [
    (state) => {
      state.subjectId = 'user-2';
    },
    (state) => {
      state.overlayId = null;
    },
    (state) => {
      state.overlayId = 'overlay-2';
    },
  ]) {
    const state = { subjectId: 'user-1', overlayId: 'overlay-1' };
    let resolveSubmit;
    const submitted = new Promise((resolve) => {
      resolveSubmit = resolve;
    });
    const delivered = [];
    const invoke = register(
      {
        getCurrentSubjectId: () => state.subjectId,
        resolveOverlayIdForSender: () => state.overlayId,
        submitMessage: () => submitted,
        registerActionAssociation: () => delivered.push('bind'),
        refreshActionConversation: () => delivered.push('refresh'),
      },
      { resumeLiveProcess: () => delivered.push('resume') }
    );
    const result = invoke('action:submitMessage', validSubmit());
    afterSubmit(state);
    resolveSubmit(MESSAGE_RESPONSE);

    assert.deepEqual(await result, { kind: 'submitted', response: MESSAGE_RESPONSE });
    assert.deepEqual(delivered, []);
  }
});

test('Action submit maps only typed conflicts and never delivers a failed HTTP call', async () => {
  const submitMessage = spy(MESSAGE_RESPONSE);
  const invokeWithoutOverlay = register({ resolveOverlayIdForSender: () => null, submitMessage });
  await assert.rejects(
    invokeWithoutOverlay('action:submitMessage', validSubmit()),
    (error) =>
      error instanceof IpcSenderRejectedError && error.code === 'overlay_window_not_registered'
  );
  assert.equal(submitMessage.calls.length, 0);

  const cases = [
    {
      error: new LocalBackendRequestError('stale process', 409, 'ExpectedProcessConflict'),
      result: { kind: 'expected_process_conflict' },
    },
    {
      error: new LocalBackendRequestError('action conflict', 409, 'ActionConflict'),
      result: { kind: 'action_conflict' },
    },
    { error: new LocalBackendRequestError('other conflict', 409, 'MessageIdentityConflict') },
    { error: new LocalBackendRequestError('backend failed', 500, 'InternalError') },
  ];
  for (const current of cases) {
    const delivered = [];
    const invokeWithFailedHttp = register(
      {
        submitMessage: async () => {
          throw current.error;
        },
        registerActionAssociation: () => delivered.push('bind'),
        refreshActionConversation: () => delivered.push('refresh'),
      },
      { resumeLiveProcess: () => delivered.push('resume') }
    );
    const submission = invokeWithFailedHttp('action:submitMessage', validSubmit());
    if (current.result) assert.deepEqual(await submission, current.result);
    else await assert.rejects(submission, (error) => error === current.error);
    assert.deepEqual(delivered, []);
  }
});

function validSubmit() {
  return {
    target: { kind: 'existing', action_id: 'action-1', expected_process_id: null },
    message: {
      version: 1,
      message_id: 'message-1',
      content: 'hello',
      images: [],
      language: 'ja',
    },
  };
}
