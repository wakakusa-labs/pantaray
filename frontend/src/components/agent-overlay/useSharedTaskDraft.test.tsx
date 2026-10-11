import { act, cleanup, renderHook, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { ActionMessageSubmitResult } from '../../../electron/src/actions/actionContracts';
import { createTaskDraftBridge } from '@/tests/taskDraftBridge';
import { useOverlayComposerController } from './useOverlayComposerController';

type Actions = NonNullable<NonNullable<Window['electron']>['actions']>;

const PDF = new File(['%PDF-1.4\n'], 'quote.pdf', { type: 'application/pdf' });
const ATTACHMENT_ID = '00000000-0000-4000-8000-000000000001';

let bridge = createTaskDraftBridge();
const submitMessage = vi.fn<Actions['submitMessage']>();

// One window's composer for a task, as the small window and the main window's pane each have.
// A rerender with another suggestion shows the next one in the same composer, as the small
// window does.
function composerIn(
  window: object,
  task: { actionId?: string; suggestionId?: string } = { actionId: 'act-1' }
) {
  const actions = {
    ...window,
    submitMessage: bridge.sending(submitMessage),
    attachFile: vi.fn(async ({ name }: { name: string }) => ({
      attachmentId: ATTACHMENT_ID,
      name,
      byteSize: 9,
    })),
  } as unknown as Actions;
  return renderHook(
    ({ suggestionId }) =>
      useOverlayComposerController({
        actions,
        initialActionId: task.actionId ?? null,
        suggestionId,
        suggestionAccepted: false,
        language: 'en',
        onRefreshedPage: () => undefined,
      }),
    { initialProps: { suggestionId: task.suggestionId ?? null } }
  );
}

// A window whose draft changes are recorded, to tell what it heard.
function recordingWindow() {
  const window = bridge.window();
  const heard: unknown[] = [];
  const onDraftChanged: typeof window.onDraftChanged = (listener) =>
    window.onDraftChanged((change) => {
      heard.push(change);
      listener(change);
    });
  return { window: { ...window, onDraftChanged }, heard };
}

const opened = async (composer: ReturnType<typeof composerIn>) => {
  // The draft is read when the composer mounts; until then nothing is shared.
  await act(async () => {});
  return composer;
};

beforeEach(() => {
  bridge = createTaskDraftBridge();
  submitMessage.mockReset();
});

afterEach(() => cleanup());

describe('a task draft shared by its composers', () => {
  it('shows what is typed in one window in the other, and never echoes it back', async () => {
    const small = recordingWindow();
    const panel = await opened(composerIn(small.window));
    const main = await opened(composerIn(bridge.window()));

    act(() => panel.result.current.changeDraft('見積を直して', []));
    await waitFor(() => expect(main.result.current.composer.draft).toBe('見積を直して'));
    expect(small.heard).toEqual([]);

    act(() => main.result.current.changeDraft('見積を直して、合計も', []));
    await waitFor(() => expect(panel.result.current.composer.draft).toBe('見積を直して、合計も'));
  });

  it('shows an attachment everywhere, and a removal too, discarding the file only then', async () => {
    const panel = await opened(composerIn(bridge.window()));
    const main = await opened(composerIn(bridge.window()));

    await act(async () => panel.result.current.attachFiles([PDF]));
    await waitFor(() =>
      expect(main.result.current.composer.attachments).toEqual([
        { kind: 'file', attachmentId: ATTACHMENT_ID, name: 'quote.pdf', byteSize: 9 },
      ])
    );
    expect(bridge.discarded).toEqual([]);

    act(() => main.result.current.removeAttachment(main.result.current.composer.attachments[0]));
    await waitFor(() => expect(panel.result.current.composer.attachments).toEqual([]));
    await waitFor(() => expect(bridge.discarded).toEqual([ATTACHMENT_ID]));
  });

  it('shows quotes in both windows, and sends them before the words with mentions moved', async () => {
    const task = { suggestionId: 'sug-1' };
    const panel = await opened(composerIn(bridge.window(), task));
    const main = await opened(composerIn(bridge.window(), task));
    const aurora = {
      kind: 'project' as const,
      projectId: 'p-1',
      displayName: 'Aurora',
      paths: ['/a'],
      start: 4,
      end: 10,
    };

    act(() => panel.result.current.quoting.onAdd('合計が違う', '直して'));
    act(() => panel.result.current.quoting.onAdd('税込み', ''));
    act(() => panel.result.current.changeDraft('see Aurora', [aurora]));
    await waitFor(() =>
      expect(main.result.current.composer.quotes.map((quote) => quote.text)).toEqual([
        '合計が違う',
        '税込み',
      ])
    );
    expect(main.result.current.composer.draft).toBe('see Aurora');

    act(() => main.result.current.submitDraft(null, true, null, 0, 'prompt_each_time', 'sug-1'));
    const { message } = submitMessage.mock.calls[0][0];
    expect(message.content).toBe('> 合計が違う\n\n直して\n\n> 税込み\n\nsee Aurora');
    expect(message.project_refs).toEqual([
      { project_id: 'p-1', display_name: 'Aurora', paths: ['/a'], start: 25, end: 31 },
    ]);
    await waitFor(() => expect(panel.result.current.composer.quotes).toEqual([]));
  });

  it('opens on the draft another window left, as moving a task between windows does', async () => {
    const panel = await opened(composerIn(bridge.window()));
    act(() => panel.result.current.changeDraft('続きは明日', []));
    await act(async () => panel.unmount());

    const main = await opened(composerIn(bridge.window()));
    expect(main.result.current.composer.draft).toBe('続きは明日');
  });

  it('keeps tasks apart', async () => {
    const first = await opened(composerIn(bridge.window(), { actionId: 'act-1' }));
    const second = await opened(composerIn(bridge.window(), { actionId: 'act-2' }));

    act(() => first.result.current.changeDraft('only for act-1', []));
    await waitFor(() => expect(bridge.store.open('user-1', 'action:act-1', probe)).not.toBeNull());
    expect(second.result.current.composer.draft).toBe('');
    expect(bridge.store.open('user-1', 'action:act-2', probe)).toBeNull();
  });

  // The small window sends what both windows show; the send is out until `fail` is called.
  const sendFromPanel = async () => {
    const task = { suggestionId: 'sug-1' };
    const panel = await opened(composerIn(bridge.window(), task));
    const main = await opened(composerIn(bridge.window(), task));
    act(() => panel.result.current.changeDraft('Only the summary', []));
    await waitFor(() => expect(main.result.current.composer.draft).toBe('Only the summary'));
    let fail!: (error: Error) => void;
    submitMessage.mockReturnValueOnce(new Promise((_, reject) => (fail = reject)));
    act(() => panel.result.current.submitDraft(null, true, null, 0, 'prompt_each_time', 'sug-1'));
    // Main took the words as the send went out: no window offers them meanwhile.
    await waitFor(() => expect(main.result.current.composer.draft).toBe(''));
    return { panel, main, fail };
  };

  it('offers a send lost in one window for its exact retry in the other', async () => {
    const { panel, main, fail } = await sendFromPanel();
    await act(async () => fail(new Error('offline')));
    await waitFor(() => expect(main.result.current.composer.submission?.state).toBe('failed'));
    expect(main.result.current.composer.draft).toBe('Only the summary');

    submitMessage.mockResolvedValueOnce(submitted('act-9'));
    act(() => main.result.current.retrySubmission());
    expect(submitMessage.mock.calls[1][0]).toEqual(submitMessage.mock.calls[0][0]);
    // It went through: neither window offers it any more.
    await waitFor(() => expect(panel.result.current.composer.submission).toBeNull());
    expect(panel.result.current.composer.draft).toBe('');
  });

  it("keeps another window's newer draft over a send that failed without an answer", async () => {
    const { panel, main, fail } = await sendFromPanel();
    act(() => main.result.current.changeDraft('Something else instead', []));
    await waitFor(() =>
      expect(bridge.store.open('user-1', 'suggestion:sug-1', probe)).not.toBeNull()
    );
    await act(async () => fail(new Error('offline')));

    // The newer draft wins in both windows; the sender drops its retry and says so.
    await waitFor(() => expect(panel.result.current.composer.failureKind).toBe('superseded'));
    expect(panel.result.current.composer).toMatchObject({
      submission: null,
      draft: 'Something else instead',
    });
    expect(main.result.current.composer).toMatchObject({
      submission: null,
      draft: 'Something else instead',
    });
    expect(bridge.store.open('user-1', 'suggestion:sug-1', probe)?.retry).toBeNull();
  });

  it('drops the read after a send that answers once the composer shows the next task', async () => {
    const window = bridge.window();
    const reads: ((draft: unknown) => void)[] = [];
    let opened9 = 0;
    const slow = {
      ...window,
      // The read after the send, the second of the Action it opened, is held back.
      openDraft: vi.fn((request: { work: string }) =>
        request.work === 'action:act-9' && ++opened9 === 2
          ? new Promise((resolve) => reads.push(resolve))
          : window.openDraft(request as never)
      ),
    };
    const composer = composerIn(slow, { suggestionId: 'sug-a' });
    await act(async () => {});
    act(() => composer.result.current.changeDraft('Do it', []));
    submitMessage.mockResolvedValueOnce(submitted('act-9'));
    await act(async () =>
      composer.result.current.submitDraft(null, true, null, 0, 'prompt_each_time', 'sug-a')
    );
    // The conversation page shows the message: the send has gone through.
    await act(async () =>
      composer.result.current.setComposer((current) => ({
        ...current,
        submission: null,
        draft: '',
      }))
    );
    await waitFor(() => expect(reads).toHaveLength(1));

    composer.rerender({ suggestionId: 'sug-b' });
    await act(async () => reads[0](draftOf('for act-9')));
    expect(composer.result.current.composer.draft).toBe('');
    expect(bridge.store.open('user-1', 'suggestion:sug-b', probe)).toBeNull();
  });

  it('keeps words typed while the read is out over what the read brings', async () => {
    const window = bridge.window();
    let answer!: (draft: unknown) => void;
    const slow = {
      ...window,
      openDraft: vi.fn((request: { work: 'action:act-1' }) =>
        new Promise((resolve) => (answer = resolve)).then(() => window.openDraft(request))
      ),
    };
    bridge.store.update('user-1', 'action:act-1', draftOf('older'), 99);
    const composer = composerIn(slow);
    act(() => composer.result.current.changeDraft('newer', []));
    await act(async () => answer(null));

    expect(composer.result.current.composer.draft).toBe('newer');
    await waitFor(() =>
      expect(bridge.store.open('user-1', 'action:act-1', probe)?.text).toBe('newer')
    );
  });

  it('shares nothing when the words go back to what the task holds, as an undo does', async () => {
    const window = bridge.window();
    const updates = vi.spyOn(window, 'updateDraft');
    const composer = await opened(composerIn(window));

    act(() => composer.result.current.changeDraft('B', []));
    act(() => composer.result.current.changeDraft('', []));
    await new Promise((resolve) => setTimeout(resolve, 250));

    expect(updates).not.toHaveBeenCalled();
  });
});

const probe = { id: 999, send: () => undefined };

function draftOf(text: string) {
  return { text, mentions: [], quotes: [], attachments: [], retry: null };
}

function submitted(actionId: string): ActionMessageSubmitResult {
  return {
    kind: 'submitted',
    response: {
      action_id: actionId,
      message_id: 'm',
      step_id: 'step-1',
      action_status: 'processing',
      disposition: 'pending',
      process_id: null,
    },
  };
}
