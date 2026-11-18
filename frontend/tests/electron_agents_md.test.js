const assert = require('assert');
const { execFileSync } = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');
const { test } = require('node:test');

const { AGENTS_MD_MAX_BYTES } = require('../electron/dist/ipc/schemas/agentsMd.js');
const { createAgentsMdFile } = require('../electron/dist/settings/agentsMd.js');

function tempHome(t) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'pantaray-agents-md-'));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  return dir;
}

const agentsMdPath = (home) => path.join(home, '.pantaray', 'AGENTS.md');

function writeOnDisk(home, data) {
  fs.mkdirSync(path.dirname(agentsMdPath(home)), { recursive: true });
  fs.writeFileSync(agentsMdPath(home), data);
}

test('a missing file reads as no instructions', (t) => {
  assert.deepEqual(createAgentsMdFile(tempHome(t)).read(), {
    kind: 'loaded',
    content: '',
    revision: null,
  });
});

test('a save creates ~/.pantaray, replaces the file, and the next save starts from it', (t) => {
  const home = tempHome(t);
  const file = createAgentsMdFile(home);
  const first = file.write({ content: '結論から短く答える。\n', expectedRevision: null });
  assert.equal(first.kind, 'saved');
  assert.equal(fs.readFileSync(agentsMdPath(home), 'utf8'), '結論から短く答える。\n');
  assert.deepEqual(file.read(), {
    kind: 'loaded',
    content: '結論から短く答える。\n',
    revision: first.revision,
  });
  assert.equal(file.write({ content: '', expectedRevision: first.revision }).kind, 'saved');
  assert.equal(file.read().content, '');
  assert.deepEqual(fs.readdirSync(path.join(home, '.pantaray')), ['AGENTS.md']);
});

test('a save over a file that changed, appeared, or went away since it was read writes nothing', (t) => {
  const home = tempHome(t);
  const file = createAgentsMdFile(home);

  // Created after a read that found nothing.
  const absent = file.read().revision;
  writeOnDisk(home, 'from an editor');
  assert.deepEqual(file.write({ content: 'mine', expectedRevision: absent }), { kind: 'conflict' });
  assert.equal(fs.readFileSync(agentsMdPath(home), 'utf8'), 'from an editor');

  // Changed after the read, with the same size, as an atomic replace by another writer.
  const read = file.read().revision;
  writeOnDisk(home, 'from an EDITOR');
  assert.deepEqual(file.write({ content: 'mine', expectedRevision: read }), { kind: 'conflict' });
  assert.equal(fs.readFileSync(agentsMdPath(home), 'utf8'), 'from an EDITOR');

  // Removed after the read.
  const beforeRemoval = file.read().revision;
  fs.rmSync(agentsMdPath(home));
  assert.deepEqual(file.write({ content: 'mine', expectedRevision: beforeRemoval }), {
    kind: 'conflict',
  });
  assert.equal(fs.existsSync(agentsMdPath(home)), false);
});

test('a file over the limit is not offered for editing and is not saved over', (t) => {
  const home = tempHome(t);
  const file = createAgentsMdFile(home);
  writeOnDisk(home, 'a'.repeat(AGENTS_MD_MAX_BYTES));
  assert.equal(file.read().kind, 'loaded');
  writeOnDisk(home, 'a'.repeat(AGENTS_MD_MAX_BYTES + 1));
  assert.deepEqual(file.read(), { kind: 'too_large' });
  assert.deepEqual(file.write({ content: 'short', expectedRevision: null }), { kind: 'conflict' });
  assert.equal(fs.statSync(agentsMdPath(home)).size, AGENTS_MD_MAX_BYTES + 1);
});

test('invalid UTF-8 reads with replacement characters, as the Action decodes it', (t) => {
  const home = tempHome(t);
  writeOnDisk(home, Buffer.from([0x61, 0xff, 0x62]));
  assert.equal(createAgentsMdFile(home).read().content, 'a�b');
});

test('a FIFO under the name reads as no instructions instead of blocking main', (t) => {
  const home = tempHome(t);
  fs.mkdirSync(path.dirname(agentsMdPath(home)));
  execFileSync('mkfifo', [agentsMdPath(home)]);
  assert.deepEqual(createAgentsMdFile(home).read(), {
    kind: 'loaded',
    content: '',
    revision: null,
  });
});
