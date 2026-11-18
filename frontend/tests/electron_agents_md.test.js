const assert = require('assert');
const { execFileSync } = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');
const { test } = require('node:test');

const { createAgentsMdFile } = require('../electron/dist/settings/agentsMd.js');

function tempHome(t) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'pantaray-agents-md-'));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  return dir;
}

const agentsMdPath = (home) => path.join(home, '.pantaray', 'AGENTS.md');

test('a missing file reads as no instructions', (t) => {
  assert.equal(createAgentsMdFile(tempHome(t)).read(), '');
});

test('a save creates ~/.pantaray, replaces the file, and reads back', (t) => {
  const home = tempHome(t);
  const file = createAgentsMdFile(home);
  file.write('結論から短く答える。\n');
  assert.equal(fs.readFileSync(agentsMdPath(home), 'utf8'), '結論から短く答える。\n');
  file.write('');
  assert.equal(file.read(), '');
  assert.deepEqual(fs.readdirSync(path.join(home, '.pantaray')), ['AGENTS.md']);
});

test('invalid UTF-8 reads with replacement characters, as the Action decodes it', (t) => {
  const home = tempHome(t);
  fs.mkdirSync(path.dirname(agentsMdPath(home)));
  fs.writeFileSync(agentsMdPath(home), Buffer.from([0x61, 0xff, 0x62]));
  assert.equal(createAgentsMdFile(home).read(), 'a�b');
});

test('a FIFO under the name reads as no instructions instead of blocking main', (t) => {
  const home = tempHome(t);
  fs.mkdirSync(path.dirname(agentsMdPath(home)));
  execFileSync('mkfifo', [agentsMdPath(home)]);
  assert.equal(createAgentsMdFile(home).read(), '');
});
