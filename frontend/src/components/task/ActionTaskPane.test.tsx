import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
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
const onAddProject = vi.fn();

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
  listeners = new Set();
  openConversation.mockReset().mockResolvedValue(undefined);
  readConversationPage.mockReset();
  submitMessage.mockReset().mockReturnValue(new Promise(() => {}));
  sendOrchestration.mockReset();
  submitApprovalDecision.mockReset().mockResolvedValue(undefined);
  writeText.mockReset().mockResolvedValue(undefined);
  onShowInChat.mockReset();
  onAddProject.mockReset();
  Object.defineProperty(window, 'electron', {
    configurable: true,
    value: {
      actions: {
        openConversation,
        submitMessage,
        resumeAction: vi.fn(() => new Promise(() => {})),
        readConversationPage,
        readToolOutputPage: vi.fn(),
        discardAttachment: vi.fn(),
        onConversationUpdated: (callback: (update: ActionLiveUpdate) => void) => {
          listeners.add(callback);
          return () => listeners.delete(callback);
        },
      },
      orchestration: { send: sendOrchestration },
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
    const { container } = await renderPane({ preview: <section aria-label="Preview" /> });
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
});
