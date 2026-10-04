import { afterEach, describe, expect, it, vi } from 'vitest';
import { saveConversationScrollPosition } from './conversationScrollPosition';
import type { ActionConversationPage } from '../../../electron/src/actions/actionContracts';
import { projectActionConversationView } from '../../../electron/src/actions/actionConversationModel';
import {
  createConversationPaging,
  EMPTY_CONVERSATION_PAGING,
  readWholeConversation,
  type ConversationPagingState,
} from './conversationPaging';

function page(
  id: string,
  cursor: string | null = null,
  actionId = 'action'
): ActionConversationPage {
  return {
    action: {
      action_id: actionId,
      suggestion_id: null,
      latest_run_id: id,
      status: 'success',
      approved_suggestion: null,
      resumable: false,
    },
    runs: [
      {
        run_id: id,
        status: 'success',
        started_at: '2026-09-09T00:00:00.000000Z',
        completed_at: '2026-09-09T00:01:00.000000Z',
        completion_event_id: `event-${id}`,
        entries: [],
        final_output: `answer ${id}`,
        error: null,
      },
    ],
    unadopted_messages: [],
    next_cursor: cursor,
  };
}
/** Every run's answer in display order, joined the way the pages used to read. */
function answers(pages: ConversationPagingState['pages']): string {
  return projectActionConversationView(pages)
    .items.flatMap((item) =>
      item.kind === 'run'
        ? item.lines.flatMap((line) => (line.kind === 'final_output' ? [line.text] : []))
        : []
    )
    .join('\n\n');
}
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}
function setup() {
  const readConversationPage =
    vi.fn<NonNullable<NonNullable<typeof window.electron>['actions']>['readConversationPage']>();
  let state: ConversationPagingState = EMPTY_CONVERSATION_PAGING;
  const publications: ConversationPagingState[] = [];
  const controller = createConversationPaging({ readConversationPage }, (next) => {
    state = next;
    publications.push(next);
  });
  return {
    controller,
    readConversationPage,
    publications,
    state: () => state,
    view: () => projectActionConversationView(state.pages),
    answers: () => answers(state.pages),
  };
}

describe('conversation paging', () => {
  afterEach(() => localStorage.clear());

  it('does not fetch saved older pages when reopening at the bottom', async () => {
    saveConversationScrollPosition('action', { top: 5000, atBottom: true, pageCount: 30 });
    const s = setup();
    s.readConversationPage.mockImplementation(async ({ cursor }) => {
      const index = Number(cursor!.slice(1));
      return page(`past-${index}`, index < 29 ? `c${index + 1}` : null);
    });
    s.controller.update(page('latest', 'c1'));
    await vi.waitFor(() => expect(s.state().olderPageState).toBe('idle'));
    expect(s.readConversationPage.mock.calls.map(([request]) => request.cursor)).toEqual([
      'c1',
      'c2',
    ]);
    expect(s.view().nextCursor).toBe('c3');
  });

  it('retains previously loaded pending messages beyond two pages while their run stays active', async () => {
    const s = setup();
    const active = page('run', 'cursor-1');
    active.action.status = 'processing';
    Object.assign(active.runs[0], {
      status: 'running',
      completed_at: null,
      completion_event_id: null,
      final_output: null,
    });
    const pending = (id: number): ActionConversationPage => ({
      ...active,
      runs: [],
      next_cursor: id === 3 ? null : `cursor-${id + 1}`,
      unadopted_messages: [
        {
          step_kind: 'user',
          approved_suggestion: null,
          step_id: `step-${id}`,
          step_number: null,
          message_id: `message-${id}`,
          accepted_sequence: id,
          content: `pending ${id}`,
          images: [],
          project_refs: [],
          status: 'pending',
        },
      ],
    });
    s.readConversationPage.mockResolvedValueOnce(pending(1)).mockResolvedValueOnce(pending(2));
    s.controller.update(active);
    await vi.waitFor(() => expect(s.state().olderPageState).toBe('idle'));
    s.readConversationPage.mockResolvedValueOnce(pending(3));
    await s.controller.loadOlder();
    s.readConversationPage
      .mockResolvedValueOnce(pending(1))
      .mockResolvedValueOnce(pending(2))
      .mockResolvedValueOnce(pending(3));
    s.controller.update(active);
    await vi.waitFor(() => expect(s.state().olderPageState).toBe('idle'));
    expect(
      s.state().pages?.flatMap((page) => page.unadopted_messages.map((entry) => entry.message_id))
    ).toEqual(['message-1', 'message-2', 'message-3']);
  });

  it('refreshes a cached active run to its terminal answer and advances using fresh cursors', async () => {
    const s = setup();
    const active = page('one', 'old-past');
    active.action.status = 'processing';
    Object.assign(active.runs[0], {
      status: 'running',
      completed_at: null,
      completion_event_id: null,
      final_output: null,
    });
    s.readConversationPage
      .mockResolvedValueOnce(page('zero', 'old-tail'))
      .mockResolvedValueOnce(page('older', 'old-unseen'));
    s.controller.update(active);
    await vi.waitFor(() => expect(s.state().olderPageState).toBe('idle'));
    s.readConversationPage.mockClear();
    s.readConversationPage
      .mockResolvedValueOnce(page('one', 'fresh-zero'))
      .mockResolvedValueOnce(page('zero', 'fresh-older'))
      .mockResolvedValueOnce(page('older', 'fresh-unseen'));
    s.controller.update(page('two', 'fresh-past'));
    await vi.waitFor(() => expect(s.state().olderPageState).toBe('idle'));
    expect(s.answers()).toContain('answer one');
    s.readConversationPage.mockResolvedValueOnce(page('ancient'));
    await s.controller.loadOlder();
    expect(s.readConversationPage.mock.calls.map(([request]) => request.cursor)).toEqual([
      'fresh-past',
      'fresh-zero',
      'fresh-older',
      'fresh-unseen',
    ]);
  });

  it('keeps previous answers throughout follow-up refresh and preserves the newest live head', async () => {
    const s = setup();
    s.controller.update(page('one'));
    const pending = deferred<ActionConversationPage>();
    s.readConversationPage.mockReturnValueOnce(pending.promise).mockResolvedValue(page('one'));
    s.controller.update(page('two', 'past'));
    expect(s.answers()).toBe('answer one\n\nanswer two');
    expect(s.view().nextCursor).toBeNull();
    const live = page('two', 'past');
    live.runs[0].final_output = 'updated answer two';
    s.controller.update(live);
    pending.resolve(page('one'));
    await vi.waitFor(() => expect(s.state().olderPageState).toBe('idle'));
    expect(s.answers()).toBe('answer one\n\nupdated answer two');
    expect(s.publications.every((state) => answers(state.pages).includes('answer one'))).toBe(true);
  });

  it('coalesces a moving cursor and recovers a stale in-flight read without dropping the loaded past', async () => {
    const s = setup();
    s.controller.update(page('one'));
    const pending = deferred<{ kind: 'stale_cursor' }>();
    s.readConversationPage.mockReturnValueOnce(pending.promise).mockResolvedValueOnce(page('one'));
    s.controller.update(page('two', 'old-boundary'));
    s.controller.update(page('two', 'new-boundary'));
    pending.resolve({ kind: 'stale_cursor' });
    await vi.waitFor(() => expect(s.state().olderPageState).toBe('idle'));
    expect(s.answers()).toBe('answer one\n\nanswer two');
    expect(s.readConversationPage.mock.calls.map(([request]) => request.cursor)).toEqual([
      'old-boundary',
      'new-boundary',
    ]);
  });

  it('does not let a previous account or conversation read repopulate reset state', async () => {
    const s = setup();
    const pending = deferred<ActionConversationPage>();
    s.readConversationPage.mockReturnValueOnce(pending.promise);
    s.controller.update(page('one', 'past'));
    s.controller.reset();
    s.controller.update(page('new', null, 'another-action'));
    pending.resolve(page('old-private-answer'));
    await pending.promise;
    await Promise.resolve();
    expect(s.answers()).toBe('answer new');
    expect(s.view().action?.action_id).toBe('another-action');
  });

  it('refreshes through all previously opened pages, beyond the initial two-page allowance', async () => {
    const s = setup();
    s.readConversationPage
      .mockResolvedValueOnce(page('three', 'c3'))
      .mockResolvedValueOnce(page('two', 'c2'));
    s.controller.update(page('four', 'c4'));
    await vi.waitFor(() => expect(s.state().olderPageState).toBe('idle'));
    s.readConversationPage.mockResolvedValueOnce(page('one'));
    await s.controller.loadOlder();
    s.readConversationPage
      .mockResolvedValueOnce(page('four', 'c4-new'))
      .mockResolvedValueOnce(page('three', 'c3-new'))
      .mockResolvedValueOnce(page('two', 'c2-new'))
      .mockResolvedValueOnce(page('one'));
    s.controller.update(page('five', 'c5'));
    await vi.waitFor(() => expect(s.state().olderPageState).toBe('idle'));
    expect(s.answers()).toBe(
      'answer one\n\nanswer two\n\nanswer three\n\nanswer four\n\nanswer five'
    );
    expect(s.view().nextCursor).toBeNull();
  });

  it('retains visible history on transport failure and refreshes a fresh cursor on explicit retry', async () => {
    const s = setup();
    s.controller.update(page('one'));
    s.readConversationPage.mockRejectedValueOnce(new Error('connection lost'));
    s.controller.update(page('two', 'past'));
    await vi.waitFor(() => expect(s.state().olderPageState).toBe('failed'));
    expect(s.answers()).toBe('answer one\n\nanswer two');
    s.readConversationPage
      .mockResolvedValueOnce(page('two', 'fresh-past'))
      .mockResolvedValueOnce(page('one'));
    await s.controller.loadOlder();
    expect(s.state().olderPageState).toBe('idle');
    expect(s.view().nextCursor).toBeNull();
    expect(s.answers()).toBe('answer one\n\nanswer two');
  });
  it('refreshes pending messages even when the latest page and cursor are unchanged', async () => {
    const s = setup();
    const head = page('two', 'same-cursor');
    s.readConversationPage.mockResolvedValueOnce(page('one'));
    s.controller.update(head);
    await vi.waitFor(() => expect(s.state().olderPageState).toBe('idle'));
    const pending = page('two', 'older-cursor');
    pending.runs = [];
    pending.unadopted_messages = [
      {
        step_kind: 'user',
        approved_suggestion: null,
        step_id: 'step-pending',
        step_number: null,
        message_id: 'message-pending',
        accepted_sequence: 3,
        content: 'Sent while running',
        images: [],
        project_refs: [],
        status: 'pending',
      },
    ];
    s.readConversationPage.mockResolvedValueOnce(pending).mockResolvedValueOnce(page('one'));
    s.controller.update(head);
    await vi.waitFor(() => expect(s.state().olderPageState).toBe('idle'));
    expect(
      s
        .view()
        .items.some(
          (item) =>
            item.kind === 'user' &&
            item.source === 'canonical' &&
            item.entry.message_id === 'message-pending'
        )
    ).toBe(true);
  });

  it('does not report a successful retry when the refreshed past still fails', async () => {
    const s = setup();
    s.controller.update(page('one'));
    s.readConversationPage.mockRejectedValueOnce(new Error('read failed'));
    s.controller.update(page('two', 'past'));
    await vi.waitFor(() => expect(s.state().olderPageState).toBe('failed'));
    s.readConversationPage
      .mockResolvedValueOnce(page('two', 'fresh-past'))
      .mockRejectedValueOnce(new Error('still unavailable'));
    expect(await s.controller.loadOlder()).toBeNull();
    expect(s.state().olderPageState).toBe('failed');
    expect(s.answers()).toBe('answer one\n\nanswer two');
  });
});

describe('readWholeConversation', () => {
  it('reads from the newest page to the first, following each cursor', async () => {
    const { readConversationPage } = setup();
    readConversationPage
      .mockResolvedValueOnce(page('three', 'c2'))
      .mockResolvedValueOnce(page('two', 'c1'))
      .mockResolvedValueOnce(page('one'));
    const chain = await readWholeConversation({ readConversationPage }, 'action');
    expect(readConversationPage.mock.calls.map(([request]) => request.cursor)).toEqual([
      null,
      'c2',
      'c1',
    ]);
    expect(answers(chain)).toBe('answer one\n\nanswer two\n\nanswer three');
  });

  it('returns nothing when a page in the middle cannot be read', async () => {
    const { readConversationPage } = setup();
    readConversationPage
      .mockResolvedValueOnce(page('three', 'c2'))
      .mockResolvedValueOnce({ kind: 'stale_cursor' });
    expect(await readWholeConversation({ readConversationPage }, 'action')).toBeNull();
  });
});
