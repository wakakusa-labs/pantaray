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

  it('empties the other window while a send is out, and gives a failed one back', async () => {
    const task = { suggestionId: 'sug-1' };
    const panel = await opened(composerIn(bridge.window(), task));
    const main = await opened(composerIn(bridge.window(), task));
    act(() => panel.result.current.changeDraft('Only the summary', []));
    await waitFor(() => expect(main.result.current.composer.draft).toBe('Only the summary'));

    let fail!: (error: Error) => void;
    submitMessage.mockReturnValueOnce(new Promise((_, reject) => (fail = reject)));
    act(() => panel.result.current.submitDraft(null, true, null, 0, 'prompt_each_time', 'sug-1'));
    await waitFor(() => expect(main.result.current.composer.draft).toBe(''));

    await act(async () => fail(new Error('offline')));
    await waitFor(() => expect(main.result.current.composer.draft).toBe('Only the summary'));

    // Sent this time: nothing is left to offer, in either window.
    submitMessage.mockResolvedValueOnce(submitted('act-9'));
    act(() => panel.result.current.retrySubmission());
    await waitFor(() => expect(main.result.current.composer.draft).toBe(''));
    expect(bridge.store.open('user-1', 'suggestion:sug-1', probe)).toBeNull();
  });
});

const probe = { id: 999, send: () => undefined };

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
