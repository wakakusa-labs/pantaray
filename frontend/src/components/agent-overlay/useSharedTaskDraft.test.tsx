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
function composerIn(
  window: ReturnType<typeof bridge.window>,
  task: { actionId?: string; suggestionId?: string } = { actionId: 'act-1' }
) {
  const actions = {
    ...window,
    submitMessage,
    attachFile: vi.fn(async ({ name }: { name: string }) => ({
      attachmentId: ATTACHMENT_ID,
      name,
      byteSize: 9,
    })),
  } as unknown as Actions;
  return renderHook(() =>
    useOverlayComposerController({
      actions,
      initialActionId: task.actionId ?? null,
      suggestionId: task.suggestionId ?? null,
      suggestionAccepted: false,
      language: 'en',
      onRefreshedPage: () => undefined,
    })
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

  it('empties the other window while a send is out, and offers one lost there for its exact retry', async () => {
    const task = { suggestionId: 'sug-1' };
    const panel = await opened(composerIn(bridge.window(), task));
    const main = await opened(composerIn(bridge.window(), task));
    act(() => panel.result.current.changeDraft('Only the summary', []));
    await waitFor(() => expect(main.result.current.composer.draft).toBe('Only the summary'));

    let fail!: (error: Error) => void;
    submitMessage.mockReturnValueOnce(new Promise((_, reject) => (fail = reject)));
    act(() => panel.result.current.submitDraft(null, true, null, 0, 'prompt_each_time', 'sug-1'));
    await waitFor(() => expect(main.result.current.composer.draft).toBe(''));

    // No answer came: the other window offers the same request, as the sender does.
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

  it('drops a read that answers after the composer moved to another task', async () => {
    const window = bridge.window();
    let answer!: (draft: unknown) => void;
    const slow = {
      ...window,
      openDraft: vi.fn(() => new Promise<never>((resolve) => (answer = resolve as never))),
    };
    bridge.store.update('user-1', 'suggestion:sug-a', draftOf('for A'), 99);
    const actions = { ...slow, submitMessage } as unknown as Actions;
    const composer = renderHook(
      ({ suggestionId }) =>
        useOverlayComposerController({
          actions,
          initialActionId: null,
          suggestionId,
          suggestionAccepted: false,
          language: 'en',
          onRefreshedPage: () => undefined,
        }),
      { initialProps: { suggestionId: 'sug-a' } }
    );
    const readA = answer;
    composer.rerender({ suggestionId: 'sug-b' });
    await act(async () => readA(draftOf('for A')));

    expect(composer.result.current.composer.draft).toBe('');
    expect(bridge.store.open('user-1', 'suggestion:sug-b', probe)).toBeNull();
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
    const actions = { ...slow, submitMessage } as unknown as Actions;
    const composer = renderHook(
      ({ suggestionId }) =>
        useOverlayComposerController({
          actions,
          initialActionId: null,
          suggestionId,
          suggestionAccepted: false,
          language: 'en',
          onRefreshedPage: () => undefined,
        }),
      { initialProps: { suggestionId: 'sug-a' } }
    );
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
    const composer = composerIn(slow as unknown as ReturnType<typeof bridge.window>);
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
  return { text, mentions: [], attachments: [], retry: null };
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
