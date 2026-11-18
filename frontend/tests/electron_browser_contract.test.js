// The browser wire contract. The backend's contract test reads the same fixture, so a
// change to one side that the other does not share fails one of the two suites.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { test } = require('node:test');

const {
  BrowserOperationAnswerSchema,
  parseBrowserOperationRequest,
  parseBrowserSessionCloseRequest,
} = require('../electron/dist/browser/browserContract.js');
const { OutboundEvent } = require('../shared/events');

const CASES = JSON.parse(
  fs.readFileSync(path.join(__dirname, 'fixtures/browser_contract_cases.json'), 'utf8')
);

const PARSERS = {
  operation_requested: parseBrowserOperationRequest,
  session_close_requested: parseBrowserSessionCloseRequest,
  operation_answer: (payload) => {
    if (!BrowserOperationAnswerSchema.safeParse(payload).success) throw new Error('invalid');
  },
};

test('event names match the fixture', () => {
  assert.deepEqual(CASES.events, {
    operation_requested: OutboundEvent.BROWSER_OPERATION_REQUESTED,
    session_close_requested: OutboundEvent.BROWSER_SESSION_CLOSE_REQUESTED,
  });
});

for (const [section, parse] of Object.entries(PARSERS)) {
  for (const { name, data } of CASES[section].valid) {
    test(`${section} accepts: ${name}`, () => {
      assert.doesNotThrow(() => parse(data));
    });
  }
  for (const { name, data } of CASES[section].invalid) {
    test(`${section} refuses: ${name}`, () => {
      assert.throws(() => parse(data));
    });
  }
}
