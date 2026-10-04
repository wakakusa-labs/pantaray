import { cleanup, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, it } from 'vitest';

import fixture from '../../tests/fixtures/action_conversation_v2.json';
import { projectActionConversationView } from '../../electron/src/actions/actionConversationModel';
import { parseActionConversationPage } from '../../electron/src/actions/actionContracts';
import type { ActionToolOutputLoader } from '../../electron/src/actions/actionToolOutputLoader';
import { ActionConversationView } from '@/components/action-conversation/ActionConversationView';
import { UiLanguageProvider } from '@/context/UiLanguageContext';

const UNUSED_TOOL_OUTPUT_LOADER: ActionToolOutputLoader = {
  load: async () => {
    throw new Error('Tool output must remain unopened in this integration test.');
  },
  retry: async () => {
    throw new Error('Tool output must remain unopened in this integration test.');
  },
  clear: () => undefined,
};

afterEach(cleanup);

it('renders the backend contract through the shared History and Overlay renderer', async () => {
  const page = parseActionConversationPage(fixture);
  const view = projectActionConversationView([page]);
  render(
    <UiLanguageProvider initialLanguage="en">
      <ActionConversationView view={view} toolOutputLoader={UNUSED_TOOL_OUTPUT_LOADER} />
    </UiLanguageProvider>
  );

  const conversation = screen.getByRole('region', { name: 'Action conversation' });
  const user = within(conversation).getByRole('article', { name: 'You' });
  const finalOutput = within(conversation).getByRole('region', {
    name: /^Final answer, Run 1:/,
  });
  const agentWork = within(conversation).getByRole('button', {
    name: /^Pantaray's work 1, Run 1:/,
  });

  expect(user).toHaveTextContent('Please inspect the Demo App repository');
  expect(finalOutput).toHaveTextContent('Canonical final answer');
  expect(within(user).getByRole('list', { name: '1 attached image' })).toBeVisible();
  expect(conversation).not.toHaveTextContent('/private/captures/secret-window.png');
  expect(conversation).not.toHaveTextContent('Private App');
  expect(within(conversation).queryByText('Inspect repository')).toBeNull();
  expect(agentWork).toHaveAttribute('aria-expanded', 'false');

  agentWork.focus();
  await userEvent.keyboard('{Enter}');

  expect(agentWork).toHaveAttribute('aria-expanded', 'true');
  expect(within(conversation).getByText('Inspect repository')).toBeVisible();
  expect(within(conversation).queryByLabelText(/^Tool output,/, { selector: 'pre' })).toBeNull();
});

it('projects the newest-first backend page as chronological history', () => {
  const page = parseActionConversationPage({
    action: {
      action_id: 'action-order',
      suggestion_id: null,
      status: 'success',
      latest_run_id: 'run-latest',
      approved_suggestion: null,
      resumable: false,
    },
    runs: [
      {
        run_id: 'run-latest',
        status: 'success',
        started_at: '2026-09-01T02:00:00.000000Z',
        completed_at: '2026-09-01T02:01:00.000000Z',
        completion_event_id: 'event-latest',
        entries: [
          {
            step_kind: 'tool',
            step_id: 'step-4',
            step_number: 4,
            label: 'Read article',
            status: 'success',
            output_available: false,
          },
          {
            step_kind: 'user',
            approved_suggestion: null,
            step_id: 'step-3',
            step_number: 3,
            message_id: 'message-2',
            accepted_sequence: 2,
            content: 'Second request',
            images: [],
            project_refs: [],
            status: 'adopted',
          },
        ],
        final_output: 'Second answer',
        error: null,
      },
      {
        run_id: 'run-older',
        status: 'success',
        started_at: '2026-09-01T01:00:00.000000Z',
        completed_at: '2026-09-01T01:01:00.000000Z',
        completion_event_id: 'event-older',
        entries: [
          {
            step_kind: 'user',
            approved_suggestion: null,
            step_id: 'step-1',
            step_number: 1,
            message_id: 'message-1',
            accepted_sequence: 1,
            content: 'First request',
            images: [],
            project_refs: [],
            status: 'adopted',
          },
        ],
        final_output: 'First answer',
        error: null,
      },
    ],
    unadopted_messages: [],
    next_cursor: null,
  });

  const view = projectActionConversationView([page]);

  expect(view.items.map((item) => (item.kind === 'run' ? item.runId : item.key))).toEqual([
    'run-older',
    'run-latest',
  ]);
  const latest = view.items[1];
  expect(latest.kind).toBe('run');
  expect(latest.kind === 'run' ? latest.lines.map((line) => line.kind) : []).toEqual([
    'user',
    'tool',
    'final_output',
  ]);
  expect(
    view.items.flatMap((item) =>
      item.kind === 'run'
        ? item.lines.flatMap((line) => (line.kind === 'final_output' ? [line.text] : []))
        : []
    )
  ).toEqual(['First answer', 'Second answer']);
});
