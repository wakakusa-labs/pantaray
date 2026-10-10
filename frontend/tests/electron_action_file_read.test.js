const assert = require('assert');
const fs = require('fs');
const os = require('os');
const path = require('path');
const { test } = require('node:test');

const { registerActionFileHandlers } = require('../electron/dist/ipc/handlers/actionFiles.js');
const { IpcValidationError } = require('../electron/dist/ipc/schemas/error.js');
const {
  ACTION_FILE_IMAGE_MAX_BYTES,
  ACTION_FILE_TEXT_MAX_BYTES,
} = require('../electron/dist/actions/actionFileAccess.js');
const {
  createActionFileProtocolHandler,
} = require('../electron/dist/protocol/actionFileProtocol.js');
const { buildActionFileUrl } = require('../electron/dist/protocol/actionFileUrl.js');

const PNG = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 0x00, 0x00, 0x00, 0x0d]);
const NOT_FOUND = { kind: 'unavailable', reason: 'not_found' };

function page({ finalOutput = null, patched = [], nextCursor = null }) {
  return {
    action: { action_id: 'act-1' },
    runs: [
      {
        run_id: 'run-1',
        final_output: finalOutput,
        entries: patched.map((subject, index) => ({
          step_kind: 'tool',
          step_id: `tool-${index}`,
          label: 'apply_patch',
          subject,
        })),
      },
    ],
    next_cursor: nextCursor,
  };
}

const links = (files) =>
  files.map((file) => `[${path.basename(file)}](pantaray-file://${encodeURI(file)})`).join('\n') ||
  null;

/**
 * An Action whose newest page's answer links `linked`, and whose older page's answer links
 * `olderLinked` after apply_patch steps whose subjects are `patched`.
 */
function harness({ linked = [], olderLinked = [], patched = [], openPathResult = '' } = {}) {
  const dir = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), 'pantaray-action-files-')));
  const reads = [];
  const readConversationPage = async (request) => {
    reads.push(request);
    if (request.actionId !== 'act-1') throw new Error('Action not found');
    if (request.cursor === null) return page({ finalOutput: links(linked), nextCursor: 'older' });
    return page({ finalOutput: links(olderLinked), patched });
  };
  const opened = [];
  const handlers = new Map();
  registerActionFileHandlers(
    {
      actions: { readConversationPage },
      actionFiles: {
        open: () => {},
        openInApp: async (realPath) => {
          opened.push(realPath);
          return openPathResult;
        },
      },
    },
    { handle: (channel, handler) => handlers.set(channel, handler) }
  );
  const protocol = createActionFileProtocolHandler({ readConversationPage });
  return {
    dir,
    reads,
    opened,
    file: (name, content, mode = 0o644) => {
      const file = path.join(dir, name);
      fs.writeFileSync(file, content, { mode });
      return file;
    },
    read: (file, actionId = 'act-1') =>
      handlers.get('actionFile:read')({}, { actionId, path: file }),
    openInApp: (file, actionId = 'act-1') =>
      handlers.get('actionFile:openInApp')({}, { actionId, path: file }),
    fetch: (file, actionId = 'act-1') => protocol({ url: buildActionFileUrl(actionId, file) }),
  };
}

test('a file the final answer links reads as text; one it does not name is not found', async () => {
  const dir = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), 'pantaray-linked-')));
  const quote = path.join(dir, '見積書 v3.md');
  const other = path.join(dir, 'other.md');
  fs.writeFileSync(quote, '# 御見積書\n');
  fs.writeFileSync(other, 'not named');
  const app = harness({ linked: [quote] });

  assert.deepEqual(await app.read(quote), { kind: 'text', text: '# 御見積書\n', truncated: false });
  assert.deepEqual(await app.read(other), NOT_FOUND);
  assert.deepEqual(await app.read(quote, 'act-2').catch(() => 'rejected'), 'rejected');
  assert.deepEqual(await app.openInApp(other), NOT_FOUND);
  assert.deepEqual(app.opened, []);
});

test('an older answer link names a file; a patch step subject alone does not', async () => {
  const probe = harness();
  const notes = probe.file('notes.txt', 'notes');
  const patched = probe.file('patched.txt', 'patched');
  const app = harness({ olderLinked: [notes], patched: [patched] });

  assert.deepEqual(await app.read(notes), { kind: 'text', text: 'notes', truncated: false });
  assert.deepEqual(
    app.reads.map((request) => request.cursor),
    [null, 'older']
  );
  // A subject is display text, not a path: it never authorizes a file.
  assert.deepEqual(await app.read(patched), NOT_FOUND);
  assert.deepEqual(await app.openInApp(patched), NOT_FOUND);
  assert.equal((await app.fetch(patched)).status, 404);
});

test('a linked path is matched exactly, whitespace included', async () => {
  const probe = harness();
  const spaced = probe.file('見積書  v3.md', 'two spaces');
  const sibling = probe.file('見積書 v3.md', 'one space');
  // The patch step's subject collapses the run of spaces, so it spells the sibling.
  const app = harness({ linked: [spaced], patched: [sibling] });

  assert.deepEqual(await app.read(spaced), { kind: 'text', text: 'two spaces', truncated: false });
  assert.deepEqual(await app.read(sibling), NOT_FOUND);
});

test('text is cut at the cap on a character boundary and says so; binary is refused', async () => {
  const probe = harness();
  const long = probe.file('long.log', 'あ'.repeat(ACTION_FILE_TEXT_MAX_BYTES));
  const binary = probe.file('data.bin', Buffer.from([0x00, 0xff, 0xfe, 0x82]));
  const app = harness({ linked: [long, binary] });

  const result = await app.read(long);
  assert.equal(result.kind, 'text');
  assert.equal(result.truncated, true);
  assert.equal(result.text, 'あ'.repeat(Math.floor(ACTION_FILE_TEXT_MAX_BYTES / 3)));
  assert.deepEqual(await app.read(binary), { kind: 'unavailable', reason: 'binary' });
});

test('an image comes back as bytes typed by its content, within the size cap', async () => {
  const probe = harness();
  const chart = probe.file('chart.png', PNG);
  const big = probe.file(
    'big.png',
    Buffer.concat([PNG, Buffer.alloc(ACTION_FILE_IMAGE_MAX_BYTES)])
  );
  const app = harness({ linked: [chart, big] });

  const image = await app.read(chart);
  assert.equal(image.kind, 'image');
  assert.equal(image.mime, 'image/png');
  assert.deepEqual(Buffer.from(image.bytes), PNG);
  assert.deepEqual(await app.read(big), { kind: 'unavailable', reason: 'too_large' });
});

test('a relative path or a missing Action id is a validation error', async () => {
  const app = harness();
  await assert.rejects(app.read('report.md'), IpcValidationError);
  await assert.rejects(app.openInApp('report.md'), IpcValidationError);
  await assert.rejects(app.read('/tmp/report.md', ''), IpcValidationError);
});

test('open in app opens a named document by its real path', async () => {
  const probe = harness();
  const files = ['見積書.html', 'report.pdf', 'sheet.xlsx', 'notes'].map((name) =>
    probe.file(name, 'x')
  );
  const app = harness({ linked: files });
  for (const file of files) {
    assert.deepEqual(await app.openInApp(file), { kind: 'opened' });
    assert.equal(app.opened.at(-1), file);
  }
});

test('open in app refuses executables and app bundles, however they are named', async () => {
  const probe = harness();
  const scripts = ['run.command', 'run.sh', 'setup.tool', 'shell.terminal', 'tool.jar'].map(
    (name) => probe.file(name, 'x')
  );
  const executable = probe.file('report.md', '#!/bin/sh\n', 0o755);
  const disguised = path.join(probe.dir, 'notes.pdf');
  fs.symlinkSync(probe.file('payload.command', 'x'), disguised);
  const bundle = path.join(probe.dir, 'Viewer');
  fs.mkdirSync(path.join(bundle, 'Contents'), { recursive: true });
  fs.writeFileSync(path.join(bundle, 'Contents', 'Info.plist'), '<plist/>');
  const app = harness({ linked: [...scripts, executable, disguised, bundle] });

  for (const file of [...scripts, executable, disguised, bundle]) {
    assert.deepEqual(
      await app.openInApp(file),
      { kind: 'unavailable', reason: 'executable' },
      file
    );
  }
  assert.deepEqual(app.opened, []);
});

test('open in app reports a failure the OS returned', async () => {
  const probe = harness();
  const file = probe.file('a.md', 'x');
  const app = harness({ linked: [file], openPathResult: 'No application knows how to open this' });
  assert.deepEqual(await app.openInApp(file), { kind: 'unavailable', reason: 'open_failed' });
});

test('the protocol streams a named PDF and nothing else', async () => {
  const probe = harness();
  const pdf = probe.file('summary.pdf', '%PDF-1.7\nbody');
  const page = probe.file('page.pdf', '<script>alert(1)</script>');
  const unnamed = probe.file('other.pdf', '%PDF-1.7\nother');
  const app = harness({ linked: [pdf, page] });

  const response = await app.fetch(pdf);
  assert.equal(response.status, 200);
  assert.equal(response.headers.get('content-type'), 'application/pdf');
  assert.equal(response.headers.get('x-content-type-options'), 'nosniff');
  assert.equal(await response.text(), '%PDF-1.7\nbody');

  assert.equal((await app.fetch(page)).status, 404);
  assert.equal((await app.fetch(unnamed)).status, 404);
  assert.equal((await app.fetch(pdf, 'act-2')).status, 404);
  assert.equal((await app.fetch(pdf.replace('summary', 'missing'))).status, 404);
  assert.equal(
    (
      await createActionFileProtocolHandler({ readConversationPage: async () => {} })({
        url: 'pantaray-action-file://other/?action=act-1&path=/etc/hosts',
      })
    ).status,
    404
  );
});

test('a path is authorized by the file it reaches, not by its spelling', async () => {
  const probe = harness();
  const work = path.join(probe.dir, 'work');
  const secrets = path.join(probe.dir, 'secrets');
  fs.mkdirSync(path.join(secrets, 'subdir'), { recursive: true });
  fs.mkdirSync(work);
  fs.writeFileSync(path.join(work, 'report.txt'), 'public');
  fs.writeFileSync(path.join(secrets, 'report.txt'), '%PDF-secret');
  fs.symlinkSync(path.join(secrets, 'subdir'), path.join(work, 'link'));
  const app = harness({ linked: [path.join(work, 'report.txt')] });

  // Normalized, this spells the named work/report.txt; opened, it reaches secrets/report.txt.
  const escape = `${work}/link/../report.txt`;
  assert.deepEqual(await app.read(escape), NOT_FOUND);
  assert.deepEqual(await app.openInApp(escape), NOT_FOUND);
  assert.equal((await app.fetch(escape)).status, 404);
  assert.deepEqual(app.opened, []);
});

test('a named path that is a symlink serves the file it points to', async () => {
  const probe = harness();
  const target = probe.file('quote_v3.md', '# v3');
  const latest = path.join(probe.dir, 'latest.md');
  fs.symlinkSync(target, latest);
  const app = harness({ linked: [latest] });

  assert.deepEqual(await app.read(latest), { kind: 'text', text: '# v3', truncated: false });
  assert.deepEqual(await app.openInApp(latest), { kind: 'opened' });
  assert.deepEqual(app.opened, [target]);
});

test('a FIFO with no writer is refused without blocking the main process', () => {
  const { execFileSync, spawnSync } = require('child_process');
  const probe = harness();
  const fifo = path.join(probe.dir, 'pipe.txt');
  execFileSync('mkfifo', [fifo]);
  // A blocking open() would hang the event loop itself, so the read runs in a child process
  // that is killed if it does not finish in time.
  const script = `
    const access = require(${JSON.stringify(require.resolve('../electron/dist/actions/actionFileAccess.js'))});
    process.stdout.write(JSON.stringify([
      access.readActionFile(${JSON.stringify(fifo)}),
      access.openRegularFile(${JSON.stringify(fifo)}),
    ]));
  `;
  const child = spawnSync(process.execPath, ['-e', script], { timeout: 5000, encoding: 'utf8' });
  assert.equal(child.signal, null, 'reading a FIFO blocked');
  assert.deepEqual(JSON.parse(child.stdout), [NOT_FOUND, null]);
});

test('open in app refuses a FIFO', async () => {
  const { execFileSync } = require('child_process');
  const probe = harness();
  const fifo = path.join(probe.dir, 'pipe.pdf');
  execFileSync('mkfifo', [fifo]);
  const app = harness({ linked: [fifo] });
  assert.deepEqual(await app.openInApp(fifo), NOT_FOUND);
  assert.equal((await app.fetch(fifo)).status, 404);
  assert.deepEqual(app.opened, []);
});
