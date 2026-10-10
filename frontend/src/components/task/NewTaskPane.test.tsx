import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';

import { UiLanguageProvider } from '@/context/UiLanguageContext';
import type { ActionMessageSubmitResult } from '../../../electron/src/actions/actionContracts';
import { NewTaskPane } from './NewTaskPane';
import { createTaskComposerDrafts } from './taskComposerDrafts';

const submitMessage = vi.fn<(request: unknown) => Promise<ActionMessageSubmitResult>>();
const onStarted = vi.fn();

function renderPane(drafts = createTaskComposerDrafts(undefined)) {
  return render(
    <UiLanguageProvider initialLanguage="en">
      <NewTaskPane onStarted={onStarted} onAddProject={() => undefined} drafts={drafts} />
    </UiLanguageProvider>
  );
}

beforeEach(() => {
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

it('shows an empty composer whose send opens a new Action and hands the pane to it', async () => {
  const messageId = '00000000-0000-4000-8000-000000000051';
  vi.spyOn(crypto, 'randomUUID').mockReturnValueOnce(messageId);
  submitMessage.mockResolvedValueOnce({
    kind: 'submitted',
    response: {
      action_id: 'act-new',
      message_id: messageId,
      step_id: 'step-1',
      action_status: 'processing',
      disposition: 'pending',
      process_id: null,
    },
  });
  renderPane();

  expect(screen.getByRole('heading', { name: 'New task' })).toBeInTheDocument();
  const message = screen.getByRole('textbox', { name: 'Message' });
  expect(message).toHaveValue('');
  fireEvent.change(message, { target: { value: 'Draft the quote' } });
  await waitFor(() => expect(screen.getByRole('button', { name: 'Send' })).toBeEnabled());
  await act(async () => {
    fireEvent.click(screen.getByRole('button', { name: 'Send' }));
  });

  expect(submitMessage).toHaveBeenCalledWith({
    target: { kind: 'new', approval_mode: 'prompt_each_time' },
    message: {
      version: 1,
      message_id: messageId,
      content: 'Draft the quote',
      images: [],
      language: 'en',
      project_refs: [],
      files: [],
    },
  });
  await waitFor(() => expect(onStarted).toHaveBeenCalledWith('act-new'));
});

it('keeps an unsent draft for the new task while the pane is away', async () => {
  const drafts = createTaskComposerDrafts(undefined);
  const first = renderPane(drafts);
  fireEvent.change(screen.getByRole('textbox', { name: 'Message' }), {
    target: { value: 'Half a thought' },
  });
  first.unmount();

  renderPane(drafts);
  expect(screen.getByRole('textbox', { name: 'Message' })).toHaveValue('Half a thought');
});
