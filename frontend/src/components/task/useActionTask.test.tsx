import { act, cleanup, renderHook, waitFor } from '@testing-library/react';
import type { ReactNode } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { UiLanguageProvider } from '@/context/UiLanguageContext';
import type {
  ActionApprovalBlocker,
  ActionLiveSnapshot,
  ActionLiveUpdate,
} from '../../../electron/src/actions/actionLiveCore';
import { createActionPage } from './actionTaskFixtures';
import { createTaskDraftBridge } from '@/tests/taskDraftBridge';
import { useActionTask } from './useActionTask';

type ElectronBridge = NonNullable<Window['electron']>;
type Actions = NonNullable<ElectronBridge['actions']>;

const wrapper = ({ children }: { children: ReactNode }) => (
  <UiLanguageProvider initialLanguage="en">{children}</UiLanguageProvider>
);

function update(
  page: ActionLiveSnapshot['page'],
  pageVersion: number,
  extra: Partial<ActionLiveSnapshot> = {}
): ActionLiveUpdate {
  return {
    kind: 'action_updated',
    snapshot: {
      actionId: page?.action.action_id ?? 'act-1',
      page,
      pageVersion,
      transientToolSteps: [],
      approvalBlockers: [],
      lifecycle: null,
      ...extra,
    },
  };
}

const blocker: ActionApprovalBlocker = {
  actionId: 'act-1',
  processId: 'run-1',
  approvalSessionId: 'approval-1',
  toolRequestId: 'tool-request-1',
  toolId: 'bash',
  intentClass: 'process_exec_local',
  commandSummary: { kind: 'bash', command: 'pytest -q', timeout_ms: 600000 },
};

describe('useActionTask', () => {
  let listeners: Set<(update: ActionLiveUpdate) => void>;
  const openConversation = vi.fn<Actions['openConversation']>();
  const submitMessage = vi.fn<Actions['submitMessage']>();
  const resumeAction = vi.fn<Actions['resumeAction']>();
  const readConversationPage = vi.fn<Actions['readConversationPage']>();
  const sendOrchestration = vi.fn();
  const submitApprovalDecision = vi.fn();

  const emit = (next: ActionLiveUpdate) =>
    act(() => listeners.forEach((listener) => listener(next)));
  const renderTask = async (actionId = 'act-1') => {
    const rendered = renderHook(({ id }) => useActionTask(id), {
      wrapper,
      initialProps: { id: actionId },
    });
    await waitFor(() => expect(rendered.result.current.approvalMode.mode).not.toBeNull());
    return rendered;
  };

  beforeEach(() => {
    listeners = new Set();
    openConversation.mockReset().mockResolvedValue(undefined);
    submitMessage.mockReset().mockReturnValue(new Promise(() => {}));
    resumeAction.mockReset().mockReturnValue(new Promise(() => {}));
    readConversationPage.mockReset();
    sendOrchestration.mockReset();
    submitApprovalDecision.mockReset().mockResolvedValue(undefined);
    Object.defineProperty(window, 'electron', {
      configurable: true,
      value: {
        actions: {
          openConversation,
          submitMessage,
          resumeAction,
          readConversationPage,
          readToolOutputPage: vi.fn(),
          ...createTaskDraftBridge().window(),
          discardAttachment: vi.fn(),
          onConversationUpdated: (callback: (update: ActionLiveUpdate) => void) => {
            listeners.add(callback);
            return () => listeners.delete(callback);
          },
        },
        orchestration: { send: sendOrchestration },
        agentOverlay: {
          submitApprovalDecision,
          getActionApprovalMode: vi.fn(async (actionId: string) => ({
            action_id: actionId,
            approval_mode: 'prompt_each_time',
            source: 'user_default',
          })),
          setActionApprovalMode: vi.fn(),
        },
      },
    });
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it('opens its Action and reads only that Action from the broadcast', async () => {
    const { result } = await renderTask();
    expect(openConversation).toHaveBeenCalledWith({ actionId: 'act-1' });

    emit(update(createActionPage('act-2', 'processing'), 1));
    expect(result.current.view).toBeNull();
    expect(result.current.status.kind).toBe('idle');

    emit(update(createActionPage('act-1', 'success'), 2));
    expect(result.current.view?.action?.action_id).toBe('act-1');
    emit(update(createActionPage('act-2', 'error'), 3, { approvalBlockers: [blocker] }));
    expect(result.current.view?.action?.status).toBe('success');
    expect(result.current.approval.blockers).toEqual([]);
  });

  it('keeps a newer page over an older or page-less snapshot', async () => {
    const { result } = await renderTask();
    emit(update(createActionPage('act-1', 'success', 'run-2'), 5));
    emit(update(createActionPage('act-1', 'processing', 'run-1'), 4));
    expect(result.current.view?.action?.latest_run_id).toBe('run-2');

    // Rebuilt after the Action finished: a lifecycle with no page read yet.
    emit(update(null, 0, { lifecycle: { processId: 'run-3', status: 'processing' } }));
    expect(result.current.view?.action?.latest_run_id).toBe('run-2');
    expect(result.current.status).toMatchObject({ kind: 'running', stopTarget: 'run-3' });
  });

  it('stops the lifecycle run first, then the page run', async () => {
    const { result } = await renderTask();
    emit(update(createActionPage('act-1', 'processing', 'run-1'), 1));
    act(() => result.current.stop());
    expect(sendOrchestration).toHaveBeenLastCalledWith({
      event: 'stop_process',
      data: { process_id: 'run-1' },
    });

    emit(
      update(createActionPage('act-1', 'processing', 'run-1'), 1, {
        lifecycle: { processId: 'run-2', status: 'processing' },
      })
    );
    act(() => result.current.stop());
    expect(sendOrchestration).toHaveBeenLastCalledWith({
      event: 'stop_process',
      data: { process_id: 'run-2' },
    });

    emit(update(createActionPage('act-1', 'success', 'run-2'), 2));
    act(() => result.current.stop());
    expect(sendOrchestration).toHaveBeenCalledTimes(2);
  });

  it('sends a follow-up into the running run', async () => {
    const { result } = await renderTask();
    emit(update(createActionPage('act-1', 'processing', 'run-1'), 1));
    act(() => result.current.composer.changeDraft('also check the tests', []));
    act(() => result.current.send());

    expect(submitMessage).toHaveBeenCalledTimes(1);
    expect(submitMessage.mock.calls[0][0].target).toEqual({
      kind: 'existing',
      action_id: 'act-1',
      expected_process_id: 'run-1',
    });
    expect(submitMessage.mock.calls[0][0].message.content).toBe('also check the tests');
    // The optimistic message holds the composer until the canonical page adopts it.
    act(() => result.current.send());
    expect(submitMessage).toHaveBeenCalledTimes(1);
  });

  it('sends a follow-up to a finished Action without an expected run', async () => {
    const { result } = await renderTask();
    emit(update(createActionPage('act-1', 'success'), 1));
    act(() => result.current.composer.changeDraft('one more thing', []));
    act(() => result.current.send());
    expect(submitMessage.mock.calls[0][0].target).toEqual({
      kind: 'existing',
      action_id: 'act-1',
      expected_process_id: null,
    });
  });

  it('resumes a stop once, even when pressed again before the answer', async () => {
    let answer: (value: Awaited<ReturnType<Actions['resumeAction']>>) => void = () => {};
    resumeAction.mockReturnValue(new Promise((resolve) => (answer = resolve)));
    const { result } = await renderTask();
    emit(update(createActionPage('act-1', 'canceled'), 1));
    expect(result.current.composer.action).toBe('resume');

    act(() => result.current.resume());
    act(() => result.current.resume());
    expect(resumeAction).toHaveBeenCalledTimes(1);

    await act(async () =>
      answer({
        kind: 'submitted',
        response: {
          action_id: 'act-1',
          message_id: resumeAction.mock.calls[0][0].messageId,
          step_id: 'step-resume',
          action_status: 'processing',
          disposition: 'started',
          process_id: 'run-2',
        },
      })
    );
    // Accepted, but the page still says resumable until its refresh lands.
    act(() => result.current.resume());
    expect(resumeAction).toHaveBeenCalledTimes(1);
  });

  it('decides an approval through the approval channel', async () => {
    const { result } = await renderTask();
    emit(update(createActionPage('act-1', 'processing'), 1, { approvalBlockers: [blocker] }));
    expect(result.current.status.kind).toBe('approval_pending');

    await act(() => result.current.approval.decide('approved_once', blocker));
    expect(submitApprovalDecision).toHaveBeenCalledWith({
      actionId: 'act-1',
      processId: 'run-1',
      approvalSessionId: 'approval-1',
      toolRequestId: 'tool-request-1',
      decision: 'approved_once',
    });
  });

  it('drops everything on an owner change and stops listening on unmount', async () => {
    const { result, unmount } = await renderTask();
    emit(update(createActionPage('act-1', 'processing'), 7, { approvalBlockers: [blocker] }));
    act(() => result.current.composer.changeDraft('draft for the old owner', []));

    emit({ kind: 'reset' });
    expect(result.current.view).toBeNull();
    expect(result.current.status.kind).toBe('idle');
    expect(result.current.approval.blockers).toEqual([]);
    expect(result.current.composer.state.draft).toBe('');

    // The version fence starts over with the new owner's live core.
    emit(update(createActionPage('act-1', 'success'), 1));
    expect(result.current.view?.action?.status).toBe('success');

    unmount();
    expect(listeners.size).toBe(0);
  });

  it('reopens after a failed open, and then hears the run it resumed', async () => {
    openConversation.mockRejectedValueOnce(new Error('page read failed'));
    const { result } = await renderTask();
    await waitFor(() => expect(result.current.conversation.openState).toBe('failed'));

    await act(() => result.current.conversation.reopen());
    expect(openConversation).toHaveBeenCalledTimes(2);
    expect(result.current.conversation.openState).toBe('open');

    // The reopen resumed the run, so its page and pause reach the pane.
    emit(update(createActionPage('act-1', 'processing'), 1));
    emit(update(createActionPage('act-1', 'processing'), 1, { approvalBlockers: [blocker] }));
    expect(result.current.status.kind).toBe('approval_pending');
    expect(result.current.approval.blockers).toEqual([blocker]);
  });
});
