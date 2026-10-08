const assert = require('assert');
const { createHash } = require('crypto');
const fs = require('fs');
const os = require('os');
const path = require('path');
const { test } = require('node:test');

const { registerActionImageHandlers } = require('../electron/dist/ipc/handlers/actionImages.js');
const { IpcSenderRejectedError } = require('../electron/dist/ipc/senderTrust.js');
const { IpcValidationError } = require('../electron/dist/ipc/schemas/error.js');
const { ACTION_IMAGE_MAX_BYTES } = require('../electron/dist/ipc/schemas/actionImages.js');
const { isValidImageStoragePath } = require('../electron/dist/protocol/imageStoragePath.js');

const PNG_SIGNATURE = [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a];

function pngBytes(payload = 'pixels') {
  return Buffer.concat([Buffer.from(PNG_SIGNATURE), Buffer.from(payload)]);
}

function toArrayBuffer(buffer) {
  return buffer.buffer.slice(buffer.byteOffset, buffer.byteOffset + buffer.byteLength);
}

const MAIN_WINDOW = { isDestroyed: () => false, webContents: { id: 9 } };

function harness(overrides = {}) {
  const localArtifactRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'pantaray-attach-'));
  const revealed = [];
  const handlers = new Map();
  const ctx = {
    windows: { getMainWindow: () => MAIN_WINDOW },
    actions: {
      getCurrentSubjectId: () => 'user-1',
      resolveOverlayIdForSender: () => 'overlay-1',
      ...overrides.actions,
    },
    actionImages: {
      localArtifactRoot,
      decodeImageDimensions: () => ({ widthPx: 800, heightPx: 600 }),
      revealInFolder: (absolutePath) => revealed.push(absolutePath),
      ...overrides.actionImages,
    },
  };
  registerActionImageHandlers(ctx, {
    handle: (channel, handler) => handlers.set(channel, handler),
  });
  return {
    localArtifactRoot,
    revealed,
    invoke: (channel, payload, sender = { id: 1 }) => handlers.get(channel)({ sender }, payload),
  };
}

test('attaching a PNG stores it under the user image namespace with owner-only permissions', async () => {
  const app = harness();
  const bytes = pngBytes();

  const result = await app.invoke('action:attachImage', {
    bytes: toArrayBuffer(bytes),
    declaredMimeType: 'image/png',
  });

  assert.equal(result.kind, 'attached');
  assert.equal(result.mimeType, 'image/png');
  assert.equal(result.byteSize, bytes.byteLength);
  assert.equal(result.sha256, createHash('sha256').update(bytes).digest('hex'));
  assert.deepEqual(
    { widthPx: result.widthPx, heightPx: result.heightPx },
    {
      widthPx: 800,
      heightPx: 600,
    }
  );
  assert.ok(isValidImageStoragePath({ userId: 'user-1', storagePath: result.storagePath }));
  assert.equal(result.storagePath.split('/')[1], new Date().toISOString().slice(0, 10));

  const stored = path.join(app.localArtifactRoot, 'generated', 'images', result.storagePath);
  assert.deepEqual(fs.readFileSync(stored), bytes);
  assert.equal(fs.statSync(stored).mode & 0o777, 0o600);
});

test('attaching refuses content that does not match the declared media type', async () => {
  const app = harness();
  const jpeg = Buffer.concat([Buffer.from([0xff, 0xd8, 0xff, 0xe0]), Buffer.from('pixels')]);

  const result = await app.invoke('action:attachImage', {
    bytes: toArrayBuffer(jpeg),
    declaredMimeType: 'image/png',
  });

  assert.deepEqual(result, { kind: 'rejected', reason: 'unsupported_media_type' });
  assert.equal(fs.existsSync(path.join(app.localArtifactRoot, 'generated')), false);
});

test('attaching refuses bytes that are not an allowed image at all', async () => {
  const app = harness();

  const result = await app.invoke('action:attachImage', {
    bytes: toArrayBuffer(Buffer.from('<svg xmlns="http://www.w3.org/2000/svg"/>')),
    declaredMimeType: 'image/png',
  });

  assert.deepEqual(result, { kind: 'rejected', reason: 'unsupported_media_type' });
});

test('attaching refuses an image over the per-image byte limit', async () => {
  const app = harness();
  const oversized = pngBytes('x'.repeat(ACTION_IMAGE_MAX_BYTES));

  const result = await app.invoke('action:attachImage', {
    bytes: toArrayBuffer(oversized),
    declaredMimeType: 'image/png',
  });

  assert.deepEqual(result, { kind: 'rejected', reason: 'too_large' });
  assert.equal(fs.existsSync(path.join(app.localArtifactRoot, 'generated')), false);
});

test('attaching refuses an image that cannot be decoded or exceeds the decoded pixel budget', async () => {
  const undecodable = harness({ actionImages: { decodeImageDimensions: () => null } });
  const huge = harness({
    actionImages: { decodeImageDimensions: () => ({ widthPx: 8000, heightPx: 8001 }) },
  });
  const payload = { bytes: toArrayBuffer(pngBytes()), declaredMimeType: 'image/png' };

  assert.deepEqual(await undecodable.invoke('action:attachImage', payload), {
    kind: 'rejected',
    reason: 'decode_failed',
  });
  assert.deepEqual(await huge.invoke('action:attachImage', { ...payload }), {
    kind: 'rejected',
    reason: 'decode_failed',
  });
});

test('attaching rejects malformed payloads and senders that are not a registered overlay', async () => {
  const app = harness();
  const bytes = toArrayBuffer(pngBytes());

  await assert.rejects(
    app.invoke('action:attachImage', { bytes, declaredMimeType: 'image/svg+xml' }),
    (error) => error instanceof IpcValidationError
  );
  await assert.rejects(
    app.invoke('action:attachImage', {
      bytes: Buffer.from(PNG_SIGNATURE),
      declaredMimeType: 'image/png',
    }),
    (error) => error instanceof IpcValidationError
  );
  await assert.rejects(
    app.invoke('action:attachImage', { bytes: new ArrayBuffer(0), declaredMimeType: 'image/png' }),
    (error) => error instanceof IpcValidationError
  );
  await assert.rejects(
    app.invoke('action:attachImage', { bytes, declaredMimeType: 'image/png', extra: 1 }),
    (error) => error instanceof IpcValidationError
  );

  const foreignSender = harness({ actions: { resolveOverlayIdForSender: () => null } });
  await assert.rejects(
    foreignSender.invoke('action:attachImage', { bytes, declaredMimeType: 'image/png' }),
    (error) => error instanceof IpcSenderRejectedError
  );

  // The main window's chat composer attaches too, through the same validation.
  const mainWindowOnly = harness({ actions: { resolveOverlayIdForSender: () => null } });
  const fromMain = await mainWindowOnly.invoke(
    'action:attachImage',
    { bytes, declaredMimeType: 'image/png' },
    MAIN_WINDOW.webContents
  );
  assert.equal(fromMain.kind, 'attached');
  await assert.rejects(
    mainWindowOnly.invoke(
      'action:attachImage',
      { bytes, declaredMimeType: 'image/svg+xml' },
      MAIN_WINDOW.webContents
    ),
    (error) => error instanceof IpcValidationError
  );

  const signedOut = harness({ actions: { getCurrentSubjectId: () => null } });
  await assert.rejects(
    signedOut.invoke('action:attachImage', { bytes, declaredMimeType: 'image/png' }),
    /Missing authenticated user id/
  );
});

test('revealing an image resolves it through the same validation as the image protocol', async () => {
  const app = harness();
  const attached = await app.invoke('action:attachImage', {
    bytes: toArrayBuffer(pngBytes()),
    declaredMimeType: 'image/png',
  });

  assert.deepEqual(await app.invoke('actionImage:reveal', { storagePath: attached.storagePath }), {
    revealed: true,
  });
  assert.deepEqual(app.revealed, [
    fs.realpathSync(path.join(app.localArtifactRoot, 'generated', 'images', attached.storagePath)),
  ]);

  assert.deepEqual(
    await app.invoke('actionImage:reveal', {
      storagePath: attached.storagePath.replace('user-1/', 'user-2/'),
    }),
    { revealed: false }
  );
  assert.deepEqual(
    await app.invoke('actionImage:reveal', { storagePath: 'user-1/2026-09-08/../secret.png' }),
    { revealed: false }
  );
  assert.equal(app.revealed.length, 1);
});
