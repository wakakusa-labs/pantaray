import { createRef, useState } from 'react';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, it, vi } from 'vitest';

import type { ConversationHistoryListItem } from '../../../electron/src/history/historyContracts';
import { UiLanguageProvider } from '@/context/UiLanguageContext';
import { ComposerMessageField } from './ComposerMessageField';
import { mentionRefs, type ComposerMention } from './composerMentions';

const TASK = 'pantaray://task/a-1';
const row = (title: string, actionId = 'a-1'): ConversationHistoryListItem => ({
  kind: 'conversation',
  action_id: actionId,
  title,
  updated_at: '2026-10-11T00:00:00.000Z',
  status: 'idle',
  latest_completion_event_id: null,
  status_line: null,
  artifact_names: [],
});

afterEach(() => {
  cleanup();
  delete (window as { electron?: unknown }).electron;
});

function renderField(
  readItem: (key: { kind: string; id: string }) => Promise<ConversationHistoryListItem | null>
) {
  const read = vi.fn(readItem);
  (window as { electron?: unknown }).electron = { history: { readItem: read } };
  const changes = vi.fn<(mentions: ComposerMention[]) => void>();
  const state = {
    get mentions() {
      return changes.mock.lastCall?.[0] ?? [];
    },
  };
  function Harness() {
    const [value, setValue] = useState('');
    const [mentions, setMentions] = useState<ComposerMention[]>([]);
    return (
      <form>
        <ComposerMessageField
          id="field"
          textareaRef={createRef()}
          value={value}
          mentions={mentions}
          readOnly={false}
          placeholder=""
          invalid={false}
          describedBy={undefined}
          onChange={(draft, next) => {
            setValue(draft);
            setMentions(next);
            changes(next);
          }}
          onKeyDown={() => {}}
          onPasteFiles={() => {}}
          onAddProject={() => {}}
        />
      </form>
    );
  }
  render(
    <UiLanguageProvider initialLanguage="ja">
      <Harness />
    </UiLanguageProvider>
  );
  return { input: screen.getByRole('textbox') as HTMLTextAreaElement, read, state };
}

const type = (input: HTMLTextAreaElement, value: string) =>
  fireEvent.change(input, { target: { value, selectionStart: value.length } });

it('a pasted link becomes a chip titled from History, the rest of the text kept', async () => {
  const { input, read, state } = renderField(async () => row('見積書を直す\n詳細'));
  type(input, `前 ${TASK} を見て`);
  await waitFor(() => expect(input.value).toBe('前 見積書を直す を見て'));
  expect(read).toHaveBeenCalledWith({ kind: 'conversation', id: 'a-1' });
  expect(state.mentions).toEqual([
    { kind: 'action', id: 'a-1', displayName: '見積書を直す', start: 2, end: 8 },
  ]);
});

it('a link History does not hold, or cannot read, stays the text the user wrote', async () => {
  const { input, read } = renderField(async ({ id }) => {
    if (id === 'gone') return null;
    throw new Error('ipc down');
  });
  const text = 'pantaray://task/gone pantaray://suggestion/s-9';
  type(input, text);
  await waitFor(() => expect(read).toHaveBeenCalledTimes(2));
  await act(async () => {});
  expect(input.value).toBe(text);
});

it.each([
  ['first link first', ['a-1', 'a-2']],
  ['second link first', ['a-2', 'a-1']],
])(
  'two pasted links both become chips, whichever answer comes first (%s)',
  async (_order, answerOrder) => {
    const answers = new Map<string, (row: ConversationHistoryListItem) => void>();
    const { input, state } = renderField(
      ({ id }) => new Promise((resolve) => answers.set(id, resolve))
    );
    type(input, 'pantaray://task/a-1 と pantaray://task/a-2 を比べて');
    await waitFor(() => expect(answers.size).toBe(2));
    for (const id of answerOrder) {
      await act(async () => answers.get(id)!(row(`T${id}`, id)));
    }
    expect(input.value).toBe('Ta-1 と Ta-2 を比べて');
    expect(state.mentions).toEqual([
      { kind: 'action', id: 'a-1', displayName: 'Ta-1', start: 0, end: 4 },
      { kind: 'action', id: 'a-2', displayName: 'Ta-2', start: 7, end: 11 },
    ]);
    expect(mentionRefs({ text: input.value, draftOffset: 0 }, state.mentions).work_refs).toEqual([
      { kind: 'action', id: 'a-1', title: 'Ta-1', start: 0, end: 4 },
      { kind: 'action', id: 'a-2', title: 'Ta-2', start: 7, end: 11 },
    ]);
  }
);

it('a link to a suggestion that became a task becomes that task’s chip', async () => {
  const { input, state } = renderField(async () => row('提案から始めた作業'));
  type(input, 'pantaray://suggestion/s-1');
  await waitFor(() => expect(input.value).toBe('提案から始めた作業'));
  expect(state.mentions[0]).toMatchObject({ kind: 'action', id: 'a-1' });
});

it('a link still being typed is not converted until it stands complete', async () => {
  const { input, state } = renderField(async ({ id }) => (id === 'a-1' ? row('Q3') : null));
  // The read for the full id answers after more of the id was typed: that span is left alone.
  type(input, 'pantaray://task/a-1');
  type(input, 'pantaray://task/a-12');
  await act(async () => {});
  expect(input.value).toBe('pantaray://task/a-12');
  expect(state.mentions).toEqual([]);
});

it('「テキストに戻す」 turns the chip the caret touches back into its link, which stays text', async () => {
  const { input, read, state } = renderField(async () => row('Q3'));
  type(input, `${TASK} だけ`);
  await waitFor(() => expect(input.value).toBe('Q3 だけ'));
  input.focus();
  input.setSelectionRange(2, 2);
  fireEvent.select(input);
  // It waits a moment, so a caret or pointer passing over chips does not make it flicker.
  expect(screen.queryByRole('button', { name: 'テキストに戻す' })).toBeNull();
  const pill = await screen.findByRole('button', { name: 'テキストに戻す' });
  expect(pill).toHaveTextContent('テキストに戻す');
  await userEvent.click(pill);
  expect(input.value).toBe(`${TASK} だけ`);
  expect(state.mentions).toEqual([]);
  expect(document.activeElement).toBe(input);
  await act(async () => {});
  expect(input.value).toBe(`${TASK} だけ`);
  expect(read).toHaveBeenCalledTimes(1);
});

it('Backspace right after a chip removes it whole, as a project chip', async () => {
  const { input, state } = renderField(async () => row('Q3'));
  type(input, `${TASK} x`);
  await waitFor(() => expect(input.value).toBe('Q3 x'));
  input.focus();
  input.setSelectionRange(2, 2);
  fireEvent.keyDown(input, { key: 'Backspace' });
  expect(input.value).toBe(' x');
  expect(state.mentions).toEqual([]);
});
