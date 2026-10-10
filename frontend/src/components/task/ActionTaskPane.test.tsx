import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { UiLanguageProvider } from '@/context/UiLanguageContext';
import type { ActionConversationPage } from '../../../electron/src/actions/actionContracts';
import type {
  ActionApprovalBlocker,
  ActionLiveSnapshot,
  ActionLiveUpdate,
} from '../../../electron/src/actions/actionLiveCore';
import { ActionTaskPane } from './ActionTaskPane';
import { createActionPage } from './actionTaskFixtures';
import { createTaskDraftBridge } from '@/tests/taskDraftBridge';

type Actions = NonNullable<NonNullable<Window['electron']>['actions']>;

function update(
  page: ActionConversationPage | null,
  pageVersion: number,
  extra: Partial<ActionLiveSnapshot> = {}
): ActionLiveUpdate {
  return {
    kind: 'action_updated',
    snapshot: {
      actionId: 'act-1',
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

let listeners: Set<(update: ActionLiveUpdate) => void>;
const openConversation = vi.fn<Actions['openConversation']>();
const readConversationPage = vi.fn<Actions['readConversationPage']>();
const submitMessage = vi.fn<Actions['submitMessage']>();
const sendOrchestration = vi.fn();
const submitApprovalDecision = vi.fn();
const writeText = vi.fn<(text: string) => Promise<void>>();
const onShowInChat = vi.fn();
// The history → small window path, which opens or focuses the one that shows an Action.
const openSmall = vi.fn<(request: { actionId: string }) => Promise<void>>();
const onAddProject = vi.fn();
// Main's shared task drafts; the pane is one window of it.
let drafts = createTaskDraftBridge();

const emit = (next: ActionLiveUpdate) => act(() => listeners.forEach((listener) => listener(next)));

async function renderPane(props: Partial<Parameters<typeof ActionTaskPane>[0]> = {}) {
  const rendered = render(
    <UiLanguageProvider initialLanguage="en">
      <ActionTaskPane
        actionId="act-1"
        title="Rebuild the quote"
        onShowInChat={onShowInChat}
        onAddProject={onAddProject}
        {...props}
      />
    </UiLanguageProvider>
  );
  // The approval mode read gates every composer action.
  await screen.findByRole('button', { name: /Ask every time/ });
  return rendered;
}

const primaryButton = (name: string) => screen.getByRole('button', { name });

beforeEach(() => {
  drafts = createTaskDraftBridge();
  listeners = new Set();
  openConversation.mockReset().mockResolvedValue(undefined);
  readConversationPage.mockReset();
  submitMessage.mockReset().mockReturnValue(new Promise(() => {}));
  sendOrchestration.mockReset();
  submitApprovalDecision.mockReset().mockResolvedValue(undefined);
  writeText.mockReset().mockResolvedValue(undefined);
  onShowInChat.mockReset();
  onAddProject.mockReset();
  openSmall.mockReset().mockResolvedValue(undefined);
  Object.defineProperty(window, 'electron', {
    configurable: true,
    value: {
      actions: {
        openConversation,
        submitMessage,
        resumeAction: vi.fn(() => new Promise(() => {})),
        readConversationPage,
        readToolOutputPage: vi.fn(),
        ...drafts.window(),
        discardAttachment: vi.fn(),
        onConversationUpdated: (callback: (update: ActionLiveUpdate) => void) => {
          listeners.add(callback);
          return () => listeners.delete(callback);
        },
      },
      orchestration: { send: sendOrchestration },
      history: { openConversation: openSmall },
      clipboard: { writeText },
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

describe('ActionTaskPane', () => {
  it('shows the running step in plain text beside the title, and nothing once idle', async () => {
    await renderPane();
    const running = createActionPage('act-1', 'processing');
    running.runs[0].entries = [
      {
        step_kind: 'tool',
        step_id: 'tool-1',
        step_number: 1,
        label: 'read',
        status: 'processing',
        outcome: 'completed',
        subject: 'quote_v3.html',
        output_preview: null,
        output_available: false,
        images: [],
        file_edit: null,
      },
    ];
    emit(update(running, 1));
    const state = () => document.querySelector('header .action-task__state');
    expect(state()?.textContent).toContain('quote_v3.html');
    expect(document.querySelector('header .badge')).toBeNull();

    emit(update(createActionPage('act-1', 'success'), 2));
    expect(state()).toBeNull();
  });

  it('names the task, and shows it in the chat or copies it', async () => {
    readConversationPage.mockResolvedValue(createActionPage('act-1', 'success'));
    await renderPane();
    expect(screen.getByRole('region', { name: 'Rebuild the quote' })).toBeTruthy();
    expect(screen.getByRole('heading', { level: 1, name: 'Rebuild the quote' })).toBeTruthy();

    fireEvent.click(screen.getByRole('button', { name: 'Show in chat' }));
    expect(onShowInChat).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByRole('button', { name: 'Copy conversation' }));
    await waitFor(() => expect(writeText).toHaveBeenCalledTimes(1));
    expect(readConversationPage).toHaveBeenCalledWith(
      expect.objectContaining({ actionId: 'act-1' })
    );
    expect(writeText.mock.calls[0][0]).toContain('answer of run-1');
  });

  it('opens the task small on request, and says so when it cannot', async () => {
    readConversationPage.mockResolvedValue(createActionPage('act-1', 'success'));
    vi.spyOn(console, 'error').mockImplementation(() => undefined);
    await renderPane();

    fireEvent.click(screen.getByRole('button', { name: 'Open small' }));
    expect(openSmall).toHaveBeenCalledExactlyOnceWith({ actionId: 'act-1' });

    openSmall.mockRejectedValueOnce(new Error('Local runtime is unavailable.'));
    fireEvent.click(screen.getByRole('button', { name: 'Open small' }));
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Could not open this task in a small window.'
    );
  });

  it("opens with the suggestion the Action started from, as Pantaray's message", async () => {
    await renderPane();
    const page = createActionPage('act-1', 'success');
    page.action.suggestion_id = 'sug-1';
    page.action.approved_suggestion = {
      suggestion_id: 'sug-1',
      content: 'Draft the **invoice** before month end?',
    };
    emit(update(page, 1));

    const suggestion = screen.getByRole('region', { name: 'Suggestion' });
    const conversation = document.querySelector('.action-task__conversation');
    expect(conversation?.firstElementChild).toBe(suggestion);
    expect(suggestion.querySelector('strong')?.textContent).toBe('invoice');
    expect(suggestion.textContent).toContain('before month end?');
    expect(screen.getByText('answer of run-1')).toBeInTheDocument();
  });

  it('turns the composer button into Stop, Resume and Send by the run and the draft', async () => {
    await renderPane();
    emit(update(createActionPage('act-1', 'processing', 'run-1'), 1));
    fireEvent.click(primaryButton('Stop'));
    expect(sendOrchestration).toHaveBeenCalledWith({
      event: 'stop_process',
      data: { process_id: 'run-1' },
    });

    emit(update(createActionPage('act-1', 'canceled', 'run-1'), 2));
    expect(screen.queryByRole('button', { name: 'Stop' })).toBeNull();
    expect(primaryButton('Resume')).toBeTruthy();

    fireEvent.change(screen.getByRole('textbox', { name: 'Message' }), {
      target: { value: 'continue with the tests' },
    });
    fireEvent.click(primaryButton('Send'));
    expect(submitMessage).toHaveBeenCalledTimes(1);
    expect(submitMessage.mock.calls[0][0].target).toEqual({
      kind: 'existing',
      action_id: 'act-1',
      expected_process_id: null,
    });
  });

  it('asks for an approval and sends the decision for that pause', async () => {
    await renderPane();
    emit(update(createActionPage('act-1', 'processing'), 1));
    expect(screen.queryByRole('button', { name: 'Approve' })).toBeNull();

    emit(update(createActionPage('act-1', 'processing'), 1, { approvalBlockers: [blocker] }));
    expect(
      screen.getByText('Waiting for approval', { selector: 'header .action-task__state' })
    ).toBeTruthy();
    await act(async () => fireEvent.click(primaryButton('Approve')));
    expect(submitApprovalDecision).toHaveBeenCalledWith({
      actionId: 'act-1',
      processId: 'run-1',
      approvalSessionId: 'approval-1',
      toolRequestId: 'tool-request-1',
      decision: 'approved_once',
    });
  });

  it('loads the older part of a long conversation', async () => {
    const page = (runId: string, cursor: string) => ({
      ...createActionPage('act-1', 'success', runId),
      next_cursor: cursor,
    });
    // Opening reads two older pages by itself; the third waits for the user.
    readConversationPage
      .mockResolvedValueOnce(page('run-2', 'cursor-2'))
      .mockResolvedValueOnce(page('run-1', 'cursor-3'))
      .mockReturnValue(new Promise(() => {}));
    await renderPane();
    emit(update(page('run-3', 'cursor-1'), 1));

    fireEvent.click(await screen.findByRole('button', { name: 'Load older messages' }));
    expect(readConversationPage).toHaveBeenLastCalledWith(
      expect.objectContaining({ actionId: 'act-1', cursor: 'cursor-3' })
    );
    expect(primaryButton('Loading older messages').getAttribute('aria-disabled')).toBe('true');
  });

  it('offers to reopen a conversation whose open failed, and reopens it', async () => {
    openConversation.mockRejectedValueOnce(new Error('page read failed'));
    await renderPane();
    const reopen = await screen.findByRole('button', { name: 'Refresh failed. Try again.' });

    await act(async () => fireEvent.click(reopen));
    expect(openConversation).toHaveBeenCalledTimes(2);
    expect(openConversation).toHaveBeenLastCalledWith({ actionId: 'act-1' });
    expect(screen.queryByRole('button', { name: 'Refresh failed. Try again.' })).toBeNull();
  });

  it('keeps the conversation and the composer beside a preview', async () => {
    const { container } = await renderPane({
      renderPreview: () => <section aria-label="Preview" />,
    });
    expect(screen.getByText('Loading the conversation…')).toBeTruthy();
    emit(update(createActionPage('act-1', 'success'), 1));
    expect(screen.queryByText('Loading the conversation…')).toBeNull();
    expect(container.querySelector('.action-task--split')).not.toBeNull();
    expect(screen.getByRole('region', { name: 'Preview' })).toBeTruthy();
    expect(screen.getByRole('region', { name: 'Action conversation' })).toBeTruthy();
    expect(screen.getByText('answer of run-1')).toBeTruthy();
    expect(screen.getByRole('textbox', { name: 'Message' })).toBeTruthy();
  });

  it('puts the file chips under a finished answer only', async () => {
    const renderFileChips = vi.fn(() => <p>chips</p>);
    await renderPane({ renderFileChips });
    emit(update(createActionPage('act-1', 'processing'), 1));
    expect(screen.queryByText('chips')).toBeNull();

    emit(update(createActionPage('act-1', 'success'), 2));
    expect(screen.getByText('chips')).toBeTruthy();
    expect(renderFileChips).toHaveBeenLastCalledWith(
      expect.objectContaining({ action: expect.objectContaining({ action_id: 'act-1' }) })
    );
  });

  it('keeps the draft and its attachments for the next time it is shown', async () => {
    const actions = window.electron!.actions!;
    Object.assign(actions, {
      attachFile: vi.fn(async ({ name }: { name: string }) => ({
        attachmentId: '00000000-0000-4000-8000-000000000001',
        name,
        byteSize: 9,
      })),
    });
    const message = () => screen.getByRole('textbox', { name: 'Message' });
    const first = await renderPane();
    emit(update(createActionPage('act-1', 'success'), 1));
    fireEvent.change(message(), { target: { value: 'add the totals' } });
    await userEvent.upload(
      first.container.querySelector<HTMLInputElement>('input[type="file"]')!,
      new File(['%PDF-1.4\n'], 'quote.pdf', { type: 'application/pdf' })
    );
    await screen.findByText('quote.pdf');

    // Another row, the chat or Workspace takes the pane's place, and the user comes back.
    first.unmount();
    await renderPane();
    emit(update(createActionPage('act-1', 'success'), 1));
    await waitFor(() => expect(message()).toHaveValue('add the totals'));
    expect(screen.getByText('quote.pdf')).toBeInTheDocument();
    expect(actions.discardAttachment).not.toHaveBeenCalled();
  });

  it('gives a failed send back as the draft, and offers none still out', async () => {
    const message = () => screen.getByRole('textbox', { name: 'Message' });
    const first = await renderPane();
    emit(update(createActionPage('act-1', 'success'), 1));
    fireEvent.change(message(), { target: { value: 'add the totals' } });
    submitMessage.mockRejectedValueOnce(new Error('connection lost'));
    fireEvent.click(primaryButton('Send'));
    await screen.findByRole('button', { name: 'Retry sending' });
    first.unmount();

    const second = await renderPane();
    emit(update(createActionPage('act-1', 'success'), 1));
    await waitFor(() => expect(message()).toHaveValue('add the totals'));

    // Left while the send is still out: no window offers its words as unsent.
    fireEvent.click(primaryButton('Send'));
    second.unmount();
    await renderPane();
    emit(update(createActionPage('act-1', 'success'), 1));
    await act(async () => {});
    expect(message()).toHaveValue('');
  });
});
