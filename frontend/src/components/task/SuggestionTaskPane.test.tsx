import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { UiLanguageProvider } from '@/context/UiLanguageContext';
import type { ActionMessageSubmitResult } from '../../../electron/src/actions/actionContracts';
import type {
  OrchestrationServerEvent,
  OverlaySnapshot,
  OverlaySnapshotPayload,
} from '../../../electron/src/orchestration/contracts';
import { SuggestionTaskPane } from './SuggestionTaskPane';
import { createTaskComposerDrafts } from './taskComposerDrafts';

type ElectronBridge = NonNullable<Window['electron']>;

function suggestion(overrides: Partial<OverlaySnapshot> = {}): OverlaySnapshot {
  return {
    suggestionId: 'sug-1',
    commandId: null,
    interactionContract: 'action_offer',
    suggestionText: 'Draft the invoice before month end?',
    reactionState: null,
    reactionTimestamp: null,
    actionPhase: 'idle',
    actionStatus: null,
    actionErrorCode: null,
    actionFailureStage: null,
    actionFailureMessagePublic: null,
    processId: null,
    actionId: null,
    updatedAt: '2026-10-10T00:00:00Z',
    lastSequence: 4,
    isLive: false,
    ...overrides,
  };
}

function actionError(
  stage: 'preflight_rejected' | 'start_failed',
  message: string,
  suggestionId = 'sug-1'
): OrchestrationServerEvent {
  return {
    event: 'error',
    data: { error_type: 'action', error_code: 'E', error_message: message, severity: 'error' },
    meta: {
      kind: 'action',
      suggestion_id: suggestionId,
      command_id: 'cmd-1',
      stage,
      error_code: 'E',
    },
  };
}

// The shape the backend's dismiss handler sends when it cannot read or save the suggestion.
function suggestionError(stage: string, suggestionId = 'sug-1'): OrchestrationServerEvent {
  return {
    event: 'error',
    data: {
      error_type: 'internal_error',
      error_code: 'WS_DEPENDENCY_UNAVAILABLE',
      error_message: 'Failed to persist suggestion state.',
      severity: 'error',
    },
    meta: {
      kind: 'suggestion',
      suggestion_id: suggestionId,
      stage,
      error_code: 'WS_DEPENDENCY_UNAVAILABLE',
    },
  };
}

let snapshotListener: ((payload: OverlaySnapshotPayload) => void) | null;
let eventListener: ((event: OrchestrationServerEvent) => void) | null;
const read = vi.fn<NonNullable<ElectronBridge['suggestions']>['read']>();
const acceptAction = vi.fn<NonNullable<ElectronBridge['orchestration']>['acceptAction']>();
const send = vi.fn();
const submitMessage = vi.fn<(request: unknown) => Promise<ActionMessageSubmitResult>>();
const onStarted = vi.fn();
const onShowInChat = vi.fn();

const publish = (snapshot: OverlaySnapshot) =>
  act(async () => snapshotListener?.({ snapshot, initialUiState: null }));
const emit = (event: OrchestrationServerEvent) => act(async () => eventListener?.(event));

async function renderPane(suggestionId = 'sug-1', drafts = createTaskComposerDrafts(undefined)) {
  const rendered = render(
    <UiLanguageProvider initialLanguage="en">
      <SuggestionTaskPane
        suggestionId={suggestionId}
        title="Invoice draft"
        onStarted={onStarted}
        onShowInChat={onShowInChat}
        onAddProject={() => undefined}
        drafts={drafts}
      />
    </UiLanguageProvider>
  );
  await act(async () => {});
  return rendered;
}

const acceptButton = () => screen.getByRole('button', { name: 'Accept' });
const dismissButton = () => screen.getByRole('button', { name: 'Dismiss suggestion' });

beforeEach(() => {
  snapshotListener = null;
  eventListener = null;
  read.mockReset().mockResolvedValue(suggestion());
  acceptAction.mockReset().mockResolvedValue(null);
  send.mockReset();
  submitMessage.mockReset();
  onStarted.mockReset();
  Object.defineProperty(window, 'electron', {
    configurable: true,
    value: {
      approval: {
        getWorkspaceEditCommandPreference: vi.fn(async () => ({
          scope_type: 'global',
          scope_ref: null,
          approval_mode: 'prompt_each_time',
          applies_to: ['workspace_edit_and_command'],
        })),
      },
      suggestions: {
        read,
        onSnapshot: (callback: typeof snapshotListener) => {
          snapshotListener = callback;
          return () => {
            snapshotListener = null;
          };
        },
      },
      orchestration: {
        send,
        acceptAction,
        onEvent: (callback: typeof eventListener) => {
          eventListener = callback;
          return () => {
            eventListener = null;
          };
        },
      },
      actions: {
        submitMessage,
        attachImage: vi.fn(),
        attachFile: vi.fn(),
        discardAttachment: vi.fn(async () => undefined),
        readConversationPage: vi.fn(),
      },
    },
  });
});

afterEach(() => {
  cleanup();
});

describe('SuggestionTaskPane', () => {
  it('shows the suggestion with its title and decision, without a badge', async () => {
    await renderPane();

    expect(read).toHaveBeenCalledWith({ suggestionId: 'sug-1' });
    expect(screen.getByRole('heading', { name: 'Invoice draft' })).toBeInTheDocument();
    expect(screen.queryByText('Suggestion')).not.toBeInTheDocument();
    expect(screen.getByText('Draft the invoice before month end?')).toBeInTheDocument();
    expect(acceptButton()).toBeEnabled();
    expect(dismissButton()).toBeEnabled();
    expect(screen.getByLabelText('Additional instructions (optional)')).toHaveAttribute(
      'placeholder',
      'Add conditions and approve (optional)'
    );
  });

  it('puts dismiss, then accept, where the composer sends, after its other controls', async () => {
    await renderPane();

    const composer = document.querySelector('form.overlay-composer');
    expect(screen.queryByRole('button', { name: 'Send' })).toBeNull();
    // Reading and tab order: the field, the composer's own controls, then decline and approve.
    const order = Array.from(composer?.querySelectorAll('textarea, button') ?? [], (element) =>
      element instanceof HTMLTextAreaElement
        ? 'field'
        : (element.getAttribute('aria-label') ?? element.textContent)
    );
    expect(order[0]).toBe('field');
    expect(order[1]).toBe('Add files');
    expect(order.slice(-2)).toEqual(['Dismiss suggestion', 'Accept']);
  });

  it('leaves both decisions to their buttons: Enter neither approves nor dismisses', async () => {
    await renderPane();
    const field = screen.getByLabelText('Additional instructions (optional)');
    fireEvent.change(field, { target: { value: 'Use the new unit price' } });

    fireEvent.keyDown(field, { key: 'Enter' });

    expect(acceptAction).not.toHaveBeenCalled();
    expect(send).not.toHaveBeenCalled();
    expect(field).toHaveValue('Use the new unit price');
    expect(acceptButton()).toBeEnabled();
  });

  it('offers the same header buttons as a task: show in chat, and copy the suggestion', async () => {
    const writeText = vi.fn(async () => undefined);
    Object.defineProperty(window, 'electron', {
      configurable: true,
      value: { ...window.electron, clipboard: { writeText } },
    });
    await renderPane();

    fireEvent.click(screen.getByRole('button', { name: 'Show in chat' }));
    expect(onShowInChat).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole('button', { name: 'Copy suggestion' }));
    await waitFor(() =>
      expect(writeText).toHaveBeenCalledWith('Draft the invoice before month end?')
    );
  });

  it('keeps an unsent extra instruction for its return, and none that went with the approval', async () => {
    const drafts = createTaskComposerDrafts(undefined);
    const field = () => screen.getByLabelText('Additional instructions (optional)');
    const first = await renderPane('sug-1', drafts);
    fireEvent.change(field(), { target: { value: 'Use the new unit price' } });
    first.unmount();

    const second = await renderPane('sug-1', drafts);
    expect(field()).toHaveValue('Use the new unit price');
    acceptAction.mockReturnValueOnce(new Promise(() => {}));
    fireEvent.click(acceptButton());
    second.unmount();

    await renderPane('sug-1', drafts);
    expect(field()).toHaveValue('');
  });

  it('keeps a draft when the pane is left while the suggestion loads or after its read failed', async () => {
    const drafts = createTaskComposerDrafts(undefined);
    const field = () => screen.getByLabelText('Additional instructions (optional)');
    const first = await renderPane('sug-1', drafts);
    fireEvent.change(field(), { target: { value: 'Use the new unit price' } });
    first.unmount();

    read.mockReturnValueOnce(new Promise(() => {}));
    (await renderPane('sug-1', drafts)).unmount();
    read.mockRejectedValueOnce(new Error('unavailable'));
    const failed = await renderPane('sug-1', drafts);
    expect(screen.getByRole('alert')).toHaveTextContent('Could not load this suggestion.');
    failed.unmount();

    await renderPane('sug-1', drafts);
    expect(field()).toHaveValue('Use the new unit price');
  });

  it('keeps a draft written around a dismissal, since a reply may still follow it', async () => {
    const drafts = createTaskComposerDrafts(undefined);
    const first = await renderPane('sug-1', drafts);
    fireEvent.change(screen.getByLabelText('Additional instructions (optional)'), {
      target: { value: 'Only the summary' },
    });
    fireEvent.click(dismissButton());
    first.unmount();

    read.mockResolvedValueOnce(suggestion({ reactionState: 'rejected' }));
    const second = await renderPane('sug-1', drafts);
    const message = () => screen.getByRole('textbox', { name: 'Message' });
    expect(message()).toHaveValue('Only the summary');
    fireEvent.change(message(), { target: { value: 'Only the summary, please' } });
    second.unmount();

    read.mockResolvedValueOnce(suggestion({ reactionState: 'rejected' }));
    await renderPane('sug-1', drafts);
    expect(message()).toHaveValue('Only the summary, please');
  });

  it('accepts with the extra instruction and no command id, and stays disabled while starting', async () => {
    let settle: (value: null) => void = () => undefined;
    acceptAction.mockReturnValueOnce(new Promise((resolve) => (settle = resolve)));
    await renderPane();

    fireEvent.change(screen.getByLabelText('Additional instructions (optional)'), {
      target: { value: ' Use the new unit price ' },
    });
    fireEvent.click(acceptButton());

    // main reuses the command it holds for this suggestion when no command id is given.
    expect(acceptAction).toHaveBeenCalledWith({
      suggestionId: 'sug-1',
      commandId: null,
      supplement: 'Use the new unit price',
      supplementProjectRefs: [],
      approvalMode: 'prompt_each_time',
      images: [],
      files: [],
    });
    expect(acceptButton()).toBeDisabled();
    expect(dismissButton()).toBeDisabled();
    expect(screen.getByRole('status')).toHaveTextContent('Starting');

    await publish(suggestion({ commandId: 'cmd-1', actionPhase: 'requesting', isLive: true }));
    await act(async () => settle(null));
    expect(acceptButton()).toBeDisabled();
    expect(screen.getByRole('status')).toHaveTextContent('Starting');
    fireEvent.click(acceptButton());
    expect(acceptAction).toHaveBeenCalledTimes(1);
    expect(onStarted).not.toHaveBeenCalled();

    await publish(
      suggestion({
        commandId: 'cmd-1',
        reactionState: 'accepted',
        actionPhase: 'processing',
        actionId: 'act-1',
        processId: 'run-1',
      })
    );
    expect(onStarted).toHaveBeenCalledWith('act-1');
    expect(onStarted).toHaveBeenCalledTimes(1);
  });

  it('lets the user accept again when the accept call fails', async () => {
    acceptAction.mockRejectedValueOnce(new Error('ipc'));
    await renderPane();

    fireEvent.click(acceptButton());
    await act(async () => {});

    expect(screen.getByRole('alert')).toHaveTextContent(
      'Could not confirm this action. Try approving again.'
    );
    expect(acceptButton()).toBeEnabled();
    fireEvent.click(acceptButton());
    expect(acceptAction).toHaveBeenCalledTimes(2);
    expect(acceptAction.mock.calls[1][0].commandId).toBeNull();
  });

  it('dismisses through dismiss_suggestion and then keeps only the composer', async () => {
    await renderPane();

    fireEvent.click(dismissButton());

    expect(send).toHaveBeenCalledWith({
      event: 'dismiss_suggestion',
      data: { suggestion_id: 'sug-1' },
    });
    expect(acceptAction).not.toHaveBeenCalled();
    expect(acceptButton()).toBeDisabled();
    expect(dismissButton()).toBeDisabled();

    await publish(suggestion({ reactionState: 'rejected', lastSequence: 5 }));
    expect(screen.queryByRole('button', { name: 'Accept' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Dismiss suggestion' })).toBeNull();
    expect(screen.getByText('You dismissed this suggestion.')).toBeInTheDocument();
    // Nothing written: the dismissal stands.
    expect(screen.getByRole('textbox', { name: 'Message' })).toHaveAttribute(
      'placeholder',
      'Add a reason, or what you would like instead (optional)'
    );
    expect(screen.getByRole('button', { name: 'Send' })).toBeDisabled();
    expect(submitMessage).not.toHaveBeenCalled();
    expect(onStarted).not.toHaveBeenCalled();
  });

  it('dismisses with the words, then sends them as a reply once the dismissal is recorded', async () => {
    const messageId = '00000000-0000-4000-8000-000000000044';
    vi.spyOn(crypto, 'randomUUID').mockReturnValueOnce(messageId);
    submitMessage.mockReturnValueOnce(new Promise(() => {}));
    await renderPane();
    fireEvent.change(screen.getByLabelText('Additional instructions (optional)'), {
      target: { value: 'Only the summary, please' },
    });

    fireEvent.click(dismissButton());
    expect(send).toHaveBeenCalledWith({
      event: 'dismiss_suggestion',
      data: { suggestion_id: 'sug-1' },
    });
    // The backend takes a reply only to a dismissed offer.
    expect(submitMessage).not.toHaveBeenCalled();

    await publish(suggestion({ reactionState: 'rejected', lastSequence: 5 }));
    expect(submitMessage).toHaveBeenCalledTimes(1);
    expect(submitMessage).toHaveBeenCalledWith({
      target: { kind: 'new', approval_mode: 'prompt_each_time', reply_to_suggestion_id: 'sug-1' },
      message: {
        version: 1,
        message_id: messageId,
        content: 'Only the summary, please',
        images: [],
        language: 'en',
        project_refs: [],
        files: [],
      },
    });
    expect(acceptAction).not.toHaveBeenCalled();
    expect(screen.getByRole('status')).toHaveTextContent('Starting');
  });

  it('keeps the words for a retry when the reply after a dismissal fails', async () => {
    submitMessage.mockRejectedValueOnce(new Error('Disconnected'));
    await renderPane();
    fireEvent.change(screen.getByLabelText('Additional instructions (optional)'), {
      target: { value: 'Only the summary, please' },
    });
    fireEvent.click(dismissButton());
    await publish(suggestion({ reactionState: 'rejected', lastSequence: 5 }));

    expect(submitMessage).toHaveBeenCalledTimes(1);
    expect(screen.getByRole('textbox', { name: 'Message' })).toHaveValue(
      'Only the summary, please'
    );
    expect(screen.getByRole('button', { name: 'Retry sending' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Accept' })).toBeNull();
  });

  it('lets the user dismiss again when the backend answers the dismissal with an error', async () => {
    await renderPane();
    // A suggestion error the pane did not cause leaves it as it is.
    await emit(suggestionError('load_state_failed'));
    expect(screen.queryByRole('alert')).toBeNull();

    fireEvent.click(dismissButton());
    await emit(suggestionError('load_state_failed', 'sug-2'));
    expect(dismissButton()).toBeDisabled();
    await emit(suggestionError('persist_status_failed'));

    expect(screen.getByRole('alert')).toHaveTextContent(
      'Could not dismiss this suggestion. Try again.'
    );
    expect(acceptButton()).toBeEnabled();
    expect(dismissButton()).toBeEnabled();
    expect(screen.getByLabelText('Additional instructions (optional)')).toBeInTheDocument();
    fireEvent.click(dismissButton());
    expect(send).toHaveBeenCalledTimes(2);
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('returns to actionable with the error after preflight_rejected', async () => {
    await renderPane();
    fireEvent.click(acceptButton());
    await publish(suggestion({ commandId: 'cmd-1', actionPhase: 'requesting', isLive: true }));
    expect(acceptButton()).toBeDisabled();

    // main resets its record before it forwards the event.
    await publish(suggestion({ lastSequence: 6 }));
    await emit(actionError('preflight_rejected', 'No AI connection is set up.'));

    expect(screen.getByRole('alert')).toHaveTextContent('No AI connection is set up.');
    expect(acceptButton()).toBeEnabled();
    expect(dismissButton()).toBeEnabled();
    expect(screen.queryByText('Starting')).toBeNull();
  });

  it('shows the public message when the start fails', async () => {
    await renderPane();
    fireEvent.click(acceptButton());
    await publish(
      suggestion({
        commandId: 'cmd-1',
        reactionState: 'accepted',
        actionPhase: 'accepted_pending_start',
      })
    );

    await emit(actionError('start_failed', 'The Action could not start.'));

    expect(screen.getByRole('alert')).toHaveTextContent('The Action could not start.');
    expect(screen.queryByRole('button', { name: 'Accept' })).toBeNull();
    expect(screen.queryByText('Starting')).toBeNull();
  });

  it('answers a message-only suggestion with a reply that starts a new Action', async () => {
    const messageId = '00000000-0000-4000-8000-000000000042';
    vi.spyOn(crypto, 'randomUUID').mockReturnValueOnce(messageId);
    read.mockResolvedValueOnce(suggestion({ interactionContract: 'message_only' }));
    let settle: (result: ActionMessageSubmitResult) => void = () => undefined;
    submitMessage.mockReturnValueOnce(new Promise((resolve) => (settle = resolve)));
    await renderPane();

    expect(screen.queryByRole('button', { name: 'Accept' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Dismiss suggestion' })).toBeNull();
    fireEvent.change(screen.getByLabelText('Message'), { target: { value: 'Yes, please' } });
    fireEvent.click(screen.getByRole('button', { name: 'Send' }));

    expect(submitMessage).toHaveBeenCalledWith({
      target: {
        kind: 'new',
        approval_mode: 'prompt_each_time',
        reply_to_suggestion_id: 'sug-1',
      },
      message: {
        version: 1,
        message_id: messageId,
        content: 'Yes, please',
        images: [],
        language: 'en',
        project_refs: [],
        files: [],
      },
    });
    expect(screen.getByRole('status')).toHaveTextContent('Starting');

    await act(async () =>
      settle({
        kind: 'submitted',
        response: {
          action_id: 'act-reply',
          message_id: messageId,
          step_id: 'step-1',
          action_status: 'processing',
          disposition: 'started',
          process_id: 'run-1',
        },
      })
    );
    expect(onStarted).toHaveBeenCalledWith('act-reply');
  });

  it('answers a dismissed suggestion with a reply that starts a new Action', async () => {
    const messageId = '00000000-0000-4000-8000-000000000043';
    vi.spyOn(crypto, 'randomUUID').mockReturnValueOnce(messageId);
    read.mockResolvedValueOnce(
      suggestion({ reactionState: 'rejected', reactionTimestamp: '2026-10-10T00:01:00Z' })
    );
    let settle: (result: ActionMessageSubmitResult) => void = () => undefined;
    submitMessage.mockReturnValueOnce(new Promise((resolve) => (settle = resolve)));
    await renderPane();

    expect(screen.getByText('Draft the invoice before month end?')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Accept' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Dismiss suggestion' })).toBeNull();
    fireEvent.change(screen.getByLabelText('Message'), {
      target: { value: 'Only the summary, please' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Send' }));

    expect(submitMessage).toHaveBeenCalledWith({
      target: {
        kind: 'new',
        approval_mode: 'prompt_each_time',
        reply_to_suggestion_id: 'sug-1',
      },
      message: {
        version: 1,
        message_id: messageId,
        content: 'Only the summary, please',
        images: [],
        language: 'en',
        project_refs: [],
        files: [],
      },
    });
    expect(acceptAction).not.toHaveBeenCalled();
    expect(screen.getByRole('status')).toHaveTextContent('Starting');

    await act(async () =>
      settle({
        kind: 'submitted',
        response: {
          action_id: 'act-reply',
          message_id: messageId,
          step_id: 'step-1',
          action_status: 'processing',
          disposition: 'started',
          process_id: 'run-1',
        },
      })
    );
    expect(onStarted).toHaveBeenCalledWith('act-reply');
  });

  it('hands over to the reply the other window sent for a dismissed suggestion', async () => {
    read.mockResolvedValueOnce(suggestion({ reactionState: 'rejected' }));
    await renderPane();
    expect(screen.getByRole('textbox', { name: 'Message' })).toBeInTheDocument();

    await publish(suggestion({ reactionState: 'rejected', actionId: 'act-reply' }));

    expect(onStarted).toHaveBeenCalledWith('act-reply');
    expect(screen.queryByRole('textbox', { name: 'Message' })).toBeNull();
  });

  it('opens the existing conversation when the suggestion was already replied to', async () => {
    read.mockResolvedValueOnce(suggestion({ reactionState: 'rejected' }));
    submitMessage.mockResolvedValueOnce({ kind: 'reply_exists', actionId: 'act-existing' });
    await renderPane();

    fireEvent.change(screen.getByLabelText('Message'), { target: { value: 'Also this' } });
    await act(async () => fireEvent.click(screen.getByRole('button', { name: 'Send' })));

    expect(onStarted).toHaveBeenCalledWith('act-existing');
  });

  it('hands over at once when the suggestion already has an Action', async () => {
    read.mockResolvedValueOnce(
      suggestion({ reactionState: 'accepted', actionPhase: 'terminal', actionId: 'act-9' })
    );
    await renderPane();

    expect(onStarted).toHaveBeenCalledWith('act-9');
    // No decision is offered; only the header's buttons remain.
    expect(screen.queryByRole('button', { name: 'Accept' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Dismiss suggestion' })).toBeNull();
  });

  it('ignores snapshots and errors of other suggestions', async () => {
    await renderPane();

    await publish(
      suggestion({ suggestionId: 'sug-2', reactionState: 'rejected', lastSequence: 9 })
    );
    await publish(suggestion({ suggestionId: 'sug-2', actionId: 'act-2', lastSequence: 9 }));
    await emit(actionError('preflight_rejected', 'Not this one.', 'sug-2'));

    expect(acceptButton()).toBeEnabled();
    expect(screen.queryByRole('alert')).toBeNull();
    expect(onStarted).not.toHaveBeenCalled();
  });
});
