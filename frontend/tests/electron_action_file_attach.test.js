const assert = require('assert');
const fs = require('fs');
const os = require('os');
const path = require('path');
const { test } = require('node:test');

const {
  registerActionAttachmentHandlers,
} = require('../electron/dist/ipc/handlers/actionAttachments.js');
const { IpcSenderRejectedError } = require('../electron/dist/ipc/senderTrust.js');
const { IpcValidationError } = require('../electron/dist/ipc/schemas/error.js');
const { ACTION_DOCUMENT_MAX_BYTES } = require('../electron/dist/ipc/schemas/actionAttachments.js');

const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

function toArrayBuffer(buffer) {
  return buffer.buffer.slice(buffer.byteOffset, buffer.byteOffset + buffer.byteLength);
}

function harness(overrides = {}) {
  const localArtifactRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'pantaray-attach-file-'));
  const handlers = new Map();
  const ctx = {
    actions: {
      getCurrentSubjectId: () => 'user-1',
      resolveOverlayIdForSender: () => 'overlay-1',
      ...overrides.actions,
    },
    actionImages: { localArtifactRoot },
  };
  registerActionAttachmentHandlers(ctx, {
    handle: (channel, handler) => handlers.set(channel, handler),
  });
  const stagingDirectory = (userId = 'user-1') =>
    path.join(localArtifactRoot, 'generated', 'attachments', userId);
  return {
    localArtifactRoot,
    stagingDirectory,
    staged: (userId) =>
      fs.existsSync(stagingDirectory(userId)) ? fs.readdirSync(stagingDirectory(userId)) : [],
    invoke: (channel, payload) => handlers.get(channel)({ sender: { id: 1 } }, payload),
  };
}

function attach(app, name, bytes = Buffer.from('%PDF-1.7 body')) {
  return app.invoke('action:attachFile', { bytes: toArrayBuffer(bytes), name });
}

test('attaching stages the bytes under the user namespace, named only by a fresh id', async () => {
  const app = harness();
  const bytes = Buffer.from('%PDF-1.7 body');

  const result = await attach(app, 'Quarterly Report.PDF', bytes);

  assert.match(result.attachmentId, UUID_PATTERN);
  assert.deepEqual(result, {
    attachmentId: result.attachmentId,
    name: 'Quarterly Report.PDF',
    byteSize: bytes.byteLength,
  });
  assert.deepEqual(app.staged(), [`${result.attachmentId}.pdf`]);
  const stored = path.join(app.stagingDirectory(), `${result.attachmentId}.pdf`);
  assert.deepEqual(fs.readFileSync(stored), bytes);
  assert.equal(fs.statSync(stored).mode & 0o777, 0o600);
});

test('attaching accepts every readable document format, whatever the case', async () => {
  const app = harness();
  for (const name of ['a.pdf', 'b.DOCX', 'c.xlsx', 'd.PpTx', 'e.ipynb']) {
    const { attachmentId } = await attach(app, name);
    const extension = path.extname(name).toLowerCase();
    assert.ok(app.staged().includes(`${attachmentId}${extension}`), name);
  }
});

test('attaching refuses a file the read tool cannot open, without writing anything', async () => {
  const app = harness();
  for (const name of ['notes.txt', 'archive.pdf.zip', 'pdf', '.pdf', '', '.', '..', 'x.pdf/']) {
    await assert.rejects(
      attach(app, name),
      (error) => error instanceof IpcValidationError,
      JSON.stringify(name)
    );
  }
  assert.equal(fs.existsSync(path.join(app.localArtifactRoot, 'generated')), false);
});

test('attaching refuses an empty file and one over the document byte limit', async () => {
  const app = harness();

  await assert.rejects(
    app.invoke('action:attachFile', { bytes: new ArrayBuffer(0), name: 'a.pdf' }),
    (error) => error instanceof IpcValidationError
  );
  await assert.rejects(
    attach(app, 'big.pdf', Buffer.alloc(ACTION_DOCUMENT_MAX_BYTES + 1)),
    (error) => error instanceof IpcValidationError
  );
  const atLimit = await attach(app, 'limit.pdf', Buffer.alloc(ACTION_DOCUMENT_MAX_BYTES));
  assert.equal(atLimit.byteSize, ACTION_DOCUMENT_MAX_BYTES);
  assert.deepEqual(app.staged(), [`${atLimit.attachmentId}.pdf`]);
});

test('attaching makes the name one safe path component and keeps it out of the staged path', async () => {
  const app = harness();

  const traversal = await attach(app, '../../etc/pass\\wd:x.pdf');
  assert.equal(traversal.name, '.._.._etc_pass_wd_x.pdf');

  const control = await attach(app, 'line\nbreak\u0000nul\u0085.docx');
  assert.equal(control.name, 'line_break_nul_.docx');

  // A decomposed name (as macOS file systems report it) is sent composed.
  const composed = 'レポート.xlsx';
  assert.notEqual(composed.normalize('NFD'), composed);
  const decomposed = await attach(app, composed.normalize('NFD'));
  assert.equal(decomposed.name, composed);

  // 300 three-byte characters: the stem is cut on a character boundary and the extension kept.
  const long = await attach(app, `${'資'.repeat(300)}.pptx`);
  assert.equal(long.name, `${'資'.repeat(83)}.pptx`);
  assert.ok(Buffer.byteLength(long.name) <= 255);

  assert.deepEqual(
    app.staged().sort(),
    [
      `${traversal.attachmentId}.pdf`,
      `${control.attachmentId}.docx`,
      `${decomposed.attachmentId}.xlsx`,
      `${long.attachmentId}.pptx`,
    ].sort()
  );
  assert.deepEqual(fs.readdirSync(path.join(app.localArtifactRoot, 'generated')), ['attachments']);
});

test('attaching and discarding refuse senders that are not a registered overlay or signed in', async () => {
  const bytes = toArrayBuffer(Buffer.from('%PDF'));
  const foreignSender = harness({ actions: { resolveOverlayIdForSender: () => null } });
  const signedOut = harness({ actions: { getCurrentSubjectId: () => null } });
  const attachmentId = '11111111-1111-4111-8111-111111111111';

  for (const [channel, payload] of [
    ['action:attachFile', { bytes, name: 'a.pdf' }],
    ['action:discardAttachment', { attachmentId }],
  ]) {
    await assert.rejects(
      foreignSender.invoke(channel, payload),
      (error) => error instanceof IpcSenderRejectedError
    );
    await assert.rejects(signedOut.invoke(channel, payload), /Missing authenticated user id/);
  }
  assert.equal(fs.existsSync(path.join(foreignSender.localArtifactRoot, 'generated')), false);
});

test('attaching refuses a session user id that is not one plain path segment', async () => {
  for (const userId of ['../escape', 'a/b', '..', '']) {
    const app = harness({ actions: { getCurrentSubjectId: () => userId } });
    await assert.rejects(attach(app, 'a.pdf'), /outside the user attachment namespace/, userId);
    assert.equal(fs.readdirSync(app.localArtifactRoot).includes('generated'), false, userId);
  }
});

test('discarding deletes only that staged file for the signed-in user', async () => {
  const app = harness();
  const kept = await attach(app, 'kept.pdf');
  const removed = await attach(app, 'removed.docx');
  const otherUser = app.stagingDirectory('user-2');
  fs.mkdirSync(otherUser, { recursive: true });
  fs.writeFileSync(path.join(otherUser, `${removed.attachmentId}.docx`), 'other user');

  assert.equal(
    await app.invoke('action:discardAttachment', { attachmentId: removed.attachmentId }),
    undefined
  );

  assert.deepEqual(app.staged(), [`${kept.attachmentId}.pdf`]);
  assert.deepEqual(app.staged('user-2'), [`${removed.attachmentId}.docx`]);
  // Already gone (sent, or discarded twice): nothing to do, not an error.
  await app.invoke('action:discardAttachment', { attachmentId: removed.attachmentId });
});

test('discarding refuses anything but an attachment id, leaving files outside untouched', async () => {
  const app = harness();
  const kept = await attach(app, 'kept.pdf');
  const outside = path.join(app.localArtifactRoot, 'generated', 'secret.pdf');
  fs.writeFileSync(outside, 'secret');

  for (const attachmentId of [
    '../secret',
    `../${kept.attachmentId}`,
    `${kept.attachmentId}.pdf`,
    '*',
    '',
  ]) {
    await assert.rejects(
      app.invoke('action:discardAttachment', { attachmentId }),
      (error) => error instanceof IpcValidationError,
      attachmentId
    );
  }
  await assert.rejects(
    app.invoke('action:discardAttachment', { attachmentId: kept.attachmentId, path: outside }),
    (error) => error instanceof IpcValidationError
  );
  assert.deepEqual(app.staged(), [`${kept.attachmentId}.pdf`]);
  assert.equal(fs.readFileSync(outside, 'utf8'), 'secret');
});
