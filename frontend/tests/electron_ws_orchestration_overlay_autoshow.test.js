const assert = require('assert');
const { test } = require('node:test');

// テスト中のログノイズを抑える（TAP出力を汚さない）
process.env.PANTARAY_LOG_LEVEL = 'silent';
process.env.PANTARAY_LOG_SALT = 'test-log-salt';
process.env.FRONTEND_PORT = '3001';

const { OutboundEvent } = require('../shared/events');
const { createOrchestrationWS } = require('../electron/ws_orchestration');

/**
 * node:test 用の簡易タイマー実装を生成する。
 * - ws_orchestration.js は setTimeout / setInterval を DI できるため、テストでは副作用を抑える。
 */
function createFakeTimers() {
  /** @type {Array<{ id: number, ms: number, fn: Function }>} */
  const timeouts = [];
  /** @type {Array<{ id: number, ms: number, fn: Function }>} */
  const intervals = [];
  let nextId = 1;
  return {
    timeouts,
    intervals,
    setTimeout: (fn, ms) => {
      const id = nextId++;
      timeouts.push({ id, ms: Number(ms) || 0, fn });
      return id;
    },
    clearTimeout: (id) => {
      const idx = timeouts.findIndex((t) => t.id === id);
      if (idx >= 0) timeouts.splice(idx, 1);
    },
    setInterval: (fn, ms) => {
      const id = nextId++;
      intervals.push({ id, ms: Number(ms) || 0, fn });
      return id;
    },
    clearInterval: (id) => {
      const idx = intervals.findIndex((t) => t.id === id);
      if (idx >= 0) intervals.splice(idx, 1);
    },
  };
}

/**
 * ws (WebSocket) の最小限モックを作る。
 * - connect() でインスタンスを生成し、emit('message', ...) で受信イベントを注入できる。
 */
function createFakeWebSocketClass({ withPing = true } = {}) {
  /** @type {any[]} */
  const instances = [];

  class FakeWebSocket {
    static OPEN = 1;
    static CONNECTING = 0;
    static CLOSED = 3;

    constructor(url, options) {
      this.url = url;
      this.options = options || {};
      this.readyState = FakeWebSocket.CONNECTING;
      this.sent = [];
      this.pings = [];
      this.closeCalls = [];
      this._handlers = new Map();
      instances.push(this);
    }

    on(event, handler) {
      const list = this._handlers.get(event) || [];
      list.push(handler);
      this._handlers.set(event, list);
    }

    emit(event, ...args) {
      const list = this._handlers.get(event) || [];
      for (const fn of list) fn(...args);
    }

    send(data) {
      this.sent.push(String(data));
    }

    ping(data) {
      if (!withPing) {
        throw new Error('ping is disabled for this fake');
      }
      this.pings.push(data);
    }

    close(code, reason) {
      this.closeCalls.push({ code, reason });
      this.readyState = FakeWebSocket.CLOSED;
    }
  }

  return { FakeWebSocket, instances };
}

test('WS: suggestion_chunk(非空) の最初の1回だけ showNotification を呼ぶ（suggestion_id単位）', () => {
  const timers = createFakeTimers();
  const { FakeWebSocket, instances } = createFakeWebSocketClass({ withPing: false });

  /** @type {string[]} */
  const calls = [];

  const client = createOrchestrationWS({
    forwardEventToRenderers: () => {},
    forwardStatusToRenderers: () => {},
    showNotification: (id) => {
      calls.push(id);
    },
    WebSocketImpl: FakeWebSocket,
    timers,
    now: () => 1234,
  });

  client.connect('ws://example.test/ws', { Authorization: 'Bearer local-api-token' });
  assert.equal(instances.length, 1);
  const ws = instances[0];
  ws.readyState = FakeWebSocket.OPEN;
  ws.emit('open');

  // 1st chunk: suggestion_id のオーバーレイを開く
  ws.emit(
    'message',
    JSON.stringify({
      event: OutboundEvent.SUGGESTION_CHUNK,
      event_id: 'E1',
      meta: { process_id: 'P1', suggestion_id: 'Sug1', kind: 'suggestion' },
      data: { content: 'hello' },
    })
  );
  assert.deepEqual(calls, ['Sug1']);

  // 2nd chunk (same suggestion): 追加で呼ばれない
  ws.emit(
    'message',
    JSON.stringify({
      event: OutboundEvent.SUGGESTION_CHUNK,
      event_id: 'E2',
      meta: { process_id: 'P1', suggestion_id: 'Sug1', kind: 'suggestion' },
      data: { content: 'world' },
    })
  );
  assert.equal(calls.length, 1);

  // Different suggestion_id: 別オーバーレイとして呼ばれる
  ws.emit(
    'message',
    JSON.stringify({
      event: OutboundEvent.SUGGESTION_CHUNK,
      event_id: 'E3',
      meta: { process_id: 'P2', suggestion_id: 'Sug2', kind: 'suggestion' },
      data: { content: 'x' },
    })
  );
  assert.equal(calls.length, 2);
  assert.equal(calls[1], 'Sug2');
});

test('WS: suggestion_chunk が空/空白のみなら showNotification を呼ばない', () => {
  const timers = createFakeTimers();
  const { FakeWebSocket, instances } = createFakeWebSocketClass({ withPing: false });

  /** @type {string[]} */
  const calls = [];

  const client = createOrchestrationWS({
    forwardEventToRenderers: () => {},
    forwardStatusToRenderers: () => {},
    showNotification: (id) => {
      calls.push(id);
    },
    WebSocketImpl: FakeWebSocket,
    timers,
    now: () => 1234,
  });

  client.connect('ws://example.test/ws', { Authorization: 'Bearer local-api-token' });
  const ws = instances[0];
  ws.readyState = FakeWebSocket.OPEN;
  ws.emit('open');

  ws.emit(
    'message',
    JSON.stringify({
      event: OutboundEvent.SUGGESTION_CHUNK,
      event_id: 'E1',
      meta: { process_id: 'P1', suggestion_id: 'Sug1' },
      data: { content: '   ' },
    })
  );

  assert.equal(calls.length, 0);
});

test('WS: showNotification が例外なら、次の suggestion_chunk で再試行できる（フラグを戻す）', () => {
  const timers = createFakeTimers();
  const { FakeWebSocket, instances } = createFakeWebSocketClass({ withPing: false });

  /** @type {string[]} */
  const calls = [];
  let shouldThrow = true;

  const client = createOrchestrationWS({
    forwardEventToRenderers: () => {},
    forwardStatusToRenderers: () => {},
    showNotification: (id) => {
      if (shouldThrow) {
        shouldThrow = false;
        throw new Error('boom');
      }
      calls.push(id);
    },
    WebSocketImpl: FakeWebSocket,
    timers,
    now: () => 1234,
  });

  client.connect('ws://example.test/ws', { Authorization: 'Bearer local-api-token' });
  const ws = instances[0];
  ws.readyState = FakeWebSocket.OPEN;
  ws.emit('open');

  // 1回目: 例外で失敗（callsには入らない）
  ws.emit(
    'message',
    JSON.stringify({
      event: OutboundEvent.SUGGESTION_CHUNK,
      event_id: 'E1',
      meta: { process_id: 'P1', suggestion_id: 'Sug1' },
      data: { content: 'hello' },
    })
  );
  assert.equal(calls.length, 0);

  // 2回目: 同一 suggestion_id でも再試行される
  ws.emit(
    'message',
    JSON.stringify({
      event: OutboundEvent.SUGGESTION_CHUNK,
      event_id: 'E2',
      meta: { process_id: 'P1', suggestion_id: 'Sug1' },
      data: { content: 'world' },
    })
  );
  assert.equal(calls.length, 1);
  assert.equal(calls[0], 'Sug1');
});
