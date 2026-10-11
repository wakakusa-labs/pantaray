import { cleanup, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type {
  ActionWithdrawMessageRequest,
  ActionWithdrawMessageResult,
} from '../../../electron/src/actions/actionContracts';
import type {
  ActionConversationUserItem,
  ActionConversationView as ActionConversationViewModel,
} from '../../../electron/src/actions/actionConversationModel';
import type { ActionToolOutputLoader } from '../../../electron/src/actions/actionToolOutputLoader';

import { UiLanguageProvider } from '@/context/UiLanguageContext';

import { ActionConversationView } from './ActionConversationView';

const TOOL_OUTPUT_LOADER: ActionToolOutputLoader = {
  load: async () => ({ kind: 'unavailable', reason: 'no_output' }),
  retry: async () => ({ kind: 'unavailable', reason: 'no_output' }),
  clear: () => undefined,
};

type UserStatus = Extract<ActionConversationUserItem, { source: 'canonical' }>['entry']['status'];

function user(key: string, status: UserStatus): ActionConversationUserItem {
  return {
    kind: 'user',
    source: 'canonical',
    key,
    visibility: 'always',
    entry: {
      step_kind: 'user',
      chat_note: null,
      approved_suggestion: null,
      step_id: `step-${key}`,
      step_number: status === 'adopted' ? 1 : null,
      message_id: `message-${key}`,
      accepted_sequence: 1,
      content: `text ${key}`,
      images: [],
      project_refs: [],
      status,
    },
  };
}

const SENDING: ActionConversationUserItem = {
  kind: 'user',
  source: 'optimistic',
  key: 'message-sending',
  visibility: 'always',
  submission: {
    state: 'submitting',
    request: {
      target: { kind: 'existing', action_id: 'action-1', expected_process_id: null },
      message: { version: 1, message_id: 'message-sending', content: 'text sending', images: [] },
    },
  },
};

function view(items: ActionConversationViewModel['items']): ActionConversationViewModel {
  return {
    action: {
      action_id: 'action-1',
      suggestion_id: null,
      status: 'canceled',
      latest_run_id: 'run-1',
      approved_suggestion: null,
      resumable: false,
    },
    items,
    nextCursor: null,
  };
}

function bridge(answer: () => Promise<ActionWithdrawMessageResult>) {
  const withdrawMessage =
    vi.fn<(request: ActionWithdrawMessageRequest) => Promise<unknown>>(answer);
  vi.stubGlobal('electron', { actions: { withdrawMessage } });
  return withdrawMessage;
}

function renderView(model: ActionConversationViewModel, language: 'en' | 'ja' = 'en') {
  const element = (next: ActionConversationViewModel) => (
    <UiLanguageProvider initialLanguage={language}>
      <ActionConversationView view={next} toolOutputLoader={TOOL_OUTPUT_LOADER} />
    </UiLanguageProvider>
  );
  const rendered = render(element(model));
  return { rerender: (next: ActionConversationViewModel) => rendered.rerender(element(next)) };
}

const bubble = (key: string) => screen.getByText(`text ${key}`).closest('article')!;

describe('withdrawing a message no run has taken', () => {
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  it('offers ✕ only on pending and not-run messages, in Japanese too', () => {
    bridge(async () => ({ kind: 'withdrawn' }));
    renderView(
      view([
        user('ran', 'adopted'),
        user('late', 'not_executed'),
        user('queued', 'pending'),
        SENDING,
      ]),
      'ja'
    );

    const buttons = screen.getAllByRole('button', { name: 'このメッセージを取り消す' });
    expect(buttons).toHaveLength(2);
    expect(within(bubble('late')).getByText('未実行')).toBeVisible();
    expect(within(bubble('late')).getByRole('button')).toBe(buttons[0]);
    expect(within(bubble('queued')).getByRole('button')).toBe(buttons[1]);
    expect(within(bubble('ran')).queryByRole('button')).toBeNull();
    expect(within(bubble('sending')).queryByRole('button')).toBeNull();
  });

  it('withdraws the message and removes it at once', async () => {
    const withdrawMessage = bridge(async () => ({ kind: 'withdrawn' }));
    renderView(view([user('ran', 'adopted'), user('late', 'not_executed')]));

    await userEvent.click(screen.getByRole('button', { name: 'Withdraw this message' }));

    expect(withdrawMessage).toHaveBeenCalledWith({ actionId: 'action-1', stepId: 'step-late' });
    await waitFor(() => expect(screen.queryByText('text late')).toBeNull());
    expect(screen.getByText('text ran')).toBeVisible();
  });

  it('keeps a message a run took first, which the refresh then shows as run', async () => {
    bridge(async () => ({ kind: 'adopted' }));
    const { rerender } = renderView(view([user('late', 'pending')]));

    await userEvent.click(screen.getByRole('button', { name: 'Withdraw this message' }));
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Withdraw this message' })).toHaveAttribute(
        'aria-disabled',
        'false'
      )
    );
    expect(screen.getByText('text late')).toBeVisible();

    rerender(view([user('late', 'adopted')]));
    expect(screen.getByText('text late')).toBeVisible();
    expect(screen.queryByRole('button', { name: 'Withdraw this message' })).toBeNull();
  });
});
