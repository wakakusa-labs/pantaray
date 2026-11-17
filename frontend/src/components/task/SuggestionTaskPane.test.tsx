import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { UiLanguageProvider } from '@/context/UiLanguageContext';
import type { ActionMessageSubmitResult } from '../../../electron/src/actions/actionContracts';
import type {
  OrchestrationServerEvent,
  OverlaySnapshot,
  OverlaySnapshotPayload,
} from '../../../electron/src/orchestration/contracts';
import { SuggestionTaskPane } from './SuggestionTaskPane';
import { createTaskDraftBridge } from '@/tests/taskDraftBridge';
import { expectTooltipIsName } from '@/tests/iconTooltip';

type ElectronBridge = NonNullable<Window['electron']>;

function suggestion(overrides: Partial<OverlaySnapshot> = {}): OverlaySnapshot {
  return {
    suggestionId: 'sug-1',
    commandId: null,
    interactionContract: 'action_offer',
    suggestionText: 'Draft the invoice before month end?',
    reactionState: null,
    reactionTimestamp: null,
    dismissReason: null,
    artifacts: [],
    todo: null,
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

let snapshotListener: ((payload: OverlaySnapshotPayload) => void) | null;
let eventListener: ((event: OrchestrationServerEvent) => void) | null;
const read = vi.fn<NonNullable<ElectronBridge['suggestions']>['read']>();
const acceptAction = vi.fn<NonNullable<ElectronBridge['orchestration']>['acceptAction']>();
const submitMessage = vi.fn<(request: unknown) => Promise<ActionMessageSubmitResult>>();
const onStarted = vi.fn();

const publish = (snapshot: OverlaySnapshot) =>
  act(async () => snapshotListener?.({ snapshot, initialUiState: null }));
const emit = (event: OrchestrationServerEvent) => act(async () => eventListener?.(event));

async function renderPane(suggestionId = 'sug-1', requestedFile: string | null = null) {
  const rendered = render(
    <UiLanguageProvider initialLanguage="en">
      <SuggestionTaskPane
        suggestionId={suggestionId}
        title="Invoice draft"
        requestedFile={requestedFile}
        onStarted={onStarted}
        onAddProject={() => undefined}
      />
    </UiLanguageProvider>
  );
  await act(async () => {});
  return rendered;
}

const field = () => screen.getByRole('textbox', { name: 'Message' });
const sendButton = () => screen.getByRole('button', { name: 'Send' });
const write = (text: string) => fireEvent.change(field(), { target: { value: text } });

beforeEach(() => {
  snapshotListener = null;
  eventListener = null;
  read.mockReset().mockResolvedValue(suggestion());
  acceptAction.mockReset().mockResolvedValue(null);
  submitMessage.mockReset();
  onStarted.mockReset();
  const bridge = createTaskDraftBridge();
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
        acceptAction,
        onEvent: (callback: typeof eventListener) => {
          eventListener = callback;
          return () => {
            eventListener = null;
          };
        },
      },
      actions: {
        submitMessage: bridge.sending(submitMessage),
        attachImage: vi.fn(),
        attachFile: vi.fn(),
        ...bridge.window(),
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
  it('shows the suggestion with its title and a reply field, without a badge', async () => {
    await renderPane();

    expect(read).toHaveBeenCalledWith({ suggestionId: 'sug-1' });
    expect(screen.getByRole('heading', { name: 'Invoice draft' })).toBeInTheDocument();
    expect(screen.queryByText('Suggestion')).not.toBeInTheDocument();
    expect(screen.getByText('Draft the invoice before month end?')).toBeInTheDocument();
    expect(field()).toHaveAttribute('placeholder', 'Reply');
    // The field, then adding files and sending: the approval mode is used as it was read.
    const composer = document.querySelector('form.overlay-composer');
    const order = Array.from(composer?.querySelectorAll('textarea, button') ?? [], (element) =>
      element instanceof HTMLTextAreaElement ? 'field' : element.getAttribute('aria-label')
    );
    expect(order).toEqual(['field', 'Add files', 'Send']);
    expect(sendButton()).toBeDisabled();
  });

  it('accepts an offer once with the reply, by Enter or Send, and stays disabled while starting', async () => {
    let settle: (value: null) => void = () => undefined;
    acceptAction.mockReturnValueOnce(new Promise((resolve) => (settle = resolve)));
    await renderPane();

    write(' Use the new unit price ');
    fireEvent.keyDown(field(), { key: 'Enter' });
    fireEvent.keyDown(field(), { key: 'Enter' });
    fireEvent.click(sendButton());

    // main reuses the command it holds for this suggestion when no command id is given.
    expect(acceptAction).toHaveBeenCalledExactlyOnceWith({
      suggestionId: 'sug-1',
      commandId: null,
      supplement: 'Use the new unit price',
      supplementProjectRefs: [],
      supplementWorkRefs: [],
      images: [],
      files: [],
    });
    expect(submitMessage).not.toHaveBeenCalled();
    expect(sendButton()).toBeDisabled();
    expect(screen.getByRole('status')).toHaveTextContent('Starting');

    await publish(suggestion({ commandId: 'cmd-1', actionPhase: 'requesting', isLive: true }));
    await act(async () => settle(null));
    expect(sendButton()).toBeDisabled();
    expect(screen.getByRole('status')).toHaveTextContent('Starting');
    fireEvent.click(sendButton());
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
    expect(onStarted).toHaveBeenCalledExactlyOnceWith('act-1', null);
  });

  it('offers the same header buttons as a task: copy the link, and copy the suggestion', async () => {
    const writeText = vi.fn(async () => undefined);
    Object.defineProperty(window, 'electron', {
      configurable: true,
      value: { ...window.electron, clipboard: { writeText } },
    });
    await renderPane();

    expectTooltipIsName(screen.getByRole('button', { name: 'Copy link' }));
    fireEvent.click(screen.getByRole('button', { name: 'Copy suggestion' }));
    await waitFor(() =>
      expect(writeText).toHaveBeenCalledWith('Draft the invoice before month end?')
    );
    fireEvent.click(screen.getByRole('button', { name: 'Copy link' }));
    await waitFor(() => expect(writeText).toHaveBeenLastCalledWith('pantaray://suggestion/sug-1'));
  });

  it('opens the suggestion small, from between the link and the copy, once it is read', async () => {
    const showHistory = vi.fn(async () => undefined);
    Object.defineProperty(window, 'electron', {
      configurable: true,
      value: { ...window.electron, agentOverlay: { showHistory } },
    });
    let answer: (snapshot: OverlaySnapshot) => void = () => undefined;
    read.mockReturnValueOnce(new Promise((resolve) => (answer = resolve)));
    await renderPane();

    const header = within(screen.getByRole('banner'));
    expect(header.getAllByRole('button').map((b) => b.getAttribute('aria-label'))).toEqual([
      'Copy link',
      'Open small',
      'Copy suggestion',
    ]);
    const small = screen.getByRole('button', { name: 'Open small' });
    expectTooltipIsName(small);
    expect(small.querySelector('svg.lucide-picture-in-picture-2')).not.toBeNull();
    // Nothing to show until the suggestion is read.
    expect(small).toBeDisabled();

    await act(async () => answer(suggestion()));
    fireEvent.click(small);
    expect(showHistory).toHaveBeenCalledExactlyOnceWith({
      suggestionId: 'sug-1',
      initialUiState: { expand: true },
    });
  });

  it('says so when the suggestion could not be opened small, until it opens', async () => {
    const showHistory = vi
      .fn<() => Promise<void>>()
      .mockRejectedValueOnce(new Error('Suggestion bootstrap is unavailable.'))
      .mockResolvedValueOnce(undefined);
    Object.defineProperty(window, 'electron', {
      configurable: true,
      value: { ...window.electron, agentOverlay: { showHistory } },
    });
    vi.spyOn(console, 'error').mockImplementation(() => undefined);
    await renderPane();
    const small = screen.getByRole('button', { name: 'Open small' });

    await act(async () => fireEvent.click(small));
    expect(screen.getByRole('alert')).toHaveTextContent(
      'Could not open this suggestion in a small window.'
    );

    await act(async () => fireEvent.click(small));
    expect(screen.queryByRole('alert')).toBeNull();
    expect(showHistory).toHaveBeenCalledTimes(2);
  });

  it('keeps Open small disabled when the suggestion could not be read', async () => {
    read.mockRejectedValueOnce(new Error('unavailable'));
    await renderPane();

    expect(screen.getByRole('alert')).toHaveTextContent('Could not load this suggestion.');
    expect(screen.getByRole('button', { name: 'Open small' })).toBeDisabled();
  });

  it('keeps an unsent reply for its return, and none that went with the acceptance', async () => {
    const first = await renderPane('sug-1');
    write('Use the new unit price');
    first.unmount();

    const second = await renderPane('sug-1');
    expect(field()).toHaveValue('Use the new unit price');
    fireEvent.click(sendButton());
    // The reply went with the acceptance.
    await publish(
      suggestion({ reactionState: 'accepted', actionPhase: 'requesting', lastSequence: 5 })
    );
    second.unmount();

    await renderPane('sug-1');
    expect(field()).toHaveValue('');
  });

  it('keeps a draft when the pane is left while the suggestion loads or after its read failed', async () => {
    const first = await renderPane('sug-1');
    write('Use the new unit price');
    first.unmount();

    read.mockReturnValueOnce(new Promise(() => {}));
    (await renderPane('sug-1')).unmount();
    read.mockRejectedValueOnce(new Error('unavailable'));
    const failed = await renderPane('sug-1');
    expect(screen.getByRole('alert')).toHaveTextContent('Could not load this suggestion.');
    failed.unmount();

    await renderPane('sug-1');
    expect(field()).toHaveValue('Use the new unit price');
  });

  it('keeps a draft written after a dismissal for its return', async () => {
    read.mockResolvedValue(suggestion({ reactionState: 'rejected' }));
    const first = await renderPane('sug-1');
    write('Only the summary, please');
    first.unmount();

    await renderPane('sug-1');
    expect(field()).toHaveValue('Only the summary, please');
  });

  it('lets the user send the same reply again when the accept call fails', async () => {
    acceptAction.mockRejectedValueOnce(new Error('ipc'));
    await renderPane();

    write('Use the new unit price');
    fireEvent.click(sendButton());
    await act(async () => {});

    expect(screen.getByRole('alert')).toHaveTextContent(
      'Your reply could not be sent. Send it again.'
    );
    // It is retried as it was sent.
    expect(field()).toHaveAttribute('readonly');
    fireEvent.click(sendButton());
    expect(acceptAction).toHaveBeenCalledTimes(2);
    expect(acceptAction.mock.calls[1][0]).toMatchObject({
      commandId: null,
      supplement: 'Use the new unit price',
    });
  });

  it('returns to actionable with the error after preflight_rejected', async () => {
    await renderPane();
    write('Use the new unit price');
    fireEvent.click(sendButton());
    await publish(suggestion({ commandId: 'cmd-1', actionPhase: 'requesting', isLive: true }));
    expect(sendButton()).toBeDisabled();

    // main resets its record before it forwards the event.
    await publish(suggestion({ lastSequence: 6 }));
    await emit(actionError('preflight_rejected', 'No AI connection is set up.'));

    expect(screen.getByRole('alert')).toHaveTextContent('No AI connection is set up.');
    expect(sendButton()).toBeEnabled();
    expect(screen.queryByText('Starting')).toBeNull();
  });

  it('shows the public message when the start fails', async () => {
    await renderPane();
    write('Use the new unit price');
    fireEvent.click(sendButton());
    await publish(
      suggestion({
        commandId: 'cmd-1',
        reactionState: 'accepted',
        actionPhase: 'accepted_pending_start',
      })
    );

    await emit(actionError('start_failed', 'The Action could not start.'));

    expect(screen.getByRole('alert')).toHaveTextContent('The Action could not start.');
    expect(screen.queryByRole('textbox', { name: 'Message' })).toBeNull();
    expect(screen.queryByText('Starting')).toBeNull();
  });

  it('answers a message-only suggestion with a reply that starts a new Action', async () => {
    const messageId = '00000000-0000-4000-8000-000000000042';
    vi.spyOn(crypto, 'randomUUID').mockReturnValueOnce(messageId);
    read.mockResolvedValueOnce(suggestion({ interactionContract: 'message_only' }));
    let settle: (result: ActionMessageSubmitResult) => void = () => undefined;
    submitMessage.mockReturnValueOnce(new Promise((resolve) => (settle = resolve)));
    await renderPane();

    write('Yes, please');
    fireEvent.click(sendButton());

    expect(submitMessage).toHaveBeenCalledWith({
      target: {
        kind: 'new',
        reply_to_suggestion_id: 'sug-1',
      },
      message: {
        version: 1,
        message_id: messageId,
        content: 'Yes, please',
        images: [],
        language: 'en',
        project_refs: [],
        work_refs: [],
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
    expect(onStarted).toHaveBeenCalledWith('act-reply', null);
  });

  it('shows a past dismissal and answers it with a reply that starts a new Action', async () => {
    const messageId = '00000000-0000-4000-8000-000000000043';
    vi.spyOn(crypto, 'randomUUID').mockReturnValueOnce(messageId);
    read.mockResolvedValueOnce(
      suggestion({
        reactionState: 'rejected',
        reactionTimestamp: '2026-10-10T00:01:00Z',
        dismissReason: 'Already sent it by hand',
      })
    );
    let settle: (result: ActionMessageSubmitResult) => void = () => undefined;
    submitMessage.mockReturnValueOnce(new Promise((resolve) => (settle = resolve)));
    await renderPane();

    expect(screen.getByText('Draft the invoice before month end?')).toBeInTheDocument();
    expect(screen.getByRole('article', { name: 'Your reason for dismissing' })).toHaveTextContent(
      'Already sent it by hand'
    );
    expect(screen.getByText('You dismissed this suggestion.')).toBeInTheDocument();
    write('Only the summary, please');
    fireEvent.click(sendButton());

    expect(submitMessage).toHaveBeenCalledWith({
      target: {
        kind: 'new',
        reply_to_suggestion_id: 'sug-1',
      },
      message: {
        version: 1,
        message_id: messageId,
        content: 'Only the summary, please',
        images: [],
        language: 'en',
        project_refs: [],
        work_refs: [],
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
    expect(onStarted).toHaveBeenCalledWith('act-reply', null);
  });

  it('hands over to the reply the other window sent for a dismissed suggestion', async () => {
    read.mockResolvedValueOnce(suggestion({ reactionState: 'rejected' }));
    await renderPane();
    expect(screen.getByRole('textbox', { name: 'Message' })).toBeInTheDocument();

    await publish(suggestion({ reactionState: 'rejected', actionId: 'act-reply' }));

    expect(onStarted).toHaveBeenCalledWith('act-reply', null);
    expect(screen.queryByRole('textbox', { name: 'Message' })).toBeNull();
  });

  it('opens the existing conversation when the suggestion was already replied to', async () => {
    read.mockResolvedValueOnce(suggestion({ reactionState: 'rejected' }));
    submitMessage.mockResolvedValueOnce({ kind: 'reply_exists', actionId: 'act-existing' });
    await renderPane();

    write('Also this');
    await act(async () => fireEvent.click(sendButton()));

    expect(onStarted).toHaveBeenCalledWith('act-existing', null);
  });

  it('hands over at once when the suggestion already has an Action', async () => {
    read.mockResolvedValueOnce(
      suggestion({ reactionState: 'accepted', actionPhase: 'terminal', actionId: 'act-9' })
    );
    await renderPane();

    expect(onStarted).toHaveBeenCalledWith('act-9', null);
  });

  it('ignores snapshots and errors of other suggestions', async () => {
    await renderPane();

    await publish(
      suggestion({ suggestionId: 'sug-2', reactionState: 'rejected', lastSequence: 9 })
    );
    await publish(suggestion({ suggestionId: 'sug-2', actionId: 'act-2', lastSequence: 9 }));
    await emit(actionError('preflight_rejected', 'Not this one.', 'sug-2'));

    write('Use the new unit price');
    expect(sendButton()).toBeEnabled();
    expect(screen.queryByRole('alert')).toBeNull();
    expect(onStarted).not.toHaveBeenCalled();
  });
  describe('files the suggestion made', () => {
    const readFile = vi.fn<NonNullable<ElectronBridge['actionFiles']>['read']>();
    const filesGroup = () => screen.queryByRole('group', { name: "This suggestion's files" });

    beforeEach(() => {
      readFile.mockReset().mockResolvedValue({ kind: 'text', text: '# Reply', truncated: false });
      Object.assign(window.electron ?? {}, { actionFiles: { read: readFile } });
    });

    it('shows no chips for a suggestion without files', async () => {
      await renderPane();

      expect(screen.getByText('Draft the invoice before month end?')).toBeInTheDocument();
      expect(filesGroup()).toBeNull();
    });

    it('previews a file its chip opens, read by the suggestion and the listed path', async () => {
      read.mockResolvedValue(suggestion({ artifacts: ['drafts/reply.md', 'sheet.csv'] }));
      await renderPane();

      const chips = within(filesGroup()!);
      expect(chips.getAllByRole('button').map((chip) => chip.textContent)).toEqual([
        'MDreply.md',
        'CSVsheet.csv',
      ]);
      fireEvent.click(chips.getByRole('button', { name: /reply\.md/ }));

      expect(await screen.findByRole('heading', { name: 'reply.md' })).toBeInTheDocument();
      expect(await screen.findByRole('heading', { name: 'Reply' })).toBeInTheDocument();
      expect(readFile).toHaveBeenCalledWith({
        target: { kind: 'suggestion', id: 'sug-1' },
        path: 'drafts/reply.md',
      });
      expect(chips.getByRole('button', { name: /reply\.md/ })).toHaveAttribute(
        'aria-pressed',
        'true'
      );
    });

    it('hands the open file over to the Action that takes the pane over', async () => {
      read.mockResolvedValue(suggestion({ artifacts: ['drafts/reply.md'] }));
      await renderPane();
      fireEvent.click(within(filesGroup()!).getByRole('button', { name: /reply\.md/ }));
      expect(await screen.findByRole('heading', { name: 'reply.md' })).toBeInTheDocument();

      await publish(suggestion({ artifacts: ['drafts/reply.md'], actionId: 'act-1' }));

      expect(onStarted).toHaveBeenCalledWith('act-1', {
        target: { kind: 'suggestion', id: 'sug-1' },
        file: { path: 'drafts/reply.md', name: 'reply.md', kind: 'markdown', label: 'MD' },
      });
    });

    it('opens the preview of the file an Overlay chip asked for', async () => {
      read.mockResolvedValue(suggestion({ artifacts: ['drafts/reply.md'] }));
      await renderPane('sug-1', 'drafts/reply.md');

      expect(await screen.findByRole('heading', { name: 'reply.md' })).toBeInTheDocument();
      expect(readFile).toHaveBeenCalledWith({
        target: { kind: 'suggestion', id: 'sug-1' },
        path: 'drafts/reply.md',
      });
    });
  });
});
