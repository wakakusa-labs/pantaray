import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import AgentOverlay from './AgentOverlay';
import {
  readConversationScrollPosition,
  saveConversationScrollPosition,
} from './agent-overlay/conversationScrollPosition';
import type { ActionImageAttachResult } from '../../electron/src/ipc/schemas/actionImages';
import { UiLanguageProvider } from '@/context/UiLanguageContext';
import type {
  ActionConversationPage,
  ActionMessageSubmitResult,
} from '../../electron/src/actions/actionContracts';
import type { ActionLiveUpdate } from '../../electron/src/actions/actionLiveCore';

import type { OverlaySnapshotPayload } from './agent-overlay/model/overlayTypes';

type ElectronBridge = NonNullable<Window['electron']>;

function createPendingSnapshot(): OverlaySnapshotPayload {
  return {
    snapshot: {
      suggestionId: 'sug-1',
      commandId: 'cmd-1',
      interactionContract: 'action_offer',
      suggestionText: 'Need approval',
      reactionState: 'accepted',
      reactionTimestamp: '2026-03-08T00:00:00Z',
      actionPhase: 'processing',
      actionStatus: 'processing',
      actionErrorCode: null,
      actionFailureStage: null,
      actionFailureMessagePublic: null,
      processId: 'proc-1',
      actionId: 'act-1',
      updatedAt: '2026-03-08T00:00:03Z',
      lastSequence: 10,
      isLive: true,
    },
  };
}

function createResumedSnapshot(): OverlaySnapshotPayload {
  return {
    snapshot: {
      suggestionId: 'sug-1',
      commandId: 'cmd-1',
      interactionContract: 'action_offer',
      suggestionText: 'Need approval',
      reactionState: 'accepted',
      reactionTimestamp: '2026-03-08T00:00:00Z',
      actionPhase: 'processing',
      actionStatus: 'processing',
      actionErrorCode: null,
      actionFailureStage: null,
      actionFailureMessagePublic: null,
      processId: 'proc-1',
      actionId: 'act-1',
      updatedAt: '2026-03-08T00:00:05Z',
      lastSequence: 11,
      isLive: true,
    },
  };
}

function createCommentOnlySnapshot(): OverlaySnapshotPayload {
  return {
    snapshot: {
      suggestionId: 'sug-comment',
      commandId: null,
      interactionContract: 'message_only',
      suggestionText: 'Just a comment, nothing to approve',
      reactionState: null,
      reactionTimestamp: null,
      actionPhase: 'idle',
      actionStatus: null,
      actionErrorCode: null,
      actionFailureStage: null,
      actionFailureMessagePublic: null,
      processId: null,
      actionId: null,
      updatedAt: '2026-03-08T00:00:00Z',
      lastSequence: 3,
      isLive: true,
    },
  };
}

// These fixtures include a canonical page; lifecycle-only updates still use ActionLiveUpdate.
type ConversationUpdate = Extract<ActionLiveUpdate, { kind: 'action_updated' }> & {
  snapshot: { page: ActionConversationPage };
};
let pageVersion = 0;

function createConversationUpdate(
  nextCursor: string | null = null,
  processing = false,
  actionId = 'act-1'
): ConversationUpdate {
  return {
    kind: 'action_updated',
    snapshot: {
      pageVersion: ++pageVersion,
      actionId,
      page: {
        // prettier-ignore
        action: { action_id: actionId, suggestion_id: actionId === 'act-1' ? 'sug-1' : null, status: processing ? 'processing' : 'success', latest_run_id: 'run-1', approved_suggestion: null, resumable: false },
        // prettier-ignore
        runs: [{ run_id: 'run-1', status: processing ? 'running' : 'success', started_at: '2026-03-08T00:00:01.000000Z', completed_at: processing ? null : '2026-03-08T00:00:05.000000Z', completion_event_id: processing ? null : 'event-1', entries: [], final_output: processing ? null : 'canonical final output', error: null }],
        unadopted_messages: [],
        next_cursor: nextCursor,
      },
      transientToolSteps: [],
      approvalBlockers: [],
      lifecycle: null,
    },
  };
}

function createStoppedUpdate(): ConversationUpdate {
  const update = createConversationUpdate();
  Object.assign(update.snapshot.page.action, { status: 'canceled', resumable: true });
  Object.assign(update.snapshot.page.runs[0], {
    status: 'canceled',
    final_output: null,
    error: { code: 'ACTION_CANCELED', message: 'stopped' },
  });
  return update;
}

function withApprovalBlocker(
  update: ConversationUpdate,
  processId = 'run-1',
  toolId = 'bash',
  commandSummary: Record<string, string | number> = {
    kind: 'bash',
    command: 'pytest -q',
    timeout_ms: 600000,
  }
): ConversationUpdate {
  return {
    ...update,
    snapshot: {
      ...update.snapshot,
      approvalBlockers: [
        {
          actionId: update.snapshot.actionId,
          processId,
          approvalSessionId: 'approval-1',
          toolRequestId: 'tool-request-1',
          toolId,
          intentClass: 'process_exec_local',
          commandSummary,
        },
      ],
    },
  };
}

function withUserMessage(
  update: ConversationUpdate,
  messageId: string,
  content: string,
  status: 'pending' | 'not_executed'
): ConversationUpdate {
  update.snapshot.page.unadopted_messages.push({
    step_kind: 'user',
    approved_suggestion: null,
    step_id: `step-${messageId}`,
    step_number: null,
    message_id: messageId,
    accepted_sequence: 1,
    content,
    images: [],
    project_refs: [],
    status,
  });
  return update;
}

function withApprovedSuggestion(
  update: ConversationUpdate,
  comment: string | null,
  content: string
) {
  update.snapshot.page.action.approved_suggestion = { suggestion_id: 'sug-1', content };
  update.snapshot.page.runs[0].entries = [
    {
      step_kind: 'user',
      step_id: 'approval',
      step_number: 1,
      message_id: 'approval-message',
      accepted_sequence: 1,
      content: comment,
      images: [],
      project_refs: [],
      status: 'adopted',
      approved_suggestion: { suggestion_id: 'sug-1', content },
    },
  ];
  return update;
}

function createSubmittedResult(actionId: string, messageId: string): ActionMessageSubmitResult {
  return {
    kind: 'submitted',
    response: {
      action_id: actionId,
      message_id: messageId,
      step_id: `step-${messageId}`,
      action_status: 'processing',
      disposition: 'pending',
      process_id: null,
    },
  };
}

function createStartedResult(actionId: string, messageId: string): ActionMessageSubmitResult {
  return {
    kind: 'submitted',
    response: {
      action_id: actionId,
      message_id: messageId,
      step_id: `step-${messageId}`,
      action_status: 'processing',
      disposition: 'started',
      process_id: 'run-2',
    },
  };
}

describe('AgentOverlay broader E2E', () => {
  let snapshotListener: ((payload: unknown) => void) | null = null;
  let conversationListener: ((payload: ActionLiveUpdate) => void) | null;
  let retainedConversationUpdate: ConversationUpdate | null;
  let focusComposerListener: (() => void) | null;
  const submitApprovalDecision = vi.fn(async () => ({
    process_id: 'run-1',
    approval_session_id: 'approval-1',
    decision: 'approved_once',
    accepted: true,
  }));
  const sendOrchestration = vi.fn();
  const acceptAction = vi.fn();
  const resizeOverlay = vi.fn();
  const observeOverlaySize = vi.fn();
  const getActionApprovalMode = vi.fn();
  const setActionApprovalMode =
    vi.fn<NonNullable<NonNullable<ElectronBridge['agentOverlay']>['setActionApprovalMode']>>();
  const getWorkspaceEditCommandPreference =
    vi.fn<NonNullable<ElectronBridge['approval']>['getWorkspaceEditCommandPreference']>();
  const defaultPermissions: Awaited<ReturnType<typeof getWorkspaceEditCommandPreference>> = {
    scope_type: 'global',
    scope_ref: null,
    approval_mode: 'prompt_each_time',
    applies_to: ['workspace_edit_and_command'],
  };
  const [writeText, readConversationPage, submitMessage, stopAction, resumeAction] = [
    vi.fn(),
    vi.fn(),
    vi.fn(),
    vi.fn(),
    vi.fn(),
  ];
  const attachImage = vi.fn();
  const attachFile = vi.fn();
  const discardAttachment = vi.fn();
  const olderPage = createConversationUpdate().snapshot.page;
  Object.assign(olderPage.runs[0], { run_id: 'run-0', final_output: 'older final output' });

  beforeEach(() => {
    submitApprovalDecision.mockClear();
    submitMessage.mockReset();
    resumeAction.mockReset();
    resumeAction.mockResolvedValue({
      kind: 'submitted',
      response: {
        action_id: 'act-1',
        message_id: '00000000-0000-4000-8000-0000000000ff',
        step_id: 'step-resume',
        action_status: 'processing',
        disposition: 'started',
        process_id: 'run-2',
      },
    });
    attachImage.mockReset();
    attachImage.mockImplementation(async () => ({
      kind: 'attached',
      storagePath:
        `user-1/2026-09-08/${attachImage.mock.calls.length}1111111-1111-4111-8111-111111111111`.concat(
          '.png'
        ),
      mimeType: 'image/png',
      byteSize: 4,
      sha256: 'abc',
      widthPx: 10,
      heightPx: 10,
    }));
    attachFile.mockReset();
    attachFile.mockImplementation(
      async ({ bytes, name }: { bytes: ArrayBuffer; name: string }) => ({
        attachmentId: `${attachFile.mock.calls.length}2222222-2222-4222-8222-222222222222`,
        name,
        byteSize: bytes.byteLength,
      })
    );
    discardAttachment.mockReset().mockResolvedValue(undefined);
    stopAction.mockReset();
    sendOrchestration.mockReset();
    acceptAction.mockReset().mockResolvedValue(null);
    resizeOverlay.mockReset();
    observeOverlaySize.mockReset();
    setActionApprovalMode.mockReset();
    getWorkspaceEditCommandPreference.mockReset().mockResolvedValue(defaultPermissions);
    getActionApprovalMode.mockReset();
    getActionApprovalMode.mockResolvedValue({
      action_id: 'act-1',
      approval_mode: 'prompt_each_time',
      source: 'user_default',
    });
    snapshotListener = null;
    conversationListener = null;
    retainedConversationUpdate = null;
    focusComposerListener = null;
    readConversationPage.mockResolvedValue(olderPage);
    Element.prototype.scrollIntoView = vi.fn();
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } });
    vi.stubGlobal(
      'ResizeObserver',
      class {
        observe(target: Element) {
          observeOverlaySize(target);
        }
        disconnect() {}
      }
    );
    Object.defineProperty(window, 'electron', {
      configurable: true,
      value: {
        ipcRenderer: {
          on: (channel: string, callback: () => void) => {
            if (channel === 'overlay:focusComposer') focusComposerListener = callback;
            return () => {
              if (focusComposerListener === callback) focusComposerListener = null;
            };
          },
        },
        approval: {
          getWorkspaceEditCommandPreference,
        },
        agentOverlay: {
          onSnapshot: (cb: (payload: unknown) => void) => {
            snapshotListener = cb;
            return () => {
              snapshotListener = null;
            };
          },
          submitApprovalDecision,
          resize: resizeOverlay,
          hide: vi.fn(),
          stopAction,
          getActionApprovalMode,
          setActionApprovalMode,
        },
        orchestration: {
          onEvent: () => () => {},
          onStatus: () => () => {},
          acceptAction,
          send: sendOrchestration,
        },
        actions: {
          submitMessage,
          resumeAction,
          attachImage,
          attachFile,
          discardAttachment,
          readConversationPage,
          onConversationUpdated: (cb: typeof conversationListener) => {
            conversationListener = cb;
            if (retainedConversationUpdate) cb?.(retainedConversationUpdate);
            return () => {
              if (conversationListener === cb) conversationListener = null;
            };
          },
        },
      },
    });
  });

  afterEach(() => {
    cleanup();
    localStorage.clear();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it('shrinks the Electron window when a completed Action is collapsed', async () => {
    vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function (
      this: HTMLElement
    ) {
      const height = this.parentElement?.hasAttribute('data-overlay-scroll')
        ? 800
        : this.hasAttribute('data-overlay-scroll')
          ? 64
          : 40;
      return {
        x: 0,
        y: 0,
        top: 0,
        left: 0,
        bottom: height,
        right: 400,
        width: 400,
        height,
      } as DOMRect;
    });
    render(
      <UiLanguageProvider initialLanguage="en">
        <AgentOverlay />
      </UiLanguageProvider>
    );
    const completed = createResumedSnapshot();
    Object.assign(completed.snapshot, {
      actionPhase: 'terminal',
      actionStatus: 'success',
      processId: null,
      isLive: false,
    });
    await act(async () => snapshotListener?.(completed));
    await act(async () => conversationListener?.(createConversationUpdate()));
    await waitFor(() => expect(resizeOverlay.mock.lastCall?.[0]).toBeGreaterThan(500));
    resizeOverlay.mockClear();
    fireEvent.click(screen.getByRole('button', { name: 'Collapse' }));
    await waitFor(() => expect(resizeOverlay).toHaveBeenCalled());
    expect(resizeOverlay.mock.lastCall?.[0]).toBeLessThan(300);

    fireEvent.click(screen.getByRole('button', { name: 'Expand' }));
    await waitFor(() => expect(resizeOverlay.mock.lastCall?.[0]).toBeGreaterThan(500));
  });

  describe('conversation copy', () => {
    /** One turn of a page chain: the newest page is `cursor: null`, each points to the next older. */
    const turn = (index: number, sequence: number, nextCursor: string | null) => {
      const page = createConversationUpdate(nextCursor).snapshot.page;
      page.action.approved_suggestion = { suggestion_id: 'sug-1', content: 'Tidy the notes' };
      Object.assign(page.runs[0], {
        run_id: `run-${index}`,
        final_output: index === 0 ? 'canonical final output' : `answer ${index}`,
        entries: [
          {
            step_kind: 'user',
            step_id: `ask-${index}`,
            step_number: sequence,
            message_id: `message-${index}`,
            accepted_sequence: sequence,
            content: sequence === 1 ? 'Keep it short' : `question ${index}`,
            images:
              sequence === 1
                ? [{ kind: 'image', storage_path: 'user-1/2026-09-29/approval.png' }]
                : [],
            project_refs: [],
            status: 'adopted',
            approved_suggestion:
              sequence === 1 ? { suggestion_id: 'sug-1', content: 'Tidy the notes' } : null,
          },
        ],
      });
      return page;
    };
    const chain = [turn(0, 4, 'c1'), turn(1, 3, 'c2'), turn(2, 2, 'c3'), turn(3, 1, null)];
    const TRANSCRIPT = [
      'Pantaray\nTidy the notes',
      'You\nKeep it short\n(1 image)',
      'Pantaray\nanswer 3',
      'You\nquestion 2',
      'Pantaray\nanswer 2',
      'You\nquestion 1',
      'Pantaray\nanswer 1',
      'You\nquestion 0',
      'Pantaray\ncanonical final output',
    ].join('\n\n');

    async function showConversation() {
      readConversationPage.mockImplementation(async ({ cursor }: { cursor: string | null }) =>
        cursor === null ? chain[0] : chain[Number(cursor.slice(1))]
      );
      render(
        <UiLanguageProvider initialLanguage="en">
          <AgentOverlay />
        </UiLanguageProvider>
      );
      await act(async () => snapshotListener?.(createResumedSnapshot()));
      expect(screen.queryByRole('button', { name: 'Copy conversation' })).toBeNull();
      const update = createConversationUpdate();
      update.snapshot.page = chain[0];
      await act(async () => conversationListener?.(update));
      // The Overlay has loaded only its first older pages; the oldest turn is not shown.
      await screen.findByText('answer 2');
      expect(screen.queryByText('answer 3')).toBeNull();
      readConversationPage.mockClear();
      writeText.mockReset();
    }

    it('reads every page first and copies the whole exchange in order', async () => {
      await showConversation();

      fireEvent.click(screen.getByRole('button', { name: 'Copy conversation' }));

      await waitFor(() => expect(writeText).toHaveBeenCalledWith(TRANSCRIPT));
      expect(readConversationPage.mock.calls.map(([request]) => request.cursor)).toEqual([
        null,
        'c1',
        'c2',
        'c3',
      ]);
    });

    it('copies nothing when an older page cannot be read', async () => {
      await showConversation();
      readConversationPage.mockImplementation(async ({ cursor }: { cursor: string | null }) =>
        cursor === 'c3'
          ? { kind: 'stale_cursor' }
          : cursor === null
            ? chain[0]
            : chain[Number(cursor.slice(1))]
      );

      fireEvent.click(screen.getByRole('button', { name: 'Copy conversation' }));

      expect(await screen.findByRole('alert')).toHaveTextContent(
        'Couldn’t copy the conversation. Try again.'
      );
      expect(writeText).not.toHaveBeenCalled();
    });
  });

  it('submits against the canonical run and clears a stopped not-executed message', async () => {
    const messageId = '00000000-0000-4000-8000-000000000001';
    vi.spyOn(crypto, 'randomUUID')
      .mockReturnValueOnce(messageId)
      .mockReturnValueOnce('00000000-0000-4000-8000-000000000007');
    let resolveSubmit!: (response: ActionMessageSubmitResult) => void;
    submitMessage.mockReturnValueOnce(
      new Promise<ActionMessageSubmitResult>((resolve) => {
        resolveSubmit = resolve;
      })
    );
    render(
      <UiLanguageProvider initialLanguage="en">
        <AgentOverlay />
      </UiLanguageProvider>
    );
    await act(async () => snapshotListener?.(createResumedSnapshot()));
    await act(async () => conversationListener?.(createConversationUpdate(null, true)));

    fireEvent.click(screen.getByRole('button', { name: 'Expand' }));
    fireEvent.change(screen.getByLabelText('Message'), { target: { value: '  Continue  ' } });
    fireEvent.click(screen.getByRole('button', { name: 'Send' }));

    expect(submitMessage).toHaveBeenCalledWith({
      target: { kind: 'existing', action_id: 'act-1', expected_process_id: 'run-1' },
      message: {
        version: 1,
        message_id: messageId,
        content: 'Continue',
        images: [],
        language: 'en',
        project_refs: [],
        files: [],
      },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Stop' }));
    expect(stopAction).toHaveBeenCalledTimes(1);
    expect(sendOrchestration).toHaveBeenCalledWith({
      event: 'stop_process',
      data: { process_id: 'run-1' },
    });
    expect(submitMessage).toHaveBeenCalledTimes(1);

    await act(async () => resolveSubmit(createSubmittedResult('act-1', messageId)));
    readConversationPage.mockResolvedValueOnce(
      withUserMessage(createConversationUpdate(), messageId, 'Continue', 'not_executed').snapshot
        .page
    );
    const refresh = screen.getByRole('button', { name: 'Refresh conversation' });
    refresh.focus();
    fireEvent.click(refresh);

    const composer = await screen.findByLabelText('Message');
    expect(await screen.findByText('Not executed')).toBeInTheDocument();
    expect(readConversationPage).toHaveBeenCalledWith({
      actionId: 'act-1',
      cursor: null,
      limit: 25,
    });
    expect(composer).toHaveValue('');
    expect(composer).toHaveFocus();
    expect(submitMessage).toHaveBeenCalledTimes(1);

    submitMessage.mockResolvedValueOnce({ kind: 'action_conflict' });
    fireEvent.change(composer, { target: { value: 'Cannot resume' } });
    fireEvent.click(screen.getByRole('button', { name: 'Send' }));
    const conflict = await screen.findByRole('alert');
    expect(conflict).toHaveTextContent('This conversation cannot accept another message.');
    expect(screen.getByLabelText('Message')).toHaveValue('Cannot resume');
    expect(screen.queryByRole('button', { name: /Retry sending|Refresh conversation/ })).toBeNull();
    expect(screen.getByLabelText('Message')).toHaveAttribute('readonly');
    // 送信できない会話でも、本文を選択してコピーできる入力欄に留まる。
    expect(screen.getByLabelText('Message')).toHaveFocus();
    expect(submitMessage).toHaveBeenCalledTimes(2);
  });

  it('keeps the open approval mode menu out of the subtree the overlay measures', async () => {
    render(
      <UiLanguageProvider initialLanguage="en">
        <AgentOverlay />
      </UiLanguageProvider>
    );
    await act(async () => snapshotListener?.(createResumedSnapshot()));
    await act(async () => conversationListener?.(createConversationUpdate(null, true)));
    fireEvent.click(screen.getByRole('button', { name: 'Expand' }));

    const trigger = await screen.findByRole('button', { name: /permissions: Ask every time/ });
    const measured = observeOverlaySize.mock.calls.map(([target]) => target as HTMLElement);
    expect(measured.length).toBeGreaterThan(0);
    // The trigger is inside what the overlay measures; the popover must not be,
    // or opening it would grow the measured content and resize the window.
    expect(measured.some((element) => element.contains(trigger))).toBe(true);

    fireEvent.click(trigger);
    const menu = screen.getByRole('menu');
    expect(measured.some((element) => element.contains(menu))).toBe(false);
  });

  it('keeps every composer control inside the input box, in reading order', async () => {
    render(
      <UiLanguageProvider initialLanguage="en">
        <AgentOverlay />
      </UiLanguageProvider>
    );
    await act(async () => snapshotListener?.(createResumedSnapshot()));
    await act(async () => conversationListener?.(createConversationUpdate(null, true)));
    fireEvent.click(screen.getByRole('button', { name: 'Expand' }));

    const message = await screen.findByLabelText('Message');
    const box = message.closest('form.overlay-composer');
    expect(box).not.toBeNull();
    const permission = await within(box as HTMLElement).findByRole('button', {
      name: /permissions: Ask every time/,
    });

    // Tab order is document order, so this list is the keyboard order through the
    // box: write, then attach, then permissions, then the primary control.
    // Nothing is left below it.
    const controls = Array.from((box as HTMLElement).querySelectorAll('textarea, button'));
    expect(controls).toHaveLength(4);
    expect(controls[0]).toBe(message);
    expect(controls[1]).toBe(within(box as HTMLElement).getByRole('button', { name: 'Add files' }));
    expect(controls[2]).toBe(permission);
    expect(controls[3]).toBe(within(box as HTMLElement).getByRole('button', { name: 'Stop' }));

    fireEvent.change(message, { target: { value: 'ready' } });
    const send = within(box as HTMLElement).getByRole('button', { name: 'Send' });
    expect(Array.from((box as HTMLElement).querySelectorAll('textarea, button'))[3]).toBe(send);
    expect(send).toBeEnabled();
  });

  it('retries only the exact failed request after explicit activation', async () => {
    const messageId = '00000000-0000-4000-8000-000000000002';
    const randomUUID = vi.spyOn(crypto, 'randomUUID').mockReturnValue(messageId);
    submitMessage
      .mockRejectedValueOnce(new Error('transport failed'))
      .mockResolvedValueOnce(createSubmittedResult('act-1', messageId));
    render(
      <UiLanguageProvider initialLanguage="en">
        <AgentOverlay />
      </UiLanguageProvider>
    );
    await act(async () => snapshotListener?.(createResumedSnapshot()));
    await act(async () => conversationListener?.(createConversationUpdate()));

    fireEvent.click(screen.getByRole('button', { name: 'Expand' }));
    fireEvent.change(screen.getByLabelText('Message'), { target: { value: 'Retry this' } });
    const send = screen.getByRole('button', { name: 'Send' });
    send.focus();
    fireEvent.click(send);
    const retry = await screen.findByRole('button', { name: 'Retry sending' });

    expect(screen.getByText('Send failed')).toBeInTheDocument();
    await waitFor(() => expect(retry).toHaveFocus());
    expect(submitMessage).toHaveBeenCalledTimes(1);
    const exactRequest = submitMessage.mock.calls[0][0];
    expect(exactRequest.target).toEqual({
      kind: 'existing',
      action_id: 'act-1',
      expected_process_id: null,
    });
    fireEvent.click(retry);
    await waitFor(() => expect(submitMessage).toHaveBeenCalledTimes(2));

    expect(submitMessage.mock.calls[1][0]).toBe(exactRequest);
    expect(randomUUID).toHaveBeenCalledTimes(1);
    const refresh = await screen.findByRole('button', { name: 'Refresh conversation' });
    await waitFor(() => expect(refresh).toHaveFocus());
    readConversationPage.mockResolvedValueOnce(
      createConversationUpdate('older-cursor', true).snapshot.page
    );
    const olderPage = withUserMessage(
      createConversationUpdate(null, true),
      messageId,
      'Retry this',
      'pending'
    ).snapshot.page;
    olderPage.runs = [];
    readConversationPage.mockResolvedValueOnce(olderPage);
    fireEvent.click(refresh);
    await waitFor(() => expect(screen.getByLabelText('Message')).toHaveValue(''));
    expect(screen.getAllByText('Retry this')).toHaveLength(1);
  });

  it('retains the input across repeated transport failures and a live execution failure', async () => {
    submitMessage.mockRejectedValue(new Error('HTTP 500'));
    render(
      <UiLanguageProvider initialLanguage="en">
        <AgentOverlay />
      </UiLanguageProvider>
    );
    await act(async () => snapshotListener?.(createResumedSnapshot()));
    await act(async () => conversationListener?.(createConversationUpdate(null, true)));
    const input = screen.getByLabelText('Message');
    fireEvent.change(input, { target: { value: 'Keep my message' } });
    fireEvent.click(screen.getByRole('button', { name: 'Send' }));
    const retry = await screen.findByRole('button', { name: 'Retry sending' });
    expect(screen.getByLabelText('Message')).toBe(input);
    expect(input).toHaveValue('Keep my message');
    expect(input).toHaveAttribute('readonly');
    const exactRequest = submitMessage.mock.calls[0][0];
    const failed = createResumedSnapshot();
    Object.assign(failed.snapshot, {
      lastSequence: 12,
      actionPhase: 'terminal',
      actionStatus: 'error',
      actionFailureStage: 'persist_final_state_failed',
      actionFailureMessagePublic: 'Could not save the result.',
    });
    await act(async () => snapshotListener?.(failed));
    expect(retry).toBeVisible();
    fireEvent.click(retry);
    await screen.findByRole('button', { name: 'Retry sending' });
    expect(submitMessage.mock.calls[1][0]).toBe(exactRequest);
    expect(input).toHaveValue('Keep my message');
    submitMessage.mockResolvedValueOnce(
      createSubmittedResult('act-1', exactRequest.message.message_id)
    );
    fireEvent.click(screen.getByRole('button', { name: 'Retry sending' }));
    await screen.findByRole('button', { name: 'Refresh conversation' });
    expect(submitMessage.mock.calls[2][0]).toBe(exactRequest);
    await act(async () =>
      conversationListener?.(
        withUserMessage(
          createConversationUpdate(),
          exactRequest.message.message_id,
          'Keep my message',
          'pending'
        )
      )
    );
    expect(input).toHaveValue('');
    expect(input).not.toHaveAttribute('readonly');
    expect(screen.queryByRole('button', { name: 'Retry sending' })).toBeNull();
  });

  it('refreshes a stale process rejection before allocating a new request', async () => {
    const firstMessageId = '00000000-0000-4000-8000-000000000005';
    const secondMessageId = '00000000-0000-4000-8000-000000000006';
    vi.spyOn(crypto, 'randomUUID')
      .mockReturnValueOnce(firstMessageId)
      .mockReturnValueOnce(secondMessageId);
    submitMessage
      .mockResolvedValueOnce({ kind: 'expected_process_conflict' })
      .mockResolvedValueOnce(createSubmittedResult('act-1', secondMessageId));
    render(
      <UiLanguageProvider initialLanguage="en">
        <AgentOverlay />
      </UiLanguageProvider>
    );
    await act(async () => snapshotListener?.(createResumedSnapshot()));
    await act(async () => conversationListener?.(createConversationUpdate(null, true)));

    fireEvent.click(screen.getByRole('button', { name: 'Expand' }));
    fireEvent.change(screen.getByLabelText('Message'), {
      target: { value: 'Use the current run' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Send' }));
    const refresh = await screen.findByRole('button', { name: 'Refresh conversation' });
    expect(screen.queryByRole('button', { name: 'Retry sending' })).toBeNull();

    readConversationPage.mockRejectedValueOnce(new Error('read failed'));
    fireEvent.click(refresh);
    const failedRefresh = await screen.findByRole('button', {
      name: 'Refresh failed. Try again.',
    });
    expect(submitMessage).toHaveBeenCalledTimes(1);

    const latest = createConversationUpdate(null, true).snapshot.page;
    latest.action.latest_run_id = 'run-2';
    latest.runs[0].run_id = 'run-2';
    let resolveLatest!: (page: ActionConversationPage) => void;
    readConversationPage.mockReturnValueOnce(
      new Promise<ActionConversationPage>((resolve) => {
        resolveLatest = resolve;
      })
    );
    failedRefresh.focus();
    fireEvent.click(failedRefresh);
    await act(async () => conversationListener?.(createConversationUpdate(null, true)));
    await act(async () => resolveLatest(latest));

    const restoredComposer = await screen.findByLabelText('Message');
    expect(restoredComposer).toHaveValue('Use the current run');
    await waitFor(() => expect(restoredComposer).toHaveFocus());
    fireEvent.click(screen.getByRole('button', { name: 'Send' }));
    await waitFor(() => expect(submitMessage).toHaveBeenCalledTimes(2));
    expect(submitMessage.mock.calls[1][0]).toEqual({
      target: { kind: 'existing', action_id: 'act-1', expected_process_id: 'run-2' },
      message: {
        version: 1,
        message_id: secondMessageId,
        content: 'Use the current run',
        images: [],
        language: 'en',
        project_refs: [],
        files: [],
      },
    });
  });

  it('continues the bound history conversation instead of starting a new Action', async () => {
    const messageId = '00000000-0000-4000-8000-000000000008';
    vi.spyOn(crypto, 'randomUUID').mockReturnValueOnce(messageId);
    submitMessage.mockResolvedValueOnce(createSubmittedResult('history-action', messageId));
    vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockReturnValue({
      bottom: 200,
      height: 200,
      left: 0,
      right: 380,
      top: 0,
      width: 380,
      x: 0,
      y: 0,
      toJSON: () => ({}),
    });
    render(
      <UiLanguageProvider initialLanguage="en">
        <AgentOverlay entryMode="standalone" initialActionId="history-action" />
      </UiLanguageProvider>
    );

    // Main's push failed, so the bound Overlay recovers through its own read.
    readConversationPage.mockRejectedValueOnce(new Error('read failed'));
    fireEvent.click(await screen.findByRole('button', { name: 'Refresh conversation' }));
    const failed = await screen.findByRole('button', { name: 'Refresh failed. Try again.' });
    readConversationPage.mockResolvedValueOnce(
      createConversationUpdate(null, false, 'history-action').snapshot.page
    );
    fireEvent.click(failed);

    expect(await screen.findByText('canonical final output')).toBeInTheDocument();
    const composer = screen.getByLabelText('Message');
    await waitFor(() => expect(composer).not.toBeDisabled());
    fireEvent.change(composer, { target: { value: 'Keep going' } });
    fireEvent.click(screen.getByRole('button', { name: 'Send' }));

    expect(submitMessage.mock.calls[0][0].target).toEqual({
      kind: 'existing',
      action_id: 'history-action',
      expected_process_id: null,
    });
  });

  it('uses the standalone response identity for retained replay and fences a reset', async () => {
    const firstMessageId = '00000000-0000-4000-8000-000000000003';
    const secondMessageId = '00000000-0000-4000-8000-000000000004';
    vi.spyOn(crypto, 'randomUUID')
      .mockReturnValueOnce(firstMessageId)
      .mockReturnValueOnce(secondMessageId);
    let resolveFirst!: (response: ActionMessageSubmitResult) => void;
    let resolveSecond!: (response: ActionMessageSubmitResult) => void;
    submitMessage
      .mockReturnValueOnce(
        new Promise<ActionMessageSubmitResult>((resolve) => {
          resolveFirst = resolve;
        })
      )
      .mockReturnValueOnce(
        new Promise<ActionMessageSubmitResult>((resolve) => {
          resolveSecond = resolve;
        })
      );
    vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockReturnValue({
      bottom: 200,
      height: 200,
      left: 0,
      right: 380,
      top: 0,
      width: 380,
      x: 0,
      y: 0,
      toJSON: () => ({}),
    });
    const computedStyleSpy = vi.spyOn(window, 'getComputedStyle').mockReturnValue({
      borderBottomWidth: '0px',
      borderTopWidth: '0px',
      fontSize: '16px',
      marginBottom: '0px',
      marginTop: '0px',
      paddingBottom: '0px',
      paddingTop: '0px',
    } as CSSStyleDeclaration);
    render(
      <UiLanguageProvider initialLanguage="en">
        <button type="button">Outside focus</button>
        <AgentOverlay entryMode="standalone" />
      </UiLanguageProvider>
    );

    const firstComposer = screen.getByLabelText('Message');
    expect(firstComposer).toHaveFocus();
    await waitFor(() => {
      expect(observeOverlaySize).toHaveBeenCalled();
      // header + 会話本文 + composer の 3 ブロック（各 200px モック）+ action バッファ 32px。
      expect(resizeOverlay).toHaveBeenCalledWith(632);
    });
    computedStyleSpy.mockRestore();
    const outsideFocus = screen.getByRole('button', { name: 'Outside focus' });
    outsideFocus.focus();
    act(() => focusComposerListener?.());
    expect(firstComposer).toHaveFocus();
    fireEvent.change(firstComposer, { target: { value: 'Start here' } });
    fireEvent.click(screen.getByRole('button', { name: /permissions: Ask every time/ }));
    fireEvent.click(screen.getByRole('menuitemradio', { name: /Auto-approve/ }));
    fireEvent.click(screen.getByRole('button', { name: 'Send' }));
    expect(submitMessage.mock.calls[0][0].target).toEqual({
      kind: 'new',
      approval_mode: 'always_allow',
    });
    expect(screen.queryByRole('button', { name: 'Send' })).toBeNull();
    outsideFocus.focus();

    retainedConversationUpdate = withUserMessage(
      createConversationUpdate(null, true, 'standalone-action'),
      firstMessageId,
      'Start here',
      'pending'
    );
    await act(async () => conversationListener?.(retainedConversationUpdate!));
    expect(screen.getByText('Sending')).toBeInTheDocument();
    expect(screen.queryByText('Pending')).toBeNull();

    await act(async () => resolveFirst(createSubmittedResult('standalone-action', firstMessageId)));
    expect(await screen.findByText('Pending')).toBeInTheDocument();
    expect(outsideFocus).toHaveFocus();
    expect(screen.getByLabelText('Message')).toBeInTheDocument();

    retainedConversationUpdate = null;
    await act(async () => conversationListener?.({ kind: 'reset' }));
    const resetComposer = screen.getByLabelText('Message');
    fireEvent.change(resetComposer, { target: { value: 'Discard after reset' } });
    fireEvent.click(screen.getByRole('button', { name: 'Send' }));
    expect(submitMessage.mock.calls[1][0].target).toEqual({
      kind: 'new',
      approval_mode: 'prompt_each_time',
    });

    await act(async () => conversationListener?.({ kind: 'reset' }));
    expect(screen.queryByText('Discard after reset')).toBeNull();
    expect(screen.getByLabelText('Message')).toHaveValue('');
    await act(async () =>
      resolveSecond(createSubmittedResult('standalone-action', secondMessageId))
    );

    expect(screen.queryByText('Sent, updating')).toBeNull();
    expect(screen.getByLabelText('Message')).toHaveValue('');
  });

  it('keeps displayed answers but uses the live process for Stop and failure feedback without a page', async () => {
    render(
      <UiLanguageProvider initialLanguage="en">
        <AgentOverlay />
      </UiLanguageProvider>
    );
    await act(async () => snapshotListener?.(createResumedSnapshot()));
    await act(async () => conversationListener?.(createConversationUpdate()));
    fireEvent.click(screen.getByRole('button', { name: 'Expand' }));
    const running = createResumedSnapshot();
    Object.assign(running.snapshot, { processId: 'new-process', lastSequence: 12 });
    await act(async () => snapshotListener?.(running));
    await act(async () =>
      conversationListener?.({
        kind: 'action_updated',
        snapshot: {
          pageVersion: 0,
          actionId: 'act-1',
          page: null,
          transientToolSteps: [],
          approvalBlockers: [],
          lifecycle: null,
        },
      })
    );
    expect(screen.getByText('canonical final output')).toBeInTheDocument();
    expect(screen.queryByLabelText('Message')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'Stop' }));
    await waitFor(() =>
      expect(sendOrchestration).toHaveBeenCalledWith({
        event: 'stop_process',
        data: { process_id: 'new-process' },
      })
    );
    Object.assign(running.snapshot, {
      lastSequence: 13,
      actionPhase: 'terminal',
      actionStatus: 'error',
      actionFailureMessagePublic: 'New run failed.',
    });
    await act(async () => snapshotListener?.(running));
    expect(screen.getByText('New run failed.')).toBeInTheDocument();
    expect(screen.getByText('canonical final output')).toBeInTheDocument();
    const recovered = createConversationUpdate().snapshot.page;
    recovered.action.latest_run_id = recovered.runs[0].run_id = 'new-process';
    readConversationPage.mockResolvedValueOnce(recovered);
    fireEvent.click(screen.getByRole('button', { name: 'Expand' }));
    fireEvent.click(screen.getByRole('button', { name: 'Refresh conversation' }));
    await screen.findByLabelText('Message');
    expect(readConversationPage).toHaveBeenCalledWith({
      actionId: 'act-1',
      cursor: null,
      limit: 25,
    });
    expect(screen.queryByText('New run failed.')).toBeNull();
  });

  it('refreshes history only when a cloned snapshot carries a new canonical page version', async () => {
    render(
      <UiLanguageProvider initialLanguage="en">
        <AgentOverlay />
      </UiLanguageProvider>
    );
    await act(async () => snapshotListener?.(createResumedSnapshot()));
    const live = createConversationUpdate('older-cursor', true);
    await act(async () => conversationListener?.(live));
    readConversationPage.mockClear();
    const transient = structuredClone(live);
    await act(async () => conversationListener?.(transient));
    expect(readConversationPage).not.toHaveBeenCalled();
    transient.snapshot.pageVersion += 1;
    await act(async () => conversationListener?.(transient));
    expect(readConversationPage).toHaveBeenCalledWith({
      actionId: 'act-1',
      cursor: 'older-cursor',
      limit: 25,
    });
  });

  it('saves the loaded history depth without requiring a scroll event', async () => {
    saveConversationScrollPosition('act-1', { top: 0, atBottom: false, pageCount: 1 });
    const middle = structuredClone(olderPage)!;
    middle.next_cursor = 'last-page';
    readConversationPage.mockResolvedValueOnce(middle).mockResolvedValueOnce(middle);
    const { container } = render(
      <UiLanguageProvider initialLanguage="en">
        <AgentOverlay />
      </UiLanguageProvider>
    );
    await act(async () => snapshotListener?.(createResumedSnapshot()));
    const scroll = container.querySelector<HTMLDivElement>('[data-overlay-scroll]')!;
    Object.defineProperties(scroll, {
      scrollHeight: { value: 1000 },
      clientHeight: { value: 200 },
    });
    await act(async () => conversationListener?.(createConversationUpdate('older-cursor')));
    fireEvent.click(screen.getByRole('button', { name: 'Expand' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Load older messages' }));
    await waitFor(() =>
      expect(screen.queryByRole('button', { name: 'Load older messages' })).toBeNull()
    );
    expect(scroll.scrollTop).toBe(0);
    expect(readConversationScrollPosition('act-1')).toEqual({
      top: 0,
      atBottom: false,
      pageCount: 4,
    });
  });

  it('preserves keyboard focus while loading the remaining history page', async () => {
    const middle = structuredClone(olderPage)!;
    middle.next_cursor = 'last-page';
    readConversationPage.mockResolvedValueOnce(middle).mockResolvedValueOnce(middle);
    render(
      <UiLanguageProvider initialLanguage="en">
        <AgentOverlay />
      </UiLanguageProvider>
    );
    await act(async () => snapshotListener?.(createResumedSnapshot()));
    await act(async () => conversationListener?.(createConversationUpdate('older-cursor')));
    fireEvent.click(screen.getByRole('button', { name: 'Expand' }));
    const loadButton = await screen.findByRole('button', { name: 'Load older messages' });
    let finish!: (page: ActionConversationPage) => void;
    readConversationPage.mockReturnValueOnce(
      new Promise<ActionConversationPage>((resolve) => {
        finish = resolve;
      })
    );
    loadButton.focus();
    fireEvent.click(loadButton);
    expect(loadButton).toHaveAttribute('aria-disabled', 'true');
    fireEvent.click(loadButton);
    expect(loadButton).toHaveFocus();
    await act(async () => finish(olderPage!));
    expect(screen.queryByRole('button', { name: 'Load older messages' })).toBeNull();
    expect(document.activeElement).toHaveTextContent('canonical final output');
  });

  it('renders approval panel and resumes naturally after approval', async () => {
    render(
      <UiLanguageProvider initialLanguage="en">
        <AgentOverlay />
      </UiLanguageProvider>
    );

    await act(async () => snapshotListener?.(createPendingSnapshot()));
    await act(async () =>
      conversationListener?.(withApprovalBlocker(createConversationUpdate(null, true)))
    );

    expect(screen.getByText('Approval required')).toBeInTheDocument();
    expect(screen.getByText('Run a local command.')).toBeInTheDocument();
    expect(screen.getByText('Command')).toBeInTheDocument();
    expect(screen.getByText('pytest -q')).toBeInTheDocument();
    expect(screen.queryByText('Timeout')).toBeNull();
    expect(screen.queryByText('600s')).toBeNull();
    expect(screen.queryByText('process_exec_local')).toBeNull();
    expect(screen.queryByText(/"command"/)).toBeNull();
    fireEvent.change(screen.getByLabelText('Message'), {
      target: { value: 'Continue after this' },
    });
    expect(screen.getByRole('button', { name: 'Send' })).toBeEnabled();

    fireEvent.click(screen.getByRole('button', { name: 'Expand' }));
    fireEvent.click(screen.getByRole('button', { name: 'Approve' }));

    await waitFor(() => {
      expect(submitApprovalDecision).toHaveBeenCalledWith({
        actionId: 'act-1',
        processId: 'run-1',
        approvalSessionId: 'approval-1',
        toolRequestId: 'tool-request-1',
        decision: 'approved_once',
      });
    });
    await act(async () => conversationListener?.(createConversationUpdate(null, true)));
    expect(screen.queryByText('Approval required')).toBeNull();
    await act(async () => conversationListener?.(createConversationUpdate('older-cursor')));
    // 更新ごとに「その時点の最新行」を対象にするため、直近の呼び出しを見る。
    const scrolledItem = () => {
      const { instances } = vi.mocked(Element.prototype.scrollIntoView).mock;
      return instances[instances.length - 1];
    };
    await waitFor(() => expect(scrolledItem()).toHaveTextContent('canonical final output'));
    expect(screen.queryByRole('button', { name: 'Stop' })).toBeNull();
    expect(screen.getByText('older final output')).toBeInTheDocument();

    await act(async () => conversationListener?.({ kind: 'reset' }));
    expect(screen.queryByText('canonical final output')).toBeNull();
    await act(async () => conversationListener?.(createConversationUpdate(null, true)));
    const failedSnapshot = createResumedSnapshot();
    // prettier-ignore
    Object.assign(failedSnapshot.snapshot, { lastSequence: 12, actionPhase: 'terminal', actionStatus: 'error', actionFailureStage: 'persist_final_state_failed', actionFailureMessagePublic: 'Could not save the final result.' });
    await act(async () => snapshotListener?.(failedSnapshot));
    expect(screen.getByText('Could not save the final result.')).toBeInTheDocument();
    // 終了状態のバッジは持たない。失敗は本文だけで伝える。
    expect(screen.queryByText('Failed')).toBeNull();
  });

  it('uses a started submit fence until the controller observes the new lifecycle', async () => {
    const messageId = '00000000-0000-4000-8000-000000000008';
    vi.spyOn(crypto, 'randomUUID').mockReturnValue(messageId);
    submitMessage.mockResolvedValueOnce(createStartedResult('act-1', messageId));
    render(
      <UiLanguageProvider initialLanguage="en">
        <AgentOverlay />
      </UiLanguageProvider>
    );

    const failedSnapshot = createResumedSnapshot();
    // prettier-ignore
    Object.assign(failedSnapshot.snapshot, { lastSequence: 12, actionPhase: 'terminal', actionStatus: 'error', actionFailureStage: 'persist_final_state_failed', actionFailureMessagePublic: 'Could not save the final result.', processId: null });
    await act(async () => snapshotListener?.(failedSnapshot));
    await act(async () => conversationListener?.(createConversationUpdate()));

    fireEvent.click(screen.getByRole('button', { name: 'Expand' }));
    fireEvent.change(screen.getByLabelText('Message'), { target: { value: 'Try again' } });
    fireEvent.click(screen.getByRole('button', { name: 'Send' }));
    await screen.findByRole('button', { name: 'Refresh conversation' });

    await act(async () => conversationListener?.(createConversationUpdate(null, true)));
    expect(screen.queryByRole('button', { name: 'Stop' })).toBeNull();

    const processing = createConversationUpdate(null, true);
    processing.snapshot.page.action.latest_run_id = 'run-2';
    processing.snapshot.page.runs[0].run_id = 'run-2';
    await act(async () => conversationListener?.(processing));

    expect(screen.queryByText('Could not save the final result.')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'Stop' }));
    expect(sendOrchestration).toHaveBeenLastCalledWith({
      event: 'stop_process',
      data: { process_id: 'run-2' },
    });

    const completedSnapshot = { snapshot: { ...failedSnapshot.snapshot, lastSequence: 13 } };
    await act(async () => snapshotListener?.(completedSnapshot));
    expect(screen.queryByRole('button', { name: 'Stop' })).toBeNull();
    expect(screen.getByText('Could not save the final result.')).toBeInTheDocument();
  });

  it('renders run_python approval without raw summary JSON', async () => {
    render(
      <UiLanguageProvider initialLanguage="en">
        <AgentOverlay />
      </UiLanguageProvider>
    );

    await act(async () => snapshotListener?.(createPendingSnapshot()));
    await act(async () =>
      conversationListener?.(
        withApprovalBlocker(createConversationUpdate(null, true), 'run-1', 'run_python', {
          summary_kind: 'run_python',
          cwd: '.',
          code_size_bytes: 128,
          args_count: 2,
          timeout_ms: 600000,
        })
      )
    );

    expect(screen.getByText('Run Python code.')).toBeInTheDocument();
    expect(screen.getByText('Python code')).toBeInTheDocument();
    expect(
      screen.getByText('Generated Python code will run in the workspace.')
    ).toBeInTheDocument();
    expect(screen.getByText('Size')).toBeInTheDocument();
    expect(screen.getByText('128 B')).toBeInTheDocument();
    expect(screen.queryByText('Timeout')).toBeNull();
    expect(screen.queryByText('600s')).toBeNull();
    expect(screen.queryByText('process_exec_local')).toBeNull();
    expect(screen.queryByText(/summary_kind/)).toBeNull();
  });

  it('does not expose internal approval bridge errors', async () => {
    submitApprovalDecision.mockRejectedValueOnce(
      new Error(
        "Error invoking remote method 'overlay:submitApprovalDecision': ActionApprovalDecisionError: Local backend request failed."
      )
    );

    render(
      <UiLanguageProvider initialLanguage="en">
        <AgentOverlay />
      </UiLanguageProvider>
    );

    await act(async () => snapshotListener?.(createPendingSnapshot()));
    await act(async () =>
      conversationListener?.(withApprovalBlocker(createConversationUpdate(null, true)))
    );

    fireEvent.click(screen.getByRole('button', { name: 'Expand' }));
    fireEvent.click(screen.getByRole('button', { name: 'Approve' }));

    await waitFor(() => {
      expect(
        screen.getByText('Failed to submit approval decision. Refresh and try again.')
      ).toBeInTheDocument();
    });
    await act(async () =>
      conversationListener?.(withApprovalBlocker(createConversationUpdate(null, true)))
    );
    expect(
      screen.getByText('Failed to submit approval decision. Refresh and try again.')
    ).toBeInTheDocument();
    expect(screen.queryByText(/Error invoking remote method/)).toBeNull();
  });

  it('starts a conversation from a comment-only Suggestion overlay', async () => {
    const messageId = '00000000-0000-4000-8000-000000000042';
    vi.spyOn(crypto, 'randomUUID').mockReturnValueOnce(messageId);
    submitMessage.mockResolvedValueOnce(createStartedResult('act-comment', messageId));
    render(
      <UiLanguageProvider initialLanguage="en">
        <AgentOverlay />
      </UiLanguageProvider>
    );
    await act(async () => snapshotListener?.(createCommentOnlySnapshot()));

    expect(screen.getByText('Just a comment, nothing to approve')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Accept' })).toBeNull();
    expect(screen.queryByRole('textbox', { name: 'Message' })).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'Reply to this suggestion' }));
    expect(screen.getByRole('textbox', { name: 'Message' })).toHaveFocus();
    fireEvent.click(screen.getByRole('button', { name: 'Expand' }));
    const composer = screen.getByLabelText('Message');

    fireEvent.change(composer, { target: { value: ' Do it then ' } });
    fireEvent.click(screen.getByRole('button', { name: 'Send' }));

    expect(submitMessage).toHaveBeenCalledWith({
      target: {
        kind: 'new',
        approval_mode: 'prompt_each_time',
        reply_to_suggestion_id: 'sug-comment',
      },
      message: {
        version: 1,
        message_id: messageId,
        content: 'Do it then',
        images: [],
        language: 'en',
        project_refs: [],
        files: [],
      },
    });

    // 送信レスポンスの action_id を取り込む。
    await act(async () => {});
    // 起動した会話は返ってきた action_id で購読される（提案 id では紐付かない）。
    const firstPage = createConversationUpdate(null, false, 'act-comment');
    firstPage.snapshot.page.runs[0].entries = [
      {
        step_kind: 'user',
        approved_suggestion: null,
        step_id: 'reply-1',
        step_number: 2,
        message_id: messageId,
        accepted_sequence: 1,
        content: 'Do it then',
        images: [],
        project_refs: [],
        status: 'adopted',
      },
      {
        step_kind: 'assistant',
        step_id: 'comment-1',
        step_number: 1,
        content: 'Just a comment, nothing to approve',
      },
    ];
    await act(async () => conversationListener?.(firstPage));
    expect(screen.getAllByText('Just a comment, nothing to approve')).toHaveLength(1);
    expect(screen.getByText('Do it then')).toBeInTheDocument();
    expect(screen.getByText('canonical final output')).toBeInTheDocument();
    submitMessage.mockResolvedValueOnce(createSubmittedResult('act-comment', 'reply-2'));
    fireEvent.change(screen.getByLabelText('Message'), { target: { value: 'And then?' } });
    fireEvent.click(screen.getByRole('button', { name: 'Send' }));
    expect(submitMessage.mock.calls[1][0].target).toEqual({
      kind: 'existing',
      action_id: 'act-comment',
      expected_process_id: null,
    });
    await act(async () =>
      conversationListener?.(createConversationUpdate('older', true, 'act-comment'))
    );
    expect(screen.queryByText('Just a comment, nothing to approve')).toBeNull();
  });

  it('approves with images and consent, blocks pending uploads and invalid instructions, and preserves a failed request', async () => {
    render(
      <UiLanguageProvider initialLanguage="en">
        <AgentOverlay />
      </UiLanguageProvider>
    );
    const snapshot = createCommentOnlySnapshot();
    snapshot.snapshot.interactionContract = 'action_offer';
    await act(async () => snapshotListener?.(snapshot));
    fireEvent.click(screen.getByRole('button', { name: 'Additional instructions (optional)' }));
    const input = screen.getByRole('textbox', { name: 'Additional instructions (optional)' });
    expect(input).toHaveFocus();
    fireEvent.change(input, { target: { value: '😀'.repeat(8_001) } });
    expect(screen.getByRole('button', { name: 'Accept' })).toBeDisabled();
    expect(screen.getByRole('alert')).toHaveTextContent('8,000');
    fireEvent.change(input, { target: { value: '  Use this image  ' } });
    fireEvent.click(screen.getByRole('button', { name: /permissions: Ask every time/ }));
    fireEvent.click(screen.getByRole('menuitemradio', { name: /Auto-approve/ }));
    let finishUpload!: (result: ActionImageAttachResult) => void;
    attachImage.mockReturnValueOnce(
      new Promise((resolve) => {
        finishUpload = resolve;
      })
    );
    fireEvent.change(document.querySelector('input[type="file"]')!, {
      target: { files: [new File(['png'], 'image.png', { type: 'image/png' })] },
    });
    expect(screen.getByRole('button', { name: 'Accept' })).toBeDisabled();
    fireEvent.keyDown(input, { key: 'Enter' });
    expect(acceptAction).not.toHaveBeenCalled();
    const storagePath = 'user-1/2026-09-11/11111111-1111-4111-8111-111111111111.png';
    // prettier-ignore
    await act(async () => finishUpload({ kind: 'attached', storagePath, mimeType: 'image/png', byteSize: 3, sha256: 'abc', widthPx: 1, heightPx: 1 }));
    acceptAction.mockRejectedValueOnce(new Error('Disconnected'));
    fireEvent.click(screen.getByRole('button', { name: 'Accept' }));
    await screen.findByText('Could not confirm this action. Try approving again.');
    fireEvent.click(screen.getByRole('button', { name: 'Additional instructions (optional)' }));
    const restored = screen.getByRole('textbox', { name: 'Additional instructions (optional)' });
    expect(restored).toHaveValue('  Use this image  ');
    expect(screen.getByRole('img', { name: 'Attached image 1 of 1' })).toBeInTheDocument();
    expect(restored).toHaveAttribute('readonly');
    expect(screen.queryByRole('button', { name: 'Add files' })).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'Accept' }));
    expect(acceptAction).toHaveBeenLastCalledWith({
      suggestionId: 'sug-comment',
      commandId: null,
      supplement: 'Use this image',
      supplementProjectRefs: [],
      approvalMode: 'always_allow',
      images: [{ kind: 'image', storage_path: storagePath }],
      files: [],
    });
    const accepted = createPendingSnapshot();
    accepted.snapshot.suggestionId = 'sug-comment';
    await act(async () => snapshotListener?.(accepted));
    await act(async () => conversationListener?.(createConversationUpdate()));
    expect(screen.getByRole('textbox', { name: 'Message' })).toHaveValue('');
    expect(screen.queryByRole('img', { name: 'Attached image 1 of 1' })).toBeNull();
  });

  it('keeps a reply open on snapshot refresh and collapses it for the next Suggestion', async () => {
    render(
      <UiLanguageProvider initialLanguage="en">
        <AgentOverlay />
      </UiLanguageProvider>
    );
    const snapshot = createCommentOnlySnapshot();
    await act(async () => snapshotListener?.(snapshot));
    fireEvent.click(screen.getByRole('button', { name: 'Reply to this suggestion' }));
    fireEvent.change(screen.getByRole('textbox', { name: 'Message' }), {
      target: { value: 'Keep this reply' },
    });
    snapshot.snapshot.lastSequence += 1;
    await act(async () => snapshotListener?.(snapshot));
    expect(screen.getByRole('textbox', { name: 'Message' })).toHaveValue('Keep this reply');
    let finishUpload!: (result: ActionImageAttachResult) => void;
    attachImage.mockReturnValueOnce(new Promise((resolve) => (finishUpload = resolve)));
    fireEvent.change(document.querySelector('input[type="file"]')!, {
      target: { files: [new File(['png'], 'image.png', { type: 'image/png' })] },
    });
    await waitFor(() => expect(attachImage).toHaveBeenCalled());
    snapshot.snapshot.suggestionId = 'sug-next';
    await act(async () => snapshotListener?.(snapshot));
    expect(screen.queryByRole('textbox', { name: 'Message' })).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'Reply to this suggestion' }));
    // prettier-ignore
    await act(async () => finishUpload({ kind: 'attached', storagePath: 'user-1/2026-09-11/11111111-1111-4111-8111-111111111111.png', mimeType: 'image/png', byteSize: 3, sha256: 'abc', widthPx: 1, heightPx: 1 }));
    expect(screen.getByRole('textbox', { name: 'Message' })).toHaveValue('');
    expect(screen.queryByRole('img', { name: 'Attached image 1 of 1' })).toBeNull();
    fireEvent.change(screen.getByRole('textbox', { name: 'Message' }), {
      target: { value: 'Fresh reply' },
    });
    expect(screen.getByRole('button', { name: 'Send' })).toBeEnabled();
  });

  it.each([
    ['en', null],
    ['en', 'Include risks'],
    ['ja', null],
    ['ja', '影響も確認して'],
  ] as const)(
    'shows the history proposal in the header above the conversation in %s (%s)',
    async (language, comment) => {
      retainedConversationUpdate = withApprovedSuggestion(
        createConversationUpdate(),
        comment,
        '[Review the changes](https://example.com)'
      );
      if (comment === null) {
        retainedConversationUpdate.snapshot.page.runs[0].entries = [];
        retainedConversationUpdate.snapshot.page.next_cursor = 'approval-in-older-history';
        readConversationPage.mockRejectedValueOnce(new Error('older page offline'));
      }
      render(
        <UiLanguageProvider initialLanguage={language}>
          <AgentOverlay entryMode="standalone" initialActionId="act-1" />
        </UiLanguageProvider>
      );
      const link = await screen.findByRole('link', { name: 'Review the changes' });
      const conversation = screen.getByRole('region', {
        name: language === 'ja' ? 'アクションの会話' : 'Action conversation',
      });
      expect(conversation).not.toContainElement(link);
      expect(
        link.compareDocumentPosition(conversation) & Node.DOCUMENT_POSITION_FOLLOWING
      ).toBeTruthy();
      expect(screen.getAllByText(language === 'ja' ? '承認済み' : 'Approved')).toHaveLength(1);
      const bubble = screen.queryByRole('article', { name: language === 'ja' ? 'あなた' : 'You' });
      if (comment === null) expect(bubble).toBeNull();
      else expect(bubble).toHaveTextContent(comment);
    }
  );

  it.each([null, 'Include risks'])(
    'keeps one approved proposal and its link focus when the page arrives (%s)',
    async (comment) => {
      render(
        <UiLanguageProvider initialLanguage="en">
          <AgentOverlay />
        </UiLanguageProvider>
      );
      const accepted = createPendingSnapshot();
      accepted.snapshot.suggestionText = '[Review the changes](https://example.com)';
      await act(async () => snapshotListener?.(accepted));
      fireEvent.click(screen.getByRole('button', { name: 'Expand' }));
      const proposalLink = screen.getByRole('link', { name: 'Review the changes' });
      proposalLink.focus();
      expect(screen.getByText('Approved')).toBeInTheDocument();
      const update = withApprovedSuggestion(
        createConversationUpdate(null, true),
        comment,
        accepted.snapshot.suggestionText
      );
      await act(async () => conversationListener?.(update));
      expect(screen.getByRole('link', { name: 'Review the changes' })).toBe(proposalLink);
      expect(proposalLink).toHaveFocus();
      expect(screen.getAllByText('Approved')).toHaveLength(1);
      const bubble = screen.queryByRole('article', { name: 'You' });
      if (comment === null) expect(bubble).toBeNull();
      else expect(bubble).toHaveTextContent(comment);

      const followup = structuredClone(update);
      followup.snapshot.pageVersion += 1;
      followup.snapshot.page.runs[0].entries.unshift({
        step_kind: 'user',
        step_id: 'followup',
        step_number: 2,
        message_id: 'followup-message',
        accepted_sequence: 2,
        content: accepted.snapshot.suggestionText,
        images: [],
        project_refs: [],
        status: 'adopted',
        approved_suggestion: null,
      });
      await act(async () => conversationListener?.(followup));
      const bubbles = screen.getAllByRole('article', { name: 'You' });
      expect(bubbles[bubbles.length - 1]).toHaveTextContent(accepted.snapshot.suggestionText);
      expect(screen.getAllByText('Approved')).toHaveLength(1);
    }
  );

  it('keeps the composer closed until an accepted action delivers its conversation page', async () => {
    const messageId = '00000000-0000-4000-8000-000000000043';
    vi.spyOn(crypto, 'randomUUID').mockReturnValue(messageId);
    submitMessage.mockResolvedValue(createSubmittedResult('act-1', messageId));
    render(
      <UiLanguageProvider initialLanguage="en">
        <AgentOverlay />
      </UiLanguageProvider>
    );
    const accepted = createResumedSnapshot();
    // prettier-ignore
    Object.assign(accepted.snapshot, { actionPhase: 'accepted_pending_start', actionStatus: 'idle', processId: null });
    await act(async () => snapshotListener?.(accepted));

    // 会話ページが届くまでは入力欄を出さない。ここで送ると別アクションが作られてしまう。
    expect(screen.getByText('Need approval')).toBeInTheDocument();
    expect(screen.queryByLabelText('Message')).toBeNull();
    expect(screen.queryByRole('button', { name: 'Send' })).toBeNull();

    await act(async () => conversationListener?.(createConversationUpdate(null, true)));
    fireEvent.click(screen.getByRole('button', { name: 'Expand' }));
    fireEvent.change(screen.getByLabelText('Message'), { target: { value: 'Continue' } });
    fireEvent.click(screen.getByRole('button', { name: 'Send' }));

    expect(submitMessage).toHaveBeenCalledTimes(1);
    expect(submitMessage.mock.calls[0][0].target).toEqual({
      kind: 'existing',
      action_id: 'act-1',
      expected_process_id: 'run-1',
    });
  });

  it.each([
    ['standalone', 'error'],
    ['standalone', 'timeout'],
    ['standalone', 'canceled'],
    ['standalone', 'success'],
    ['overlay', 'error'],
    ['overlay', 'timeout'],
    ['overlay', 'canceled'],
    ['overlay', 'success'],
  ] as const)(
    'stops %s controls on %s before the conversation refresh succeeds',
    async (entryMode, status) => {
      const messageId = '00000000-0000-4000-8000-000000000091';
      vi.spyOn(crypto, 'randomUUID').mockReturnValueOnce(messageId);
      submitMessage.mockResolvedValueOnce(createStartedResult('act-1', messageId));
      render(
        <UiLanguageProvider initialLanguage="en">
          <AgentOverlay
            entryMode={entryMode}
            initialActionId={entryMode === 'standalone' ? 'act-1' : null}
          />
        </UiLanguageProvider>
      );
      if (entryMode === 'overlay')
        await act(async () => snapshotListener?.(createResumedSnapshot()));
      const running = createConversationUpdate(null, true);
      await act(async () => conversationListener?.(running));
      fireEvent.click(screen.getByRole('button', { name: 'Expand' }));
      expect(screen.getByRole('button', { name: 'Stop' })).toBeEnabled();
      expect(screen.getAllByText('Running').length).toBeGreaterThan(0);

      const stopped = {
        ...running,
        snapshot: { ...running.snapshot, lifecycle: { processId: 'run-1', status } },
      };
      await act(async () => conversationListener?.(stopped));
      expect(screen.queryByRole('button', { name: 'Stop' })).toBeNull();
      expect(screen.queryByText('Running')).toBeNull();
      if (status === 'error' || status === 'timeout')
        expect(screen.getByText('An error occurred and execution stopped.')).toBeInTheDocument();
      readConversationPage.mockRejectedValueOnce(new Error('offline'));
      fireEvent.click(screen.getByRole('button', { name: 'Refresh conversation' }));
      await screen.findByRole('button', { name: 'Refresh failed. Try again.' });
      expect(screen.queryByText('Running')).toBeNull();

      fireEvent.change(screen.getByLabelText('Message'), {
        target: { value: 'Continue after recovery' },
      });
      fireEvent.click(screen.getByRole('button', { name: 'Send' }));
      await waitFor(() => expect(submitMessage).toHaveBeenCalledTimes(1));
      expect(submitMessage.mock.calls[0][0].target).toEqual({
        kind: 'existing',
        action_id: 'act-1',
        expected_process_id: null,
      });
    }
  );

  it('uses the next live run instead of a stale terminal page for Stop and steering', async () => {
    const messageId = '00000000-0000-4000-8000-000000000092';
    vi.spyOn(crypto, 'randomUUID').mockReturnValueOnce(messageId);
    submitMessage.mockResolvedValueOnce(createSubmittedResult('act-1', messageId));
    render(
      <UiLanguageProvider initialLanguage="en">
        <AgentOverlay entryMode="standalone" initialActionId="act-1" />
      </UiLanguageProvider>
    );
    const previous = createStoppedUpdate();
    await act(async () => conversationListener?.(previous));
    fireEvent.click(screen.getByRole('button', { name: 'Expand' }));
    const started = {
      ...previous,
      snapshot: {
        ...previous.snapshot,
        lifecycle: { processId: 'run-2', status: 'processing' as const },
      },
    };
    await act(async () => conversationListener?.(started));
    expect(screen.queryByRole('button', { name: 'Resume' })).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'Stop' }));
    expect(sendOrchestration).toHaveBeenLastCalledWith({
      event: 'stop_process',
      data: { process_id: 'run-2' },
    });
    fireEvent.change(screen.getByLabelText('Message'), {
      target: { value: 'Update the current work' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Send' }));
    await waitFor(() => expect(submitMessage).toHaveBeenCalledTimes(1));
    expect(submitMessage.mock.calls[0][0].target).toEqual({
      kind: 'existing',
      action_id: 'act-1',
      expected_process_id: 'run-2',
    });
  });

  it('replaces temporary failure feedback with the refreshed result without affecting another conversation', async () => {
    render(
      <UiLanguageProvider initialLanguage="en">
        <AgentOverlay entryMode="standalone" initialActionId="act-1" />
      </UiLanguageProvider>
    );
    const running = createConversationUpdate(null, true);
    await act(async () => conversationListener?.(running));
    fireEvent.click(screen.getByRole('button', { name: 'Expand' }));
    await act(async () =>
      conversationListener?.({
        ...running,
        snapshot: {
          ...running.snapshot,
          actionId: 'another-action',
          lifecycle: { processId: 'run-1', status: 'error' },
        },
      })
    );
    expect(screen.getByRole('button', { name: 'Stop' })).toBeEnabled();
    await act(async () =>
      conversationListener?.({
        ...running,
        snapshot: { ...running.snapshot, lifecycle: { processId: 'run-1', status: 'error' } },
      })
    );
    expect(screen.getByText('An error occurred and execution stopped.')).toBeInTheDocument();
    readConversationPage.mockResolvedValueOnce(createStoppedUpdate().snapshot.page);
    const refresh = screen.getByRole('button', { name: 'Refresh conversation' });
    refresh.focus();
    fireEvent.click(refresh);
    expect(await screen.findByText('stopped')).toBeInTheDocument();
    await waitFor(() => expect(screen.getByLabelText('Message')).toHaveFocus());
    expect(screen.queryByText('An error occurred and execution stopped.')).toBeNull();
    expect(screen.getByRole('button', { name: 'Resume' })).toBeEnabled();
  });

  it('states the run did not succeed until the canonical outcome arrives', async () => {
    render(
      <UiLanguageProvider initialLanguage="en">
        <AgentOverlay />
      </UiLanguageProvider>
    );
    await act(async () => snapshotListener?.(createResumedSnapshot()));
    await act(async () => conversationListener?.(createConversationUpdate(null, true)));

    const failed = createResumedSnapshot();
    // prettier-ignore
    Object.assign(failed.snapshot, { lastSequence: 12, actionPhase: 'terminal', actionStatus: 'error', processId: null });
    await act(async () => snapshotListener?.(failed));

    expect(screen.getByText('An unexpected error occurred.')).toBeInTheDocument();
    // 終了状態のラベル（バッジ）は持たない。地の文だけで伝える。
    expect(screen.queryByText('Failed')).toBeNull();

    const canonicalFailure = createConversationUpdate();
    Object.assign(canonicalFailure.snapshot.page.action, { status: 'error' });
    // prettier-ignore
    Object.assign(canonicalFailure.snapshot.page.runs[0], { status: 'error', final_output: null, error: { code: 'run_failed', message: 'The agent stopped before finishing.' } });
    await act(async () => conversationListener?.(canonicalFailure));

    expect(screen.getByText('The agent stopped before finishing.')).toBeInTheDocument();
    expect(screen.queryByText('An unexpected error occurred.')).toBeNull();
  });

  it('shows thinking only until the current run shows its first tool', async () => {
    render(
      <UiLanguageProvider initialLanguage="en">
        <AgentOverlay />
      </UiLanguageProvider>
    );
    // 採用直後で会話ページがまだ届いていない間が、いちばん長い無言になる。
    await act(async () => snapshotListener?.(createResumedSnapshot()));
    expect(screen.getByText('Thinking')).toBeInTheDocument();

    await act(async () => conversationListener?.(createConversationUpdate(null, true)));
    expect(screen.getByText('Thinking')).toBeInTheDocument();

    // ツールは一瞬で終わる。最初のツールが出たら、以降は作業の件数が進みを示す。
    const withTool = createConversationUpdate(null, true);
    // prettier-ignore
    withTool.snapshot.page.runs[0].entries.push({ step_kind: 'tool', step_id: 'step-2', step_number: 2, label: 'Read file', status: 'success', outcome: 'completed', output_available: false, images: [], subject: null, output_preview: null });
    await act(async () => conversationListener?.(withTool));
    expect(screen.queryByText('Thinking')).toBeNull();

    // 続けて送った依頼は新しい実行になり、その最初のツールまでまた出す。
    const nextRun = createConversationUpdate(null, true);
    nextRun.snapshot.page.action.latest_run_id = 'run-2';
    nextRun.snapshot.page.runs[0].run_id = 'run-2';
    await act(async () => conversationListener?.(nextRun));
    expect(screen.getByText('Thinking')).toBeInTheDocument();

    await act(async () => conversationListener?.(createConversationUpdate()));
    expect(screen.getByText('canonical final output')).toBeInTheDocument();
    expect(screen.queryByText('Thinking')).toBeNull();
  });

  it('scrolls the newest line instead of the enclosing run', async () => {
    render(
      <UiLanguageProvider initialLanguage="en">
        <AgentOverlay />
      </UiLanguageProvider>
    );
    const update = createConversationUpdate();
    update.snapshot.page.runs[0].entries.push(
      // prettier-ignore
      { step_kind: 'tool', step_id: 'step-2', step_number: 2, label: 'Read file', status: 'success', outcome: 'completed', output_available: false, images: [], subject: null, output_preview: null },
      // prettier-ignore
      { step_kind: 'user', approved_suggestion: null, step_id: 'step-1', step_number: 1, message_id: '00000000-0000-4000-8000-000000000044', accepted_sequence: 1, content: 'Do the thing', images: [], project_refs: [], status: 'adopted' }
    );
    await act(async () => snapshotListener?.(createResumedSnapshot()));
    await act(async () => conversationListener?.(update));

    const scrollIntoView = vi.mocked(Element.prototype.scrollIntoView);
    await waitFor(() => expect(scrollIntoView).toHaveBeenCalled());
    // run 全体（li）ではなく run の中の最後の行を、末尾合わせで表示する。
    const target = scrollIntoView.mock.instances[scrollIntoView.mock.instances.length - 1];
    expect(target).toHaveClass('action-conversation__outcome');
    expect(target).toHaveTextContent('canonical final output');
    expect(scrollIntoView).toHaveBeenLastCalledWith({ block: 'end' });
  });

  it('follows run progress only from the bottom, and shows what the user sends', async () => {
    submitMessage.mockReturnValueOnce(new Promise(() => {}));
    const { container } = render(
      <UiLanguageProvider initialLanguage="en">
        <AgentOverlay />
      </UiLanguageProvider>
    );
    await act(async () => snapshotListener?.(createResumedSnapshot()));
    const scroll = container.querySelector<HTMLDivElement>('[data-overlay-scroll]')!;
    const scrollHeight = { value: 1000, configurable: true };
    Object.defineProperties(scroll, { scrollHeight, clientHeight: { value: 200 } });
    const userScrollsTo = (top: number) => {
      scroll.scrollTop = top;
      fireEvent.scroll(scroll);
    };
    const progress = (label: string, runId = 'run-1') => {
      const update = createConversationUpdate(null, true);
      update.snapshot.page.action.latest_run_id = runId;
      update.snapshot.page.runs[0].run_id = runId;
      // prettier-ignore
      update.snapshot.page.runs[0].entries.push({ step_kind: 'tool', step_id: `step-${label}`, step_number: 2, label, status: 'success', outcome: 'completed', output_available: false, images: [], subject: null, output_preview: null });
      Object.defineProperty(scroll, 'scrollHeight', { ...scrollHeight, value: 1000 + pageVersion });
      return act(async () => conversationListener?.(update));
    };
    await progress('Read file');
    fireEvent.click(screen.getByRole('button', { name: 'Expand' }));
    expect(scroll.scrollTop).toBe(scroll.scrollHeight);

    userScrollsTo(300);
    await progress('Edit file');
    // A run that starts by itself (a queued message taking over) does not pull the reader back either.
    await progress('Run tests', 'run-2');
    expect(scroll.scrollTop).toBe(300);

    userScrollsTo(scroll.scrollHeight - 200);
    await progress('Write summary');
    expect(scroll.scrollTop).toBe(scroll.scrollHeight);

    userScrollsTo(300);
    fireEvent.change(screen.getByLabelText('Message'), { target: { value: 'Also update docs' } });
    fireEvent.click(screen.getByRole('button', { name: 'Send' }));
    expect(scroll.scrollTop).toBe(scroll.scrollHeight);
  });

  it('ignores a late decision failure after the approval process changes', async () => {
    let rejectDecision: (error: Error) => void = () => {};
    submitApprovalDecision.mockImplementationOnce(
      () =>
        new Promise((_resolve, reject) => {
          rejectDecision = reject;
        })
    );
    render(
      <UiLanguageProvider initialLanguage="en">
        <AgentOverlay />
      </UiLanguageProvider>
    );
    const first = withApprovalBlocker(createConversationUpdate(null, true));
    await act(async () => snapshotListener?.(createPendingSnapshot()));
    await act(async () => conversationListener?.(first));
    fireEvent.click(screen.getByRole('button', { name: 'Expand' }));
    fireEvent.click(screen.getByRole('button', { name: 'Approve' }));
    await waitFor(() => expect(submitApprovalDecision).toHaveBeenCalledTimes(1));
    await act(async () =>
      conversationListener?.(withApprovalBlocker(createConversationUpdate(null, true), 'run-2'))
    );
    await act(async () => rejectDecision(new Error('stale failure')));
    expect(
      screen.queryByText('Failed to submit approval decision. Refresh and try again.')
    ).toBeNull();
  });
  it('sends on Enter but leaves newlines and IME confirmation alone', async () => {
    const messageId = '00000000-0000-4000-8000-000000000099';
    vi.spyOn(crypto, 'randomUUID').mockReturnValue(messageId);
    submitMessage.mockResolvedValue(createSubmittedResult('act-1', messageId));
    render(
      <UiLanguageProvider initialLanguage="en">
        <AgentOverlay />
      </UiLanguageProvider>
    );
    await act(async () => snapshotListener?.(createResumedSnapshot()));
    await act(async () => conversationListener?.(createConversationUpdate(null, true)));
    fireEvent.click(screen.getByRole('button', { name: 'Expand' }));

    const composer = await screen.findByLabelText('Message');
    expect(composer).toBeEnabled();
    fireEvent.change(composer, { target: { value: 'Continue' } });

    // 変換確定の Enter で送ると、書きかけの日本語がそのまま飛んでしまう。
    fireEvent.keyDown(composer, { key: 'Enter', isComposing: true });
    // Shift+Enter は改行なので、textarea の既定動作に任せる。
    fireEvent.keyDown(composer, { key: 'Enter', shiftKey: true });
    expect(submitMessage).not.toHaveBeenCalled();

    fireEvent.keyDown(composer, { key: 'Enter' });
    await waitFor(() => expect(submitMessage).toHaveBeenCalledTimes(1));
  });

  const pngFile = (name: string, size = 4) =>
    new File([new Uint8Array(size)], name, { type: 'image/png' });

  async function openComposer() {
    render(
      <UiLanguageProvider initialLanguage="en">
        <AgentOverlay />
      </UiLanguageProvider>
    );
    await act(async () => snapshotListener?.(createResumedSnapshot()));
    await act(async () => conversationListener?.(createConversationUpdate(null, true)));
    fireEvent.click(screen.getByRole('button', { name: 'Expand' }));
    return screen.getByLabelText('Message');
  }

  it('sends pasted and dropped images as storage-path references and can drop one first', async () => {
    const messageId = '00000000-0000-4000-8000-000000000011';
    vi.spyOn(crypto, 'randomUUID').mockReturnValue(messageId);
    submitMessage.mockResolvedValue(createSubmittedResult('act-1', messageId));
    const composer = await openComposer();

    await act(async () => {
      fireEvent.paste(composer, { clipboardData: { files: [pngFile('pasted.png')] } });
    });
    await act(async () => {
      fireEvent.drop(composer.closest('form')!, {
        dataTransfer: { files: [pngFile('dropped.png')] },
      });
    });

    const attachments = await screen.findByRole('list', { name: 'Attachments, 2' });
    const thumbnails = within(attachments).getAllByRole('img');
    expect(thumbnails.map((image) => image.getAttribute('alt'))).toEqual([
      'Attached image 1 of 2',
      'Attached image 2 of 2',
    ]);
    // An intrinsic box keeps a late decode from moving the composer, and so the overlay window.
    expect(thumbnails[0]).toHaveAttribute('width', '96');
    expect(thumbnails[0]).toHaveAttribute('height', '96');
    expect(thumbnails[0].getAttribute('src')).toMatch(/^pantaray-image:\/\/local\//);
    expect(attachImage.mock.calls.map(([request]) => request.declaredMimeType)).toEqual([
      'image/png',
      'image/png',
    ]);

    fireEvent.click(
      within(attachments).getByRole('button', { name: 'Remove attached image 1 of 2' })
    );
    fireEvent.change(composer, { target: { value: 'Look at this' } });
    fireEvent.click(screen.getByRole('button', { name: 'Send' }));

    expect(submitMessage.mock.calls[0][0].message.images).toEqual([
      { kind: 'image', storage_path: expect.stringMatching(/^user-1\/2026-09-08\/2/) },
    ]);
    expect(screen.getByRole('list', { name: /^Attachments/ })).toBeVisible();
    expect(screen.getByLabelText('Message')).toHaveValue('Look at this');
    expect(screen.queryByRole('button', { name: /Remove attached image/ })).toBeNull();
  });

  it('explains every refusal and never sends a file the main process declined', async () => {
    const composer = await openComposer();

    await act(async () => {
      fireEvent.paste(composer, {
        clipboardData: { files: [new File(['x'], 'notes.txt', { type: 'text/plain' })] },
      });
    });
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'You can attach PNG, JPEG, GIF, and WebP images, and PDF, Word, Excel, PowerPoint, and notebook files.'
    );
    expect(attachImage).not.toHaveBeenCalled();
    expect(attachFile).not.toHaveBeenCalled();

    await act(async () => {
      fireEvent.paste(composer, {
        clipboardData: { files: [pngFile('huge.png', 8_000_001)] },
      });
    });
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Each image must be 8 MB or smaller.'
    );
    expect(attachImage).not.toHaveBeenCalled();

    attachImage.mockResolvedValueOnce({ kind: 'rejected', reason: 'decode_failed' });
    await act(async () => {
      fireEvent.paste(composer, { clipboardData: { files: [pngFile('broken.png')] } });
    });
    expect(await screen.findByRole('alert')).toHaveTextContent('That image could not be read.');
    expect(screen.queryByRole('list', { name: /^Attachments/ })).toBeNull();
  });

  it('stops at the per-message image limit', async () => {
    const composer = await openComposer();

    await act(async () => {
      fireEvent.paste(composer, {
        clipboardData: { files: Array.from({ length: 11 }, (_, index) => pngFile(`${index}.png`)) },
      });
    });

    expect(await screen.findByRole('list', { name: 'Attachments, 10' })).toBeVisible();
    expect(screen.getByRole('alert')).toHaveTextContent(
      'Up to 10 images can be sent in one message.'
    );
    expect(attachImage).toHaveBeenCalledTimes(10);
    // Documents have their own limit, so the picker stays open for them.
    expect(screen.getByRole('button', { name: 'Add files' })).toBeEnabled();
  });

  const documentFile = (name: string, size = 4, type = 'application/pdf') =>
    new File([new Uint8Array(size)], name, { type });

  it('routes documents by extension, a notebook without a MIME type included, and sends them as files', async () => {
    const messageId = '00000000-0000-4000-8000-000000000014';
    vi.spyOn(crypto, 'randomUUID').mockReturnValue(messageId);
    submitMessage.mockResolvedValue(createSubmittedResult('act-1', messageId));
    const composer = await openComposer();

    await act(async () => {
      fireEvent.drop(composer.closest('form')!, {
        dataTransfer: {
          files: [
            documentFile('Report.PDF', 1_258_291),
            documentFile('analysis.ipynb', 2048, ''),
            pngFile('chart.png'),
          ],
        },
      });
    });

    const attachments = await screen.findByRole('list', { name: 'Attachments, 3' });
    expect(attachFile.mock.calls.map(([request]) => request.name)).toEqual([
      'Report.PDF',
      'analysis.ipynb',
    ]);
    expect(attachImage).toHaveBeenCalledTimes(1);
    expect(within(attachments).getByText('Report.PDF')).toBeVisible();
    expect(within(attachments).getByText('1.2 MB')).toBeVisible();
    expect(within(attachments).getByRole('img', { name: 'Attached image 1 of 1' })).toBeVisible();
    expect(screen.getByRole('button', { name: 'Remove analysis.ipynb' })).toBeVisible();

    fireEvent.change(composer, { target: { value: 'Summarize these' } });
    fireEvent.click(screen.getByRole('button', { name: 'Send' }));

    const { message } = submitMessage.mock.calls[0][0];
    expect(message.files).toEqual([
      {
        attachment_id: '12222222-2222-4222-8222-222222222222',
        name: 'Report.PDF',
        byte_size: 1_258_291,
      },
      {
        attachment_id: '22222222-2222-4222-8222-222222222222',
        name: 'analysis.ipynb',
        byte_size: 2048,
      },
    ]);
    expect(message.images).toHaveLength(1);
  });

  it('refuses an oversized document and an eleventh one without sending them to the main process', async () => {
    const composer = await openComposer();
    const oversized = documentFile('scan.pdf');
    Object.defineProperty(oversized, 'size', { value: 20 * 1024 * 1024 + 1 });

    await act(async () => {
      fireEvent.paste(composer, { clipboardData: { files: [oversized] } });
    });
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Each PDF, Word, Excel, PowerPoint, or notebook file must be 20 MB or smaller.'
    );
    expect(attachFile).not.toHaveBeenCalled();

    await act(async () => {
      fireEvent.paste(composer, {
        clipboardData: {
          files: Array.from({ length: 11 }, (_, index) => documentFile(`${index}.docx`)),
        },
      });
    });
    expect(await screen.findByRole('list', { name: 'Attachments, 10' })).toBeVisible();
    expect(screen.getByRole('alert')).toHaveTextContent(
      'Up to 10 files can be sent in one message.'
    );
    expect(attachFile).toHaveBeenCalledTimes(10);

    attachFile.mockRejectedValueOnce(new Error('ipc failed'));
    fireEvent.click(screen.getByRole('button', { name: 'Remove 0.docx' }));
    await act(async () => {
      fireEvent.paste(composer, { clipboardData: { files: [documentFile('broken.xlsx')] } });
    });
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'The file could not be attached. Try again.'
    );
    expect(screen.getByRole('list', { name: 'Attachments, 9' })).toBeVisible();
  });

  it('carries an attached document with the approval', async () => {
    render(
      <UiLanguageProvider initialLanguage="en">
        <AgentOverlay />
      </UiLanguageProvider>
    );
    const snapshot = createCommentOnlySnapshot();
    snapshot.snapshot.interactionContract = 'action_offer';
    await act(async () => snapshotListener?.(snapshot));
    fireEvent.click(screen.getByRole('button', { name: 'Additional instructions (optional)' }));
    await act(async () => {
      fireEvent.change(document.querySelector('input[type="file"]')!, {
        target: { files: [documentFile('brief.docx', 10)] },
      });
    });
    await screen.findByRole('button', { name: 'Remove brief.docx' });

    fireEvent.click(screen.getByRole('button', { name: 'Accept' }));

    expect(acceptAction).toHaveBeenLastCalledWith(
      expect.objectContaining({
        files: [
          {
            attachment_id: '12222222-2222-4222-8222-222222222222',
            name: 'brief.docx',
            byte_size: 10,
          },
        ],
      })
    );
    // The accepted snapshot replaces the composer; the document now belongs to the Action.
    const accepted = createPendingSnapshot();
    accepted.snapshot.suggestionId = 'sug-comment';
    await act(async () => snapshotListener?.(accepted));
    expect(discardAttachment).not.toHaveBeenCalled();
  });

  it('discards staged documents when the next suggestion replaces the composer', async () => {
    render(
      <UiLanguageProvider initialLanguage="en">
        <AgentOverlay />
      </UiLanguageProvider>
    );
    const snapshot = createCommentOnlySnapshot();
    await act(async () => snapshotListener?.(snapshot));
    fireEvent.click(screen.getByRole('button', { name: 'Reply to this suggestion' }));
    await act(async () => {
      fireEvent.change(document.querySelector('input[type="file"]')!, {
        target: { files: [documentFile('notes.pdf')] },
      });
    });
    await screen.findByRole('button', { name: 'Remove notes.pdf' });

    snapshot.snapshot.suggestionId = 'sug-next';
    await act(async () => snapshotListener?.(snapshot));
    expect(discardAttachment).toHaveBeenCalledWith({
      attachmentId: '12222222-2222-4222-8222-222222222222',
    });

    // A document still being staged when the composer is replaced is discarded when it lands.
    fireEvent.click(screen.getByRole('button', { name: 'Reply to this suggestion' }));
    let finishAttach!: (result: unknown) => void;
    attachFile.mockReturnValueOnce(new Promise((resolve) => (finishAttach = resolve)));
    fireEvent.change(document.querySelector('input[type="file"]')!, {
      target: { files: [documentFile('late.pdf')] },
    });
    await waitFor(() => expect(attachFile).toHaveBeenCalledTimes(2));
    snapshot.snapshot.suggestionId = 'sug-third';
    await act(async () => snapshotListener?.(snapshot));
    const lateId = '33333333-3333-4333-8333-333333333333';
    await act(async () => finishAttach({ attachmentId: lateId, name: 'late.pdf', byteSize: 4 }));
    expect(discardAttachment).toHaveBeenLastCalledWith({ attachmentId: lateId });
    expect(discardAttachment).toHaveBeenCalledTimes(2);
  });

  it('leaves a document that went out with a reply to the backend when the composer is replaced', async () => {
    submitMessage.mockReturnValue(new Promise(() => {}));
    render(
      <UiLanguageProvider initialLanguage="en">
        <AgentOverlay />
      </UiLanguageProvider>
    );
    const snapshot = createCommentOnlySnapshot();
    await act(async () => snapshotListener?.(snapshot));
    fireEvent.click(screen.getByRole('button', { name: 'Reply to this suggestion' }));
    await act(async () => {
      fireEvent.change(document.querySelector('input[type="file"]')!, {
        target: { files: [documentFile('notes.pdf')] },
      });
    });
    await screen.findByRole('button', { name: 'Remove notes.pdf' });
    fireEvent.change(screen.getByRole('textbox', { name: 'Message' }), {
      target: { value: 'Read this' },
    });
    await waitFor(() => expect(screen.getByRole('button', { name: 'Send' })).toBeEnabled());
    fireEvent.click(screen.getByRole('button', { name: 'Send' }));
    expect(submitMessage.mock.calls[0][0].message.files).toHaveLength(1);

    snapshot.snapshot.suggestionId = 'sug-next';
    await act(async () => snapshotListener?.(snapshot));
    expect(discardAttachment).not.toHaveBeenCalled();
  });

  it('discards the documents a concurrent batch pushes past the per-message limit', async () => {
    const composer = await openComposer();
    let finishFirst!: (result: unknown) => void;
    attachFile.mockReturnValueOnce(new Promise((resolve) => (finishFirst = resolve)));
    const batch = (prefix: string) =>
      Array.from({ length: 6 }, (_, index) => documentFile(`${prefix}${index}.pdf`));

    await act(async () => {
      fireEvent.paste(composer, { clipboardData: { files: batch('first-') } });
    });
    await act(async () => {
      fireEvent.paste(composer, { clipboardData: { files: batch('second-') } });
    });
    expect(await screen.findByRole('list', { name: 'Attachments, 6' })).toBeVisible();
    await act(async () =>
      finishFirst({
        attachmentId: '44444444-4444-4444-8444-444444444444',
        name: 'first-0.pdf',
        byteSize: 4,
      })
    );

    expect(await screen.findByRole('list', { name: 'Attachments, 10' })).toBeVisible();
    expect(screen.getByRole('alert')).toHaveTextContent(
      'Up to 10 files can be sent in one message.'
    );
    expect(screen.queryByText('first-4.pdf')).toBeNull();
    expect(screen.queryByText('first-5.pdf')).toBeNull();
    const dropped = attachFile.mock.results.slice(-2).map(async (result) => {
      const { attachmentId } = await (result.value as Promise<{ attachmentId: string }>);
      return { attachmentId };
    });
    expect(discardAttachment.mock.calls.map(([request]) => request)).toEqual(
      await Promise.all(dropped)
    );
  });

  it('discards a removed document and keeps keyboard focus in the composer', async () => {
    const messageId = '00000000-0000-4000-8000-000000000015';
    vi.spyOn(crypto, 'randomUUID').mockReturnValue(messageId);
    submitMessage.mockResolvedValue(createSubmittedResult('act-1', messageId));
    const composer = await openComposer();
    await act(async () => {
      fireEvent.paste(composer, {
        clipboardData: { files: [documentFile('a.pdf'), documentFile('b.pptx')] },
      });
    });

    const removeFirst = await screen.findByRole('button', { name: 'Remove a.pdf' });
    removeFirst.focus();
    fireEvent.click(removeFirst);
    expect(discardAttachment).toHaveBeenCalledWith({
      attachmentId: '12222222-2222-4222-8222-222222222222',
    });
    const removeSecond = screen.getByRole('button', { name: 'Remove b.pptx' });
    expect(removeSecond).toHaveFocus();
    fireEvent.click(removeSecond);
    expect(composer).toHaveFocus();
    expect(screen.queryByRole('list', { name: /^Attachments/ })).toBeNull();

    fireEvent.change(composer, { target: { value: 'Never mind' } });
    fireEvent.click(screen.getByRole('button', { name: 'Send' }));
    expect(submitMessage.mock.calls[0][0].message.files).toEqual([]);
  });

  it('cannot send while an image is still being written, so it lands on the message it was meant for', async () => {
    const messageId = '00000000-0000-4000-8000-000000000012';
    vi.spyOn(crypto, 'randomUUID').mockReturnValue(messageId);
    submitMessage.mockResolvedValue(createSubmittedResult('act-1', messageId));
    let finishAttach!: (result: unknown) => void;
    attachImage.mockReturnValueOnce(
      new Promise((resolve) => {
        finishAttach = resolve;
      })
    );
    const composer = await openComposer();

    fireEvent.change(composer, { target: { value: 'Look at this' } });
    await act(async () => {
      fireEvent.paste(composer, { clipboardData: { files: [pngFile('slow.png')] } });
    });

    // Sending now would clear the attachments and silently move this image onto the next message.
    const send = screen.getByRole('button', { name: 'Send' });
    await waitFor(() => expect(send).toBeDisabled());
    fireEvent.click(send);
    fireEvent.submit(composer.closest('form')!);
    expect(submitMessage).not.toHaveBeenCalled();

    await act(async () => {
      finishAttach({
        kind: 'attached',
        storagePath: 'user-1/2026-09-08/33333333-3333-4333-8333-333333333333.png',
        mimeType: 'image/png',
        byteSize: 4,
        sha256: 'abc',
        widthPx: 10,
        heightPx: 10,
      });
    });

    await waitFor(() => expect(send).toBeEnabled());
    fireEvent.click(send);

    expect(submitMessage.mock.calls[0][0].message.images).toEqual([
      {
        kind: 'image',
        storage_path: 'user-1/2026-09-08/33333333-3333-4333-8333-333333333333.png',
      },
    ]);
  });

  it('drops an in-flight attachment when the conversation resets, so it cannot follow an account switch', async () => {
    let finishAttach!: (result: unknown) => void;
    attachImage.mockReturnValueOnce(
      new Promise((resolve) => {
        finishAttach = resolve;
      })
    );
    const composer = await openComposer();

    await act(async () => {
      fireEvent.paste(composer, { clipboardData: { files: [pngFile('slow.png')] } });
    });
    // A subject change clears the conversation and the composer with it.
    await act(async () => conversationListener?.({ kind: 'reset' }));
    await act(async () => {
      finishAttach({
        kind: 'attached',
        // Written for the user who was signed in when the paste happened.
        storagePath: 'user-1/2026-09-08/44444444-4444-4444-8444-444444444444.png',
        mimeType: 'image/png',
        byteSize: 4,
        sha256: 'abc',
        widthPx: 10,
        heightPx: 10,
      });
    });

    expect(screen.queryByRole('list', { name: /^Attachments/ })).toBeNull();

    // The replacement composer must be usable, not stuck behind a leftover in-flight count.
    await act(async () => conversationListener?.(createConversationUpdate(null, true)));
    fireEvent.change(screen.getByLabelText('Message'), { target: { value: 'Fresh start' } });
    expect(screen.queryByRole('list', { name: /^Attachments/ })).toBeNull();
    expect(screen.getByRole('button', { name: 'Send' })).toBeEnabled();
  });

  it('keeps an attachment across live steps of a running Action', async () => {
    const messageId = '00000000-0000-4000-8000-000000000013';
    vi.spyOn(crypto, 'randomUUID').mockReturnValue(messageId);
    submitMessage.mockResolvedValue(createSubmittedResult('act-1', messageId));
    let finishAttach!: (result: unknown) => void;
    attachImage.mockReturnValueOnce(
      new Promise((resolve) => {
        finishAttach = resolve;
      })
    );
    const composer = await openComposer();

    await act(async () => {
      fireEvent.paste(composer, { clipboardData: { files: [pngFile('slow.png')] } });
    });
    // Queueing onto a running Action is a supported flow, and every tool step it emits updates
    // the conversation. That must not be mistaken for the composer being replaced.
    await act(async () => conversationListener?.(createConversationUpdate(null, true)));
    await act(async () => {
      finishAttach({
        kind: 'attached',
        storagePath: 'user-1/2026-09-08/55555555-5555-4555-8555-555555555555.png',
        mimeType: 'image/png',
        byteSize: 4,
        sha256: 'abc',
        widthPx: 10,
        heightPx: 10,
      });
    });

    expect(await screen.findByRole('list', { name: 'Attachments, 1' })).toBeVisible();
    fireEvent.change(screen.getByLabelText('Message'), { target: { value: 'Look at this' } });
    const send = screen.getByRole('button', { name: 'Send' });
    await waitFor(() => expect(send).toBeEnabled());
    fireEvent.click(send);

    expect(submitMessage.mock.calls[0][0].message.images).toEqual([
      {
        kind: 'image',
        storage_path: 'user-1/2026-09-08/55555555-5555-4555-8555-555555555555.png',
      },
    ]);
  });

  // 送信ボタンはひとつで、担う操作は入力欄の状態が決める。書きかけがあれば送信、
  // 空ならエージェントが動いていれば停止、直前に止めたままなら再開。
  it('turns the composer control into Stop while the agent runs, and Send once text exists', async () => {
    render(
      <UiLanguageProvider initialLanguage="en">
        <AgentOverlay />
      </UiLanguageProvider>
    );
    await act(async () => snapshotListener?.(createResumedSnapshot()));
    await act(async () => conversationListener?.(createConversationUpdate(null, true)));
    fireEvent.click(screen.getByRole('button', { name: 'Expand' }));

    const message = await screen.findByLabelText('Message');
    const box = message.closest('form.overlay-composer') as HTMLElement;
    fireEvent.click(within(box).getByRole('button', { name: 'Stop' }));
    expect(stopAction).toHaveBeenCalledTimes(1);
    // 止める先は会話が今名乗っている run。再開した run もここに現れる。
    expect(sendOrchestration).toHaveBeenCalledWith({
      event: 'stop_process',
      data: { process_id: 'run-1' },
    });

    fireEvent.change(message, { target: { value: 'more' } });
    expect(within(box).queryByRole('button', { name: 'Stop' })).toBeNull();
    expect(within(box).getByRole('button', { name: 'Send' })).toBeEnabled();
  });

  it('offers Resume on a stopped conversation and opens one follow-up turn', async () => {
    const messageId = '00000000-0000-4000-8000-0000000000ff';
    vi.spyOn(crypto, 'randomUUID').mockReturnValue(messageId);
    render(
      <UiLanguageProvider initialLanguage="en">
        <AgentOverlay />
      </UiLanguageProvider>
    );
    await act(async () => snapshotListener?.(createResumedSnapshot()));
    await act(async () => conversationListener?.(createStoppedUpdate()));
    fireEvent.click(screen.getByRole('button', { name: 'Expand' }));

    const message = await screen.findByLabelText('Message');
    const box = message.closest('form.overlay-composer') as HTMLElement;
    // 停止済みなので、押せる送信でも停止でもなく再開になる。
    expect(within(box).queryByRole('button', { name: 'Send' })).toBeNull();
    expect(within(box).queryByRole('button', { name: 'Stop' })).toBeNull();
    fireEvent.click(within(box).getByRole('button', { name: 'Resume' }));

    await waitFor(() =>
      expect(resumeAction).toHaveBeenCalledWith({ actionId: 'act-1', messageId })
    );
    // 「再開」は押しただけの操作で、会話に自分のメッセージを足さない。
    expect(submitMessage).not.toHaveBeenCalled();
    expect(box.textContent).not.toContain('Resume the');

    // 書きかけがあれば、止まっていても再開ではなく送信。
    fireEvent.change(message, { target: { value: 'do this instead' } });
    expect(within(box).queryByRole('button', { name: 'Resume' })).toBeNull();
    expect(within(box).getByRole('button', { name: 'Send' })).toBeEnabled();
  });

  it('says so when a Resume is refused instead of leaving the press unanswered', async () => {
    resumeAction.mockResolvedValue({ kind: 'action_conflict' });
    render(
      <UiLanguageProvider initialLanguage="en">
        <AgentOverlay />
      </UiLanguageProvider>
    );
    await act(async () => snapshotListener?.(createResumedSnapshot()));
    await act(async () => conversationListener?.(createStoppedUpdate()));
    fireEvent.click(screen.getByRole('button', { name: 'Expand' }));

    const message = await screen.findByLabelText('Message');
    const box = message.closest('form.overlay-composer') as HTMLElement;
    fireEvent.click(within(box).getByRole('button', { name: 'Resume' }));

    expect(
      await within(box).findByText('The conversation could not be resumed. Try again.')
    ).toBeVisible();
    fireEvent.change(message, { target: { value: 'type instead' } });
    expect(within(box).queryByText('The conversation could not be resumed. Try again.')).toBeNull();
  });

  it('retries a Resume whose response was lost with the same idempotency key', async () => {
    const messageId = '00000000-0000-4000-8000-0000000000fe';
    const uuid = vi.spyOn(crypto, 'randomUUID').mockReturnValue(messageId);
    resumeAction.mockRejectedValueOnce(new Error('transport'));
    render(
      <UiLanguageProvider initialLanguage="en">
        <AgentOverlay />
      </UiLanguageProvider>
    );
    await act(async () => snapshotListener?.(createResumedSnapshot()));
    await act(async () => conversationListener?.(createStoppedUpdate()));
    fireEvent.click(screen.getByRole('button', { name: 'Expand' }));

    const message = await screen.findByLabelText('Message');
    const box = message.closest('form.overlay-composer') as HTMLElement;
    fireEvent.click(within(box).getByRole('button', { name: 'Resume' }));
    await within(box).findByText('The conversation could not be resumed. Try again.');

    // 応答だけが失われた可能性がある。別の key で送り直すと、走り出した run と衝突する
    // だけで最初の応答には辿り着けないので、再試行は同じ key で送る。
    uuid.mockReturnValue('00000000-0000-4000-8000-0000000000aa');
    fireEvent.click(within(box).getByRole('button', { name: 'Resume' }));
    await waitFor(() => expect(resumeAction).toHaveBeenCalledTimes(2));
    expect(resumeAction.mock.calls.map(([request]) => request.messageId)).toEqual([
      messageId,
      messageId,
    ]);
  });

  it('keeps Resume locked after a success until the conversation stops offering it', async () => {
    render(
      <UiLanguageProvider initialLanguage="en">
        <AgentOverlay />
      </UiLanguageProvider>
    );
    await act(async () => snapshotListener?.(createResumedSnapshot()));
    await act(async () => conversationListener?.(createStoppedUpdate()));
    fireEvent.click(screen.getByRole('button', { name: 'Expand' }));

    const message = await screen.findByLabelText('Message');
    const box = message.closest('form.overlay-composer') as HTMLElement;
    fireEvent.click(within(box).getByRole('button', { name: 'Resume' }));
    await waitFor(() => expect(resumeAction).toHaveBeenCalledTimes(1));

    // 応答が返っても、正典の会話がまだ「再開できる」と言っている間は二度目を送らない。
    expect(within(box).getByRole('button', { name: 'Resume' })).toBeDisabled();
    fireEvent.click(within(box).getByRole('button', { name: 'Resume' }));
    expect(resumeAction).toHaveBeenCalledTimes(1);

    await act(async () => conversationListener?.(createConversationUpdate(null, true)));
    expect(within(box).queryByRole('button', { name: 'Resume' })).toBeNull();
  });

  it('ignores a Resume response that arrives after the conversation was reset', async () => {
    let rejectResume!: (error: Error) => void;
    resumeAction.mockReturnValueOnce(
      new Promise((_resolve, reject) => {
        rejectResume = reject;
      })
    );
    render(
      <UiLanguageProvider initialLanguage="en">
        <AgentOverlay />
      </UiLanguageProvider>
    );
    await act(async () => snapshotListener?.(createResumedSnapshot()));
    await act(async () => conversationListener?.(createStoppedUpdate()));
    fireEvent.click(screen.getByRole('button', { name: 'Expand' }));
    const stale = (await screen.findByLabelText('Message')).closest(
      'form.overlay-composer'
    ) as HTMLElement;
    fireEvent.click(within(stale).getByRole('button', { name: 'Resume' }));
    await waitFor(() => expect(resumeAction).toHaveBeenCalledTimes(1));

    // 入力欄が入れ替わったあとに届いた古い失敗は、新しい会話のものではない。
    await act(async () => conversationListener?.({ kind: 'reset' }));
    await act(async () => conversationListener?.(createStoppedUpdate()));
    await act(async () => rejectResume(new Error('late transport failure')));

    const fresh = (await screen.findByLabelText('Message')).closest(
      'form.overlay-composer'
    ) as HTMLElement;
    expect(
      within(fresh).queryByText('The conversation could not be resumed. Try again.')
    ).toBeNull();
    expect(within(fresh).getByRole('button', { name: 'Resume' })).toBeEnabled();
  });

  it('sends the chosen initial permissions and retains them on a transport retry', async () => {
    submitMessage.mockRejectedValue(new Error('response lost'));
    render(
      <UiLanguageProvider initialLanguage="en">
        <AgentOverlay entryMode="standalone" />
      </UiLanguageProvider>
    );
    fireEvent.click(await screen.findByRole('button', { name: /permissions: Ask every time/ }));
    fireEvent.click(screen.getByRole('menuitemradio', { name: /Auto-approve/ }));
    fireEvent.change(screen.getByLabelText('Message'), { target: { value: 'Run the command' } });
    fireEvent.click(screen.getByRole('button', { name: 'Send' }));
    const retry = await screen.findByRole('button', { name: 'Retry sending' });
    const request = submitMessage.mock.calls[0][0];
    expect(request.target).toEqual({ kind: 'new', approval_mode: 'always_allow' });
    await act(async () => fireEvent.click(retry));
    expect(submitMessage.mock.calls[1][0]).toEqual(request);
  });
  it('recovers initial permission loading without losing the draft or submitting unknown consent', async () => {
    submitMessage.mockResolvedValue(createStartedResult('new-action', 'message-id'));
    getWorkspaceEditCommandPreference
      .mockRejectedValueOnce(new Error('connection interrupted'))
      .mockResolvedValue(defaultPermissions);
    render(
      <UiLanguageProvider initialLanguage="en">
        <AgentOverlay entryMode="standalone" />
      </UiLanguageProvider>
    );
    fireEvent.change(screen.getByLabelText('Message'), { target: { value: 'Keep this draft' } });
    const retry = await screen.findByRole('button', { name: 'Retry loading permissions' });
    expect(screen.getByRole('button', { name: 'Send' })).toBeDisabled();
    fireEvent.submit(screen.getByLabelText('Message').closest('form')!);
    expect(submitMessage).not.toHaveBeenCalled();
    retry.focus();
    fireEvent.click(retry);
    await waitFor(() => expect(screen.getByRole('button', { name: 'Send' })).toBeEnabled());
    expect(screen.getByLabelText('Message')).toHaveValue('Keep this draft');
    expect(screen.getByRole('button', { name: /permissions: Ask every time/ })).toHaveFocus();
    fireEvent.click(screen.getByRole('button', { name: 'Send' }));
    expect(submitMessage.mock.calls[0][0].target).toEqual({
      kind: 'new',
      approval_mode: 'prompt_each_time',
    });
  });
  it('blocks existing sends and resumes after an ambiguous permission downgrade until re-read', async () => {
    getActionApprovalMode.mockResolvedValue({ approval_mode: 'always_allow' });
    setActionApprovalMode.mockRejectedValue(new Error('save response lost'));
    render(
      <UiLanguageProvider initialLanguage="en">
        <AgentOverlay />
      </UiLanguageProvider>
    );
    await act(async () => snapshotListener?.(createResumedSnapshot()));
    await act(async () => conversationListener?.(createStoppedUpdate()));
    fireEvent.click(screen.getByRole('button', { name: 'Expand' }));
    fireEvent.click(await screen.findByRole('button', { name: /permissions: Auto-approve/ }));
    fireEvent.click(screen.getByRole('menuitemradio', { name: /Ask every time/ }));
    const retry = await screen.findByRole('button', { name: 'Retry loading permissions' });
    expect(screen.getByRole('button', { name: 'Resume' })).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: 'Resume' }));
    expect(resumeAction).not.toHaveBeenCalled();
    const input = screen.getByLabelText('Message');
    fireEvent.change(input, { target: { value: 'Wait for consent' } });
    expect(screen.getByRole('button', { name: 'Send' })).toBeDisabled();
    fireEvent.submit(input.closest('form')!);
    expect(submitMessage).not.toHaveBeenCalled();
    getActionApprovalMode.mockResolvedValue({ approval_mode: 'prompt_each_time' });
    fireEvent.click(retry);
    await waitFor(() => expect(screen.getByRole('button', { name: 'Send' })).toBeEnabled());
    fireEvent.change(input, { target: { value: '' } });
    expect(screen.getByRole('button', { name: 'Resume' })).toBeEnabled();
    fireEvent.click(screen.getByRole('button', { name: 'Resume' }));
    await waitFor(() => expect(resumeAction).toHaveBeenCalledOnce());
  });
});
