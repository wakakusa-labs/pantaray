import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type {
  ActionConversationRunItem,
  ActionConversationToolItem,
  ActionConversationUserItem,
  ActionConversationView as ActionConversationViewModel,
} from '../../../electron/src/actions/actionConversationModel';
import type { ActionToolOutputLoader } from '../../../electron/src/actions/actionToolOutputLoader';

import { UiLanguageProvider } from '@/context/UiLanguageContext';

import { ActionConversationView } from './ActionConversationView';

const EMPTY_TOOL_OUTPUT_LOADER: ActionToolOutputLoader = {
  load: async () => ({ kind: 'unavailable', reason: 'no_output' }),
  retry: async () => ({ kind: 'unavailable', reason: 'no_output' }),
  clear: () => undefined,
};

type CanonicalUserItem = Extract<ActionConversationUserItem, { source: 'canonical' }>;

const canonicalUser = (key: string, content: string): CanonicalUserItem => ({
  kind: 'user',
  source: 'canonical',
  key,
  visibility: 'always',
  entry: {
    step_kind: 'user',
    approved_suggestion: null,
    step_id: `step-${key}`,
    step_number: 1,
    message_id: `message-${key}`,
    accepted_sequence: 1,
    content,
    images: [],
    project_refs: [],
    status: 'adopted',
  },
});

const tool = (
  key: string,
  label: string,
  status: ActionConversationToolItem['entry']['status'] = 'success',
  stepNumber = 1,
  images: ActionConversationToolItem['entry']['images'] = [],
  subject: string | null = null,
  outputPreview: string | null = null,
  outcome: ActionConversationToolItem['entry']['outcome'] = 'completed'
): ActionConversationToolItem => ({
  kind: 'tool',
  key,
  visibility: 'agent_work',
  entry: {
    step_kind: 'tool',
    step_id: `step-${key}`,
    step_number: stepNumber,
    label,
    status,
    outcome,
    subject,
    output_preview: outputPreview,
    output_available: status !== 'processing',
    images,
  },
});

const viewWith = (
  items: ActionConversationViewModel['items'],
  status: NonNullable<ActionConversationViewModel['action']>['status'] = 'processing',
  suggestionId: string | null = null
): ActionConversationViewModel => ({
  action: {
    action_id: 'action-1',
    suggestion_id: suggestionId,
    status,
    latest_run_id: 'run-1',
    approved_suggestion: null,
    resumable: false,
  },
  items,
  nextCursor: null,
});

function renderView(
  view: ActionConversationViewModel,
  language: 'en' | 'ja' = 'en',
  toolOutputLoader: ActionToolOutputLoader = EMPTY_TOOL_OUTPUT_LOADER
) {
  return render(
    <UiLanguageProvider initialLanguage={language}>
      <ActionConversationView view={view} toolOutputLoader={toolOutputLoader} />
    </UiLanguageProvider>
  );
}

/** 行そのもの。状態語は行の本体の外に置くので、本文の親を辿るだけでは届かない。 */
function toolLineOf(node: HTMLElement): HTMLElement {
  const line = node.closest<HTMLElement>('.action-conversation__tool-line');
  if (line === null) throw new Error('the node is not inside a tool row');
  return line;
}

describe('ActionConversationView', () => {
  afterEach(cleanup);

  it('keeps an unexecuted approval status visible without an empty user bubble', () => {
    const user = canonicalUser('approval', '');
    Object.assign(user.entry, {
      content: null,
      status: 'not_executed',
      step_number: null,
      approved_suggestion: { suggestion_id: 'suggestion-1', content: 'Review the changes' },
    });
    renderView(viewWith([user], 'canceled', 'suggestion-1'));
    expect(screen.getByText('Not executed')).toBeInTheDocument();
    expect(screen.queryByRole('article', { name: 'You' })).not.toBeInTheDocument();
  });

  it.each(['en', 'ja'] as const)(
    'names repeated work and tool groups distinctly in %s',
    async (language) => {
      const run: ActionConversationRunItem = {
        kind: 'run',
        runId: 'run-1',
        status: 'success',
        startedAt: '2026-08-30T00:00:00.000000Z',
        completedAt: '2026-08-30T00:01:00.000000Z',
        lines: [
          canonicalUser('request', 'Check files'),
          tool('a', 'read', 'success', 2),
          tool('b', 'read', 'success', 3),
          { kind: 'assistant', key: 'comment', visibility: 'always', text: 'Check more' },
          tool('c', 'read', 'success', 5),
          tool('d', 'read', 'success', 6),
          canonicalUser('reply', 'Check these too'),
          tool('e', 'read', 'success', 8),
          tool('f', 'read', 'success', 9),
          tool('g', 'read', 'success', 10),
          tool('h', 'read', 'success', 11),
          {
            kind: 'final_output',
            runId: 'run-1',
            status: 'success',
            visibility: 'always',
            text: 'Done',
          },
        ],
      };
      renderView(viewWith([run], 'success'), language);
      const work = screen.getAllByRole('button', { name: /^(Pantaray's work|Pantarayの作業)/ });
      expect(new Set(work.map((button) => button.getAttribute('aria-label'))).size).toBe(
        work.length
      );
      for (const button of work) await userEvent.click(button);
      const groups = screen.getAllByRole('button', { name: /^(Read a file|ファイルを読む) 2,/ });
      expect(groups).toHaveLength(2);
      expect(new Set(groups.map((button) => button.getAttribute('aria-label'))).size).toBe(
        groups.length
      );
    }
  );

  it.each(['work', 'commentary', 'tool'] as const)(
    'keeps work chronological and restores %s focus when final work folds around user replies',
    async (focusTarget) => {
      const active: ActionConversationRunItem = {
        kind: 'run',
        runId: 'run-1',
        status: 'running',
        startedAt: '2026-08-30T00:00:00.000000Z',
        completedAt: null,
        lines: [
          canonicalUser('request', 'Please investigate'),
          { kind: 'assistant', key: 'first', visibility: 'always', text: 'Checking files' },
          tool('read', 'Read files', 'success', 3),
          {
            kind: 'assistant',
            key: 'second',
            visibility: 'always',
            text: '[Checking tests](https://example.com)',
          },
          tool('test', 'Run tests', 'success', 5),
          canonicalUser('reply', 'Include the result'),
          { kind: 'assistant', key: 'third', visibility: 'always', text: 'Checking the result' },
          tool('result', 'Read result', 'processing', 8),
        ],
      };
      const { rerender } = renderView(viewWith([active]));
      const disclosures = screen.getAllByRole('button', { name: /^Pantaray's work 1,/ });
      expect(disclosures).toHaveLength(3);
      for (const disclosure of disclosures) await userEvent.click(disclosure);
      const run = screen.getByRole('article', { name: /^Run 1:/ });
      const text = run.textContent!;
      expect(text.indexOf('Read files')).toBeLessThan(text.indexOf('Checking tests'));
      expect(text.indexOf('Checking tests')).toBeLessThan(text.indexOf('Run tests'));
      expect(text.indexOf('Run tests')).toBeLessThan(text.indexOf('Include the result'));
      expect(text.indexOf('Include the result')).toBeLessThan(text.indexOf('Checking the result'));
      if (focusTarget === 'commentary')
        screen.getByRole('link', { name: 'Checking tests' }).focus();
      if (focusTarget === 'tool')
        screen.getByRole('button', { name: /^Run tests, Step 5,/ }).focus();

      rerender(
        <UiLanguageProvider initialLanguage="en">
          <ActionConversationView
            view={viewWith(
              [
                {
                  ...active,
                  status: 'success',
                  completedAt: '2026-08-30T00:00:01.000000Z',
                  lines: [
                    ...active.lines.slice(0, -1),
                    tool('result', 'Read result', 'success', 8),
                    {
                      kind: 'final_output',
                      runId: 'run-1',
                      status: 'success',
                      visibility: 'always',
                      text: 'The final result',
                    },
                  ],
                },
              ],
              'success'
            )}
            toolOutputLoader={EMPTY_TOOL_OUTPUT_LOADER}
          />
        </UiLanguageProvider>
      );
      expect(screen.getByText('Please investigate')).toBeVisible();
      expect(screen.getByText('Include the result')).toBeVisible();
      expect(screen.getByText('The final result')).toBeVisible();
      expect(screen.queryByText('Checking files')).toBeNull();
      expect(screen.queryByText('Checking tests')).toBeNull();
      expect(screen.queryByText('Checking the result')).toBeNull();
      const completedWork = screen.getAllByRole('button', { name: /^Pantaray's work/ });
      expect(completedWork).toHaveLength(2);
      expect(completedWork[focusTarget === 'work' ? 1 : 0]).toHaveFocus();
      completedWork[0].focus();
      await userEvent.keyboard('{Enter}');
      expect(screen.getByText('Checking files')).toBeVisible();
      expect(screen.getByText('Checking tests')).toBeVisible();
      expect(screen.queryByText('Checking the result')).toBeNull();
      completedWork[1].focus();
      await userEvent.keyboard(' ');
      expect(screen.getByText('Checking the result')).toBeVisible();
    }
  );

  it.each([
    ['en', 'Assistant', "Pantaray's work"],
    ['ja', 'アシスタント', 'Pantarayの作業'],
  ] as const)(
    'keeps the assistant message visible before the reply in %s',
    async (language, label, workLabel) => {
      const run: ActionConversationRunItem = {
        kind: 'run',
        runId: 'run-1',
        status: 'success',
        startedAt: '2026-08-30T00:00:00.000000Z',
        completedAt: '2026-08-30T00:00:01.000000Z',
        lines: [
          {
            kind: 'assistant',
            key: 'assistant-first',
            visibility: 'always',
            text: '**Earlier observation**',
          },
          canonicalUser('reply', 'Tell me more'),
          tool('read', 'read', 'success', 3),
        ],
      };
      renderView(viewWith([run], 'success'), language);
      const assistant = screen.getByRole('region', { name: new RegExp(`^${label},`) });
      const reply = screen.getByText('Tell me more');
      expect(assistant).toBeVisible();
      expect(within(assistant).getByText('Earlier observation').tagName).toBe('STRONG');
      expect(
        assistant.compareDocumentPosition(reply) & Node.DOCUMENT_POSITION_FOLLOWING
      ).toBeTruthy();
      const disclosure = screen.getByRole('button', { name: new RegExp(`^${workLabel} 1,`) });
      disclosure.focus();
      await userEvent.keyboard('{Enter}');
      await userEvent.keyboard('{Enter}');
      expect(disclosure).toHaveAttribute('aria-expanded', 'false');
      expect(assistant).toBeVisible();
      expect(screen.getAllByText('Earlier observation')).toHaveLength(1);
    }
  );

  it('keeps interleaved USER and Tool lines chronological while keyboard-expanding agent work', async () => {
    const run: ActionConversationRunItem = {
      kind: 'run',
      runId: 'run-1',
      status: 'success',
      startedAt: '2026-08-30T00:00:00.000000Z',
      completedAt: '2026-08-30T00:00:01.000000Z',
      lines: [
        canonicalUser('one', 'First request'),
        tool('search', 'Search files', 'success', 1),
        canonicalUser('two', 'Follow-up'),
        tool('search-again', 'Search files', 'success', 2),
        {
          kind: 'final_output',
          runId: 'run-1',
          status: 'success',
          visibility: 'always',
          text: '**Done**',
        },
      ],
    };
    renderView(viewWith([run], 'success'));

    expect(screen.getByText('First request')).toBeVisible();
    expect(screen.getByText('Follow-up')).toBeVisible();
    expect(screen.getByRole('region', { name: /^Final answer, Run 1:/ })).toHaveTextContent('Done');
    expect(screen.queryByText('Search files')).toBeNull();
    const [disclosure, laterDisclosure] = screen.getAllByRole('button', {
      name: /^Pantaray's work 1, Run 1:/,
    });
    expect(disclosure).toHaveAttribute('aria-expanded', 'false');

    disclosure.focus();
    await userEvent.keyboard('{Enter}');
    laterDisclosure.focus();
    await userEvent.keyboard(' ');

    expect(disclosure).toHaveAttribute('aria-expanded', 'true');
    expect(screen.getAllByText('Search files')).toHaveLength(2);
    expect(screen.getByRole('button', { name: /^Search files, Step 1, Run 1:/ })).toBeVisible();
    expect(screen.getByRole('button', { name: /^Search files, Step 2, Run 1:/ })).toBeVisible();
    // 出力は行そのものが開く。「ツール出力」の行はもう置かない。
    expect(screen.queryByRole('button', { name: /^Tool output/ })).toBeNull();
    // steering は run.lines の時系列のまま描く。種類ごとにまとめると USER が全部先頭へ寄ってしまう。
    const runArticle = disclosure.closest('.action-conversation__run')!;
    const text = runArticle.textContent ?? '';
    expect(text.indexOf('First request')).toBeLessThan(text.indexOf("Pantaray's work"));
    expect(text.indexOf('Search files')).toBeLessThan(text.indexOf('Follow-up'));
    expect(text.indexOf('Follow-up')).toBeLessThan(text.lastIndexOf('Search files'));
    expect(text.lastIndexOf('Search files')).toBeLessThan(text.indexOf('Done'));
  });

  it('keeps newly running and completed work collapsed until explicitly opened', async () => {
    let run: ActionConversationRunItem = {
      kind: 'run',
      runId: 'run-1',
      status: 'running',
      startedAt: '2026-08-30T00:00:00.000000Z',
      completedAt: null,
      lines: [],
    };
    const { rerender } = renderView(viewWith([run]));
    const update = () =>
      rerender(
        <UiLanguageProvider initialLanguage="en">
          <ActionConversationView
            view={viewWith([run])}
            toolOutputLoader={EMPTY_TOOL_OUTPUT_LOADER}
          />
        </UiLanguageProvider>
      );
    for (let i = 1; i <= 3; i += 1) {
      run = { ...run, lines: [...run.lines, tool(`step-${i}`, `Work ${i}`, 'processing', i)] };
      update();
      const disclosure = screen.getByRole('button', { name: new RegExp(`^Pantaray's work ${i},`) });
      expect(disclosure).toHaveAttribute('aria-expanded', 'false');
      expect(screen.queryByText(`Work ${i}`)).toBeNull();
      run = {
        ...run,
        lines: [...run.lines.slice(0, -1), tool(`step-${i}`, `Work ${i}`, 'success', i)],
      };
      update();
      expect(screen.getByRole('button', { name: new RegExp(`^Pantaray's work ${i},`) })).toBe(
        disclosure
      );
      expect(screen.queryByText(`Work ${i}`)).toBeNull();
    }
    await userEvent.click(screen.getByRole('button', { name: /^Pantaray's work 3,/ }));
    run = { ...run, lines: [...run.lines, tool('step-4', 'Work 4', 'processing', 4)] };
    update();
    expect(screen.getByRole('button', { name: /^Pantaray's work 4,/ })).toHaveAttribute(
      'aria-expanded',
      'true'
    );
    expect(screen.getByText('Work 4')).toBeVisible();
  });

  it("moves only inner Tool focus to Pantaray's work before terminal auto-collapse", async () => {
    const loader: ActionToolOutputLoader = {
      load: async () => ({
        kind: 'text',
        content: 'one\ntwo\nthree\nfour\nfive\nsix',
        truncated: false,
      }),
      retry: async () => ({ kind: 'unavailable', reason: 'no_output' }),
      clear: () => undefined,
    };
    const active: ActionConversationRunItem = {
      kind: 'run',
      runId: 'run-1',
      status: 'running',
      startedAt: '2026-08-30T00:00:00.000000Z',
      completedAt: null,
      lines: [
        tool('done', 'Read database', 'success', 1),
        tool('active', 'Query database', 'processing', 2),
      ],
    };
    const older: ActionConversationRunItem = {
      kind: 'run',
      runId: 'run-older',
      status: 'canceled',
      startedAt: '2026-08-29T00:00:00.000000Z',
      completedAt: '2026-08-29T00:00:01.000000Z',
      lines: [
        tool('old', 'Old database'),
        {
          kind: 'terminal_outcome',
          runId: 'run-older',
          status: 'canceled',
          visibility: 'always',
          code: 'canceled',
          text: 'Old canceled run',
        },
      ],
    };
    const { rerender, unmount } = renderView(viewWith([active, older]), 'en', loader);
    // 実行中の行も同じ見出しに入り、明示的に開いたときだけ見える。
    expect(screen.queryByText('Read database')).toBeNull();
    expect(screen.queryByText('Query database')).toBeNull();
    await userEvent.click(screen.getByRole('button', { name: /^Pantaray's work 2, Run 1:/ }));
    expect(screen.getByText('Read database')).toBeVisible();
    // 実行状態はオーバーレイのヘッダーが持つ。会話が目に見える形で重ねて出すのは行の状態語だけで、
    // 実行そのものの状態は読み上げ専用の 1 か所にとどめる。
    expect(
      screen
        .getAllByText('Running')
        .filter((node) => !node.classList.contains('action-conversation__sr-only'))
    ).toHaveLength(1);
    expect(screen.getByRole('article', { name: /^Run 1:/ })).toBeVisible();
    expect(screen.getByRole('article', { name: /^Run 2:/ })).toBeVisible();
    const disclosure = screen.getByRole('button', { name: /^Pantaray's work 2, Run 1:/ });
    expect(disclosure).toHaveAttribute('aria-expanded', 'true');
    expect(
      within(toolLineOf(screen.getByText('Query database'))).getByRole('status')
    ).toHaveTextContent('Running');

    const failedTool = {
      ...active,
      lines: [active.lines[0], tool('active', 'Query database', 'timeout', 2)],
    } as const;
    rerender(
      <UiLanguageProvider initialLanguage="en">
        <ActionConversationView view={viewWith([failedTool, older])} toolOutputLoader={loader} />
      </UiLanguageProvider>
    );
    expect(screen.getByText('Timed out')).toBeVisible();

    const updatedActive = {
      ...active,
      lines: [...failedTool.lines, tool('new', 'New tool', 'processing', 3)],
    } satisfies ActionConversationRunItem;
    rerender(
      <UiLanguageProvider initialLanguage="en">
        <ActionConversationView view={viewWith([updatedActive, older])} toolOutputLoader={loader} />
      </UiLanguageProvider>
    );
    expect(screen.getByRole('button', { name: /^Pantaray's work 3, Run 1:/ })).toBe(disclosure);
    const outputDisclosure = screen.getByRole('button', {
      name: /^Read database, Step 1, Run 1:/,
    });
    await userEvent.click(outputDisclosure);
    const output = await screen.findByLabelText(/^Tool output, Read database, Step 1, Run 1:/, {
      selector: 'pre',
    });
    output.focus();

    const terminal: ActionConversationRunItem = {
      ...active,
      status: 'success',
      completedAt: '2026-08-30T00:00:01.000000Z',
      lines: [
        tool('done', 'Read database', 'success', 1),
        tool('active', 'Query database', 'success', 2),
        {
          kind: 'final_output',
          runId: 'run-1',
          status: 'success',
          visibility: 'always',
          text: 'Ready',
        },
      ],
    };
    rerender(
      <UiLanguageProvider initialLanguage="en">
        <ActionConversationView
          view={viewWith([terminal, older], 'success')}
          toolOutputLoader={loader}
        />
      </UiLanguageProvider>
    );

    expect(screen.getByText('Ready')).toBeVisible();
    expect(screen.queryByText('Read database')).toBeNull();
    // 終わった実行は何も読み上げない。読み上げ領域は、次の実行のために空のまま置いておく。
    expect(screen.getByRole('status')).toBeEmptyDOMElement();
    expect(screen.getByRole('button', { name: /^Pantaray's work 2, Run 1:/ })).toHaveAttribute(
      'aria-expanded',
      'false'
    );
    expect(disclosure).toHaveFocus();

    await userEvent.click(screen.getByRole('button', { name: /^Pantaray's work 1, Run 2:/ }));
    screen.getByRole('button', { name: /^Old database, Step 1, Run 2:/ }).focus();
    rerender(
      <UiLanguageProvider initialLanguage="en">
        <ActionConversationView view={viewWith([terminal], 'success')} toolOutputLoader={loader} />
      </UiLanguageProvider>
    );
    expect(screen.getByRole('region', { name: 'Action conversation' })).toHaveFocus();
    unmount();

    const outside = document.body.appendChild(document.createElement('button'));
    const externalFocusView = renderView(viewWith([active]), 'en', loader);
    outside.focus();
    externalFocusView.rerender(
      <UiLanguageProvider initialLanguage="en">
        <ActionConversationView view={viewWith([terminal], 'success')} toolOutputLoader={loader} />
      </UiLanguageProvider>
    );

    expect(outside).toHaveFocus();
    outside.remove();
  });

  it('colors only the referenced project names, counting offsets in code points', () => {
    // The emoji before each name are two UTF-16 units but one code point, so UTF-16
    // slicing would shift both spans.
    const content = '🙂 Compare Atlas with 📦 Borealis notes';
    const referenced = canonicalUser('refs', content);
    referenced.entry.project_refs = [
      { display_name: 'Atlas', start: 10, end: 15 },
      { display_name: 'Borealis', start: 23, end: 31 },
    ];
    const plain = canonicalUser('plain', 'No projects here');
    renderView(viewWith([referenced, plain]));

    const [referencedText, plainText] = screen
      .getAllByRole('article', { name: 'You' })
      .map((article) => article.querySelector('p')!);
    expect(referencedText).toHaveTextContent(content, { normalizeWhitespace: false });
    const refs = referencedText.querySelectorAll('.action-conversation__project-ref');
    expect(Array.from(refs, (ref) => ref.textContent)).toEqual(['Atlas', 'Borealis']);
    expect(plainText.children).toHaveLength(0);
    expect(plainText).toHaveTextContent('No projects here');
  });

  it('colors project names in a message that is still being sent', () => {
    const optimistic: ActionConversationUserItem = {
      kind: 'user',
      source: 'optimistic',
      key: 'message-optimistic',
      visibility: 'always',
      submission: {
        state: 'submitting',
        request: {
          target: { kind: 'existing', action_id: 'action-1', expected_process_id: 'process-1' },
          message: {
            version: 1,
            message_id: 'message-optimistic',
            content: 'Open Atlas',
            images: [],
            project_refs: [
              {
                project_id: 'project-1',
                display_name: 'Atlas',
                paths: ['/workspace/atlas'],
                start: 5,
                end: 10,
              },
            ],
          },
        },
      },
    };
    renderView(viewWith([optimistic]));

    const text = screen.getByRole('article', { name: 'You' }).querySelector('p')!;
    expect(text).toHaveTextContent('Open Atlas');
    const refs = text.querySelectorAll('.action-conversation__project-ref');
    expect(Array.from(refs, (ref) => ref.textContent)).toEqual(['Atlas']);
  });

  it('labels pending, optimistic, and active tool states in Japanese', async () => {
    const pendingBase = canonicalUser('pending', '次の依頼');
    const pending: ActionConversationUserItem = {
      ...pendingBase,
      entry: {
        ...pendingBase.entry,
        images: [{ kind: 'image', storage_path: '/private/capture.png' }],
        status: 'not_executed',
      },
    };
    const optimistic: ActionConversationUserItem = {
      kind: 'user',
      source: 'optimistic',
      key: 'message-optimistic',
      visibility: 'always',
      submission: {
        state: 'submitting',
        request: {
          target: { kind: 'existing', action_id: 'action-1', expected_process_id: 'process-1' },
          message: {
            version: 1,
            message_id: 'message-optimistic',
            content: '再送する内容',
            images: [],
          },
        },
      },
    };
    const run: ActionConversationRunItem = {
      kind: 'run',
      runId: 'run-1',
      status: 'approval_pending',
      startedAt: '2026-08-30T00:00:00.000000Z',
      completedAt: null,
      lines: [tool('timeout', '外部ツール', 'timeout')],
    };
    const { rerender } = renderView(viewWith([run, pending, optimistic]), 'ja');

    expect(screen.getByRole('region', { name: 'アクションの会話' })).toBeVisible();
    // 失敗した行だけの実行も、作業の見出しから開く。
    expect(screen.queryByText('タイムアウト')).toBeNull();
    await userEvent.click(screen.getByRole('button', { name: /^Pantarayの作業 1,/ }));
    expect(screen.getByText('タイムアウト')).toHaveClass('action-conversation__state--failed');
    expect(screen.getByText('未実行')).toBeVisible();
    expect(screen.getAllByRole('article', { name: 'あなた' })).toHaveLength(2);
    expect(screen.getByRole('list', { name: '添付画像 1 件' })).toBeVisible();
    expect(screen.getByRole('img', { name: '添付画像 1 / 1' })).toBeVisible();
    expect(screen.queryByText('/private/capture.png')).toBeNull();
    const optimisticUser = screen.getByText('再送する内容').closest('article');
    expect(optimisticUser).not.toBeNull();
    const optimisticStatus = within(optimisticUser!).getByRole('status');
    expect(optimisticStatus).toHaveTextContent('送信中');

    const failed = {
      ...optimistic,
      submission: { ...optimistic.submission, state: 'failed' },
    } as const;
    rerender(
      <UiLanguageProvider initialLanguage="ja">
        <ActionConversationView
          view={viewWith([run, pending, failed])}
          toolOutputLoader={EMPTY_TOOL_OUTPUT_LOADER}
        />
      </UiLanguageProvider>
    );
    expect(within(screen.getByText('再送する内容').closest('article')!).getByRole('status')).toBe(
      optimisticStatus
    );
    expect(optimisticStatus).toHaveTextContent('送信失敗');
  });

  it('folds consecutive runs of one tool into a counted, expandable row in Japanese', async () => {
    const run: ActionConversationRunItem = {
      kind: 'run',
      runId: 'run-1',
      // 走り終わった run だけを畳む。キャンセルされた run は、飛んでいたツールを残したまま終わる。
      status: 'canceled',
      startedAt: '2026-08-30T00:00:00.000000Z',
      completedAt: '2026-08-30T00:00:30.000000Z',
      lines: [
        tool('t1', 'zanei_timeline', 'success', 1),
        // live イベントは ToolDefinition.name を載せてくる。同じツールとしてまとまること。
        tool('t2', 'Recent Computer Activity', 'success', 2),
        tool('t3', 'zanei_timeline', 'success', 3),
        tool('t4', 'zanei_timeline', 'success', 4),
        tool('t5', 'zanei_query', 'error', 5),
        tool('t6', 'draft_final_answer', 'success', 6),
        tool('t7', 'bash', 'processing', 7),
      ],
    };
    renderView(viewWith([run]), 'ja');

    // 生の tool id も英語名も日本語 UI には出さない。「完了」もどこにも出さない。
    expect(
      screen.queryByText(/zanei_timeline|draft_final_answer|Recent Computer Activity/)
    ).toBeNull();
    expect(screen.queryByText('完了')).toBeNull();
    // 失敗・結果未取得も含む全 7 件が親の開閉に従う。
    expect(screen.queryByText('失敗')).toBeNull();
    expect(screen.queryByText('結果未取得')).toBeNull();
    expect(screen.queryByText('回答を作成しました')).toBeNull();
    await userEvent.click(screen.getByRole('button', { name: /^Pantarayの作業 7, 実行 1:/ }));

    const group = screen.getByRole('button', { name: /^最近の操作を確認 4, 実行 1:/ });
    expect(group).toHaveAttribute('aria-expanded', 'false');
    // 回数は読み上げ名にだけ持ち、行には出さない。
    expect(group).toHaveTextContent(/^最近の操作を確認$/);
    // 失敗した行と、1件しかないツールは畳まない。
    expect(screen.getByText('操作の詳細を確認')).toBeVisible();
    expect(screen.getByText('失敗')).toHaveClass('action-conversation__state--failed');
    expect(screen.getByText('回答を作成しました')).toBeVisible();
    expect(screen.getByText('結果未取得')).toBeVisible();
    // 行そのものが開閉ボタンなので、「ツール出力」の行はどこにも無い。
    expect(screen.queryByRole('button', { name: /^ツール出力/ })).toBeNull();

    await userEvent.click(group);

    expect(group).toHaveAttribute('aria-expanded', 'true');
    // 畳んだ行は名前のまま、開いた 4 行は「何をしたか」を言う。
    expect(screen.getAllByText('最近の操作を確認')).toHaveLength(1);
    expect(
      screen.getAllByRole('button', {
        name: /^最近の操作を確認しました, ステップ [1-4], 実行 1:/,
      })
    ).toHaveLength(4);

    await userEvent.click(group);
    // 畳み直すと残るのは畳んだ行だけ。
    expect(screen.getAllByText('最近の操作を確認')).toHaveLength(1);
    expect(screen.queryByText('最近の操作を確認しました')).toBeNull();
  });

  it.each(['en', 'ja'] as const)(
    'stops tool and screen-reader activity on a lifecycle event in %s',
    async (language) => {
      const run: ActionConversationRunItem = {
        kind: 'run',
        runId: 'run-1',
        status: 'running',
        startedAt: '2026-08-30T00:00:00.000000Z',
        completedAt: null,
        lines: [tool('t1', 'read', 'processing', 1, [], 'src/app.py')],
      };
      const view = viewWith([run]);
      const { rerender } = renderView(view, language);
      expect(screen.getAllByText(language === 'ja' ? '実行中' : 'Running').length).toBeGreaterThan(
        0
      );
      rerender(
        <UiLanguageProvider initialLanguage={language}>
          <ActionConversationView
            view={view}
            lifecycle={{ processId: 'run-1', status: 'error' }}
            toolOutputLoader={EMPTY_TOOL_OUTPUT_LOADER}
          />
        </UiLanguageProvider>
      );
      expect(screen.queryByText(language === 'ja' ? '実行中' : 'Running')).toBeNull();
      expect(screen.queryByText('src/app.py')).toBeNull();
      await userEvent.click(
        screen.getByRole('button', {
          name: language === 'ja' ? /^Pantarayの作業 1,/ : /^Pantaray's work 1,/,
        })
      );
      expect(
        screen.getByText(language === 'ja' ? '結果未取得' : 'Result unavailable')
      ).toBeVisible();
      expect(screen.getByText('src/app.py')).toBeVisible();
      expect(
        screen.queryByText(language === 'ja' ? 'src/app.py を読みました' : 'Read src/app.py')
      ).toBeNull();
    }
  );

  it('leaves rows of a live run unfolded so an open row survives the next Tool', async () => {
    const live: ActionConversationRunItem = {
      kind: 'run',
      runId: 'run-1',
      status: 'running',
      startedAt: '2026-08-30T00:00:00.000000Z',
      completedAt: null,
      lines: [
        tool('t1', 'read', 'success', 1, [], 'src/app.py'),
        tool('t2', 'read', 'success', 2, [], 'src/main.py'),
      ],
    };
    const { rerender } = renderView(viewWith([live]));
    await userEvent.click(screen.getByRole('button', { name: /^Pantaray's work 2, Run 1:/ }));

    // 走っている間は、同じツールが続いても行のまま。開いて読んでいる行を作り直さない。
    const first = screen.getByRole('button', { name: /^Read src\/app.py, Step 1, Run 1:/ });
    await userEvent.click(first);
    expect(
      screen.getByRole('button', { name: /^Read src\/main.py, Step 2, Run 1:/ })
    ).toBeVisible();
    expect(screen.queryByRole('button', { name: /^Read a file 2, Run 1:/ })).toBeNull();

    rerender(
      <UiLanguageProvider initialLanguage="en">
        <ActionConversationView
          view={viewWith([{ ...live, status: 'success', completedAt: '2026-08-30T00:00:02.000Z' }])}
          toolOutputLoader={EMPTY_TOOL_OUTPUT_LOADER}
        />
      </UiLanguageProvider>
    );

    // 終われば作業ごと畳む。開き直すと、同じツールの続きは 1 行にまとまっている。
    const work = screen.getByRole('button', { name: /^Pantaray's work 2, Run 1:/ });
    expect(work).toHaveAttribute('aria-expanded', 'false');
    expect(screen.queryByRole('button', { name: /^Read src\/app.py, Step 1, Run 1:/ })).toBeNull();
    await userEvent.click(work);
    expect(screen.getByRole('button', { name: /^Read a file 2, Run 1:/ })).toHaveAttribute(
      'aria-expanded',
      'false'
    );
  });

  it('keeps denied and unavailable Tools distinct inside expanded work without failure styling', async () => {
    // 承認されなかった呼び出しも、記録がオフのまま呼ばれた読み取りも success として残る。
    // 畳んだ行はツール名しか出さないので、そこに紛れると「やった」ようにしか見えない。
    const run: ActionConversationRunItem = {
      kind: 'run',
      runId: 'run-1',
      status: 'success',
      startedAt: '2026-08-30T00:00:00.000000Z',
      completedAt: '2026-08-30T00:00:30.000000Z',
      lines: [
        tool('t1', 'apply_patch', 'success', 1, [], 'src/a.py'),
        tool('t2', 'apply_patch', 'success', 2, [], 'src/app.py', null, 'denied'),
        tool('t3', 'apply_patch', 'success', 3, [], 'src/b.py'),
        tool('t4', 'zanei_timeline', 'success', 4, [], null, null, 'unavailable'),
        tool('t5', 'zanei_timeline', 'success', 5),
      ],
    };
    renderView(viewWith([run], 'success'), 'ja');

    // 未実行の行も親の開閉に従い、展開後は個別の状態を残す。
    expect(screen.queryByText('src/app.py の実行は許可されませんでした')).toBeNull();
    expect(screen.queryByText('記録がオフのため最近の操作を確認できませんでした')).toBeNull();
    expect(screen.queryByText('src/a.py を編集しました')).toBeNull();
    await userEvent.click(screen.getByRole('button', { name: /^Pantarayの作業 5, 実行 1:/ }));
    expect(screen.getByText('src/app.py の実行は許可されませんでした')).toBeVisible();
    expect(screen.getByText('記録がオフのため最近の操作を確認できませんでした')).toBeVisible();
    // 断られた編集も、読めなかった読み取りも「しました」とは言わない。
    expect(screen.queryByText('src/app.py を編集しました')).toBeNull();
    expect(screen.getByText('最近の操作を確認しました')).toBeVisible();
    // 失敗ではないので赤くしない。ただし何も起きなかった印は残す。
    const markers = screen.getAllByText('未実行');
    expect(markers).toHaveLength(2);
    for (const marker of markers) {
      expect(marker).not.toHaveClass('action-conversation__state--failed');
    }
    // 畳まれるのは、断られた行を挟まずに続いた成功だけ。ここではどのツールも畳まれない。
    expect(screen.queryByRole('button', { name: /^ファイルを編集 \d/ })).toBeNull();
    expect(screen.queryByRole('button', { name: /^最近の操作を確認 \d/ })).toBeNull();
    expect(screen.getByText('src/a.py を編集しました')).toBeVisible();
    expect(screen.getByText('src/b.py を編集しました')).toBeVisible();
  });

  it('marks a Tool the user stopped before it started as not run, not as failed', async () => {
    // 停止が届いたとき発行前だった呼び出しは error として残る。状態語をそのまま出すと
    // 「実行して失敗した」に見え、行の文も「実行しました」と言ってしまう。
    const run: ActionConversationRunItem = {
      kind: 'run',
      runId: 'run-1',
      status: 'canceled',
      startedAt: '2026-08-30T00:00:00.000000Z',
      completedAt: '2026-08-30T00:00:30.000000Z',
      lines: [tool('t1', 'bash', 'error', 1, [], 'rm -rf build', null, 'not_executed')],
    };
    renderView(viewWith([run], 'canceled'), 'ja');
    await userEvent.click(screen.getByRole('button', { name: /^Pantarayの作業 1,/ }));

    expect(screen.getByText('rm -rf build は停止したため実行しませんでした')).toBeVisible();
    expect(screen.queryByText('失敗')).toBeNull();
    const marker = screen.getByText('未実行');
    expect(marker).not.toHaveClass('action-conversation__state--failed');
  });

  it('shows pages still being prepared without a status word or the failed colour', async () => {
    const run: ActionConversationRunItem = {
      kind: 'run',
      runId: 'run-1',
      status: 'success',
      startedAt: '2026-08-30T00:00:00.000000Z',
      completedAt: '2026-08-30T00:00:30.000000Z',
      lines: [tool('t1', 'render_pdf_page', 'success', 1, [], 'slides.pptx', null, 'preparing')],
    };
    renderView(viewWith([run], 'success'), 'ja');
    await userEvent.click(screen.getByRole('button', { name: /^Pantarayの作業 1,/ }));

    const line = toolLineOf(screen.getByText('slides.pptx を表示する準備をしています'));
    expect(line).toBeVisible();
    expect(line.querySelector('.action-conversation__state')).toBeNull();
    expect(line.querySelector('.action-conversation__state--failed')).toBeNull();
  });

  it('announces that a run started or waits for approval without showing it', () => {
    const run: ActionConversationRunItem = {
      kind: 'run',
      runId: 'run-1',
      status: 'running',
      startedAt: '2026-08-30T00:00:00.000000Z',
      completedAt: null,
      lines: [],
    };
    const { rerender } = renderView(viewWith([run]), 'ja');

    // ツール行が 1 つも出ていない段階でも、実行が始まったことは読み上げに届く。
    const announcement = screen.getByRole('status');
    expect(announcement).toHaveTextContent('実行中');
    expect(announcement).toHaveClass('action-conversation__sr-only');
    expect(announcement).toHaveAttribute('aria-live', 'polite');

    rerender(
      <UiLanguageProvider initialLanguage="ja">
        <ActionConversationView
          view={viewWith([{ ...run, status: 'approval_pending' }])}
          toolOutputLoader={EMPTY_TOOL_OUTPUT_LOADER}
        />
      </UiLanguageProvider>
    );
    expect(screen.getByRole('status')).toBe(announcement);
    expect(announcement).toHaveTextContent('確認待ち');

    rerender(
      <UiLanguageProvider initialLanguage="ja">
        <ActionConversationView
          view={viewWith([{ ...run, status: 'success', completedAt: '2026-08-30T00:00:02.000Z' }])}
          toolOutputLoader={EMPTY_TOOL_OUTPUT_LOADER}
        />
      </UiLanguageProvider>
    );
    // 終わった実行は状態語を持たない。返答そのものが結果なので「完了」とは言わない。
    expect(announcement).toBeEmptyDOMElement();
  });

  it('keeps processing and output-unavailable Tools noninteractive', async () => {
    const available = tool('unavailable', 'Finished without output');
    const unavailable = {
      ...available,
      entry: { ...available.entry, output_available: false },
    } satisfies ActionConversationToolItem;
    const run: ActionConversationRunItem = {
      kind: 'run',
      runId: 'run-1',
      status: 'running',
      startedAt: '2026-08-30T00:00:00.000000Z',
      completedAt: null,
      lines: [tool('processing', 'Still running', 'processing'), unavailable],
    };
    const load = vi.fn(EMPTY_TOOL_OUTPUT_LOADER.load);
    renderView(viewWith([run]), 'en', {
      ...EMPTY_TOOL_OUTPUT_LOADER,
      load,
    });

    await userEvent.click(screen.getByRole('button', { name: /^Pantaray's work 2,/ }));
    expect(screen.getByText('Still running')).toBeVisible();
    expect(screen.queryByRole('button', { name: /^Still running,/ })).toBeNull();
    expect(screen.queryByRole('button', { name: /^Finished without output,/ })).toBeNull();
    expect(load).not.toHaveBeenCalled();
  });

  it('labels the Tool waiting for approval instead of calling it running', async () => {
    const run: ActionConversationRunItem = {
      kind: 'run',
      runId: 'run-1',
      status: 'approval_pending',
      startedAt: '2026-08-30T00:00:00.000000Z',
      completedAt: null,
      // A Tool started in the same turn can finish after the one waiting for approval.
      lines: [tool('waiting', 'Delete screenshots', 'processing'), tool('listed', 'List files')],
    };
    renderView(viewWith([run]), 'ja');

    await userEvent.click(screen.getByRole('button', { name: /^Pantarayの作業 2,/ }));
    expect(screen.getByText('承認待ち')).toBeVisible();
    expect(screen.queryByText('実行中')).toBeNull();
  });

  it.each([
    ['error', 'Action failed', 'Public failure'],
    ['canceled', 'Action canceled', 'Canceled by user'],
  ] as const)(
    'keeps the %s outcome visible without announcing old history',
    (status, label, text) => {
      const run: ActionConversationRunItem = {
        kind: 'run',
        runId: 'run-1',
        status,
        startedAt: '2026-08-30T00:00:00.000000Z',
        completedAt: '2026-08-30T00:00:01.000000Z',
        lines: [
          {
            kind: 'terminal_outcome',
            runId: 'run-1',
            status,
            visibility: 'always',
            code: status,
            text,
          },
        ],
      };
      renderView(viewWith([run], status));

      const outcome = screen.getByRole('region', { name: new RegExp(`^${label},`) });
      expect(outcome).toHaveTextContent(text);
      expect(outcome).not.toHaveAttribute('aria-live');
    }
  );

  const withImages = (
    key: string,
    images: CanonicalUserItem['entry']['images']
  ): CanonicalUserItem => {
    const item = canonicalUser(key, 'Look at these');
    return { ...item, entry: { ...item.entry, images } };
  };

  const STORED_IMAGES: CanonicalUserItem['entry']['images'] = [
    { kind: 'image', storage_path: 'user-1/2026-09-08/11111111-1111-4111-8111-111111111111.png' },
    { kind: 'image', storage_path: 'user-1/2026-09-08/22222222-2222-4222-8222-222222222222.png' },
  ];

  it('keeps approval attachments usable when no comment was added', async () => {
    const user = withImages('approval', STORED_IMAGES.slice(0, 1));
    user.entry.content = null;
    user.entry.approved_suggestion = { suggestion_id: 'suggestion-1', content: 'Review changes' };
    renderView(viewWith([user], 'processing', 'suggestion-1'));
    const bubble = screen.getByRole('article', { name: 'You' });
    expect(bubble).not.toHaveTextContent('Review changes');
    const thumbnail = within(bubble).getByRole('button', { name: 'Open attached image 1 of 1' });
    thumbnail.focus();
    await userEvent.keyboard('{Enter}');
    expect(screen.getByRole('dialog', { name: 'Attached image' })).toBeVisible();
    await userEvent.keyboard('{Escape}');
    expect(thumbnail).toHaveFocus();
  });

  it('shows the documents a message carried, sent or still sending, without an open control', () => {
    const sent = canonicalUser('sent', '');
    sent.entry.content = null;
    sent.entry.files = [
      { name: '見積書.pdf', byte_size: 1_258_291 },
      { name: '見積書.pdf', byte_size: 512 },
    ];
    const sending: ActionConversationUserItem = {
      kind: 'user',
      source: 'optimistic',
      key: 'sending',
      visibility: 'always',
      // prettier-ignore
      submission: { state: 'submitting', request: { target: { kind: 'existing', action_id: 'action-1', expected_process_id: null }, message: { version: 1, message_id: 'sending', content: '要約して', images: [], files: [{ attachment_id: '22222222-2222-4222-8222-222222222222', name: 'analysis.ipynb', byte_size: 2048 }] } } },
    };
    renderView(viewWith([sent, sending]), 'ja');

    const [sentBubble, sendingBubble] = screen.getAllByRole('article', { name: 'あなた' });
    const sentFiles = within(sentBubble).getByRole('list', { name: '添付ファイル 2 件' });
    expect(
      within(sentFiles)
        .getAllByRole('listitem')
        .map((item) => item.textContent)
    ).toEqual(['見積書.pdf1.2 MB', '見積書.pdf512 B']);
    expect(
      within(sendingBubble).getByRole('list', { name: '添付ファイル 1 件' })
    ).toHaveTextContent('analysis.ipynb2 KB');
    expect(within(sentBubble).queryByRole('button')).toBeNull();
  });

  it('renders attached images as counted thumbnails served over the image scheme', () => {
    renderView(viewWith([withImages('images', STORED_IMAGES)]));

    expect(screen.getByRole('list', { name: '2 attached images' })).toBeVisible();
    const thumbnails = screen.getAllByRole('img');
    expect(thumbnails.map((image) => image.getAttribute('alt'))).toEqual([
      'Attached image 1 of 2',
      'Attached image 2 of 2',
    ]);
    expect(thumbnails.map((image) => image.getAttribute('loading'))).toEqual(['lazy', 'lazy']);
    expect(thumbnails[0]).toHaveAttribute(
      'src',
      'pantaray-image://local/user-1/2026-09-08/11111111-1111-4111-8111-111111111111.png'
    );
    // A data: URL would put megabytes of base64 in React state and in the DOM.
    expect(document.querySelectorAll('img[src^="data:"]')).toHaveLength(0);
    expect(screen.getByRole('button', { name: 'Open attached image 1 of 2' })).toBeVisible();
  });

  it('opens a thumbnail in a modal lightbox and returns focus when it closes', async () => {
    const revealImage = vi.fn(async () => ({ revealed: true }));
    Object.defineProperty(window, 'electron', {
      configurable: true,
      value: { actions: { revealImage } },
    });
    renderView(viewWith([withImages('images', STORED_IMAGES)]));

    const thumbnail = screen.getByRole('button', { name: 'Open attached image 2 of 2' });
    thumbnail.focus();
    await userEvent.keyboard('{Enter}');

    const dialog = screen.getByRole('dialog', { name: 'Attached image' });
    expect(dialog).toHaveAttribute('aria-modal', 'true');
    expect(within(dialog).getByRole('img', { name: 'Attached image 2 of 2' })).toHaveAttribute(
      'src',
      'pantaray-image://local/user-1/2026-09-08/22222222-2222-4222-8222-222222222222.png'
    );
    expect(within(dialog).getByRole('button', { name: 'Close' })).toHaveFocus();

    await userEvent.click(within(dialog).getByRole('button', { name: 'Show in Finder' }));
    expect(revealImage).toHaveBeenCalledWith({
      storagePath: 'user-1/2026-09-08/22222222-2222-4222-8222-222222222222.png',
    });

    await userEvent.keyboard('{Escape}');

    expect(screen.queryByRole('dialog')).toBeNull();
    expect(thumbnail).toHaveFocus();
  });

  it('says what each tool step did, and falls back to the head of its result', async () => {
    const run: ActionConversationRunItem = {
      kind: 'run',
      runId: 'run-1',
      status: 'running',
      startedAt: '2026-08-30T00:00:00.000000Z',
      completedAt: null,
      lines: [
        tool('read', 'read', 'success', 1, [], 'tests/test_retrieval.py'),
        tool('search', 'web_search', 'success', 2, [], '日本語の情報検索'),
        // 引数を持たないツールに添えるのは散文の結果だけ。構造そのものは投影が落としてくる。
        tool('draft', 'draft_final_answer', 'success', 3, [], null, 'Drafted 3 sections'),
        tool('bash', 'bash', 'processing', 4, [], "PYTHONPATH=src python - <<'PY'…"),
      ],
    };
    renderView(viewWith([run]), 'ja');
    await userEvent.click(screen.getByRole('button', { name: /^Pantarayの作業 4, 実行 1:/ }));
    const command = screen.getByText("PYTHONPATH=src python - <<'PY'…");
    expect(within(toolLineOf(command)).getByText('実行中')).toBeVisible();

    expect(screen.getByText('tests/test_retrieval.py を読み取りました')).toBeVisible();
    expect(screen.getByText('ウェブを検索しました 日本語の情報検索')).toBeVisible();
    // 引数を持たない行だけが結果の頭を添える。
    expect(screen.getByText('回答を作成しました')).toBeVisible();
    expect(screen.getByText('Drafted 3 sections')).toHaveClass('action-conversation__tool-preview');
    // 実行中の行は進行形で、コマンドはそのまま等幅で出る。
    expect(command).toHaveClass('action-conversation__tool-text--mono');
  });

  it('keeps a captured screenshot inside the expanded tool step, never on the collapsed line', async () => {
    const run: ActionConversationRunItem = {
      kind: 'run',
      runId: 'run-1',
      status: 'success',
      startedAt: '2026-08-30T00:00:00.000000Z',
      completedAt: '2026-08-30T00:00:01.000000Z',
      lines: [tool('capture', 'capture_screen', 'success', 1, STORED_IMAGES, 'Google Chrome')],
    };
    renderView(viewWith([run], 'success'));

    expect(screen.queryByRole('img')).toBeNull();
    await userEvent.click(screen.getByRole('button', { name: /^Pantaray's work 1, Run 1:/ }));
    await userEvent.click(screen.getByRole('button', { name: /^Captured Google Chrome, Step 1,/ }));

    expect(screen.getByRole('list', { name: '2 screenshots' })).toBeVisible();
    const thumbnails = screen.getAllByRole('img');
    expect(thumbnails.map((image) => image.getAttribute('alt'))).toEqual([
      'Screenshot 1 of 2',
      'Screenshot 2 of 2',
    ]);
    expect(thumbnails.map((image) => image.getAttribute('loading'))).toEqual(['lazy', 'lazy']);
    expect(thumbnails[0]).toHaveAttribute(
      'src',
      'pantaray-image://local/user-1/2026-09-08/11111111-1111-4111-8111-111111111111.png'
    );
    expect(document.querySelectorAll('img[src^="data:"]')).toHaveLength(0);
    expect(screen.getByRole('button', { name: 'Open screenshot 1 of 2' })).toBeVisible();
  });

  it('states that an image is unavailable instead of leaving a broken thumbnail', () => {
    renderView(viewWith([withImages('images', STORED_IMAGES.slice(0, 1))]));

    fireEvent.error(screen.getByRole('img'));

    expect(screen.getByText('Image unavailable')).toBeVisible();
    expect(screen.queryByRole('img')).toBeNull();
    expect(screen.queryByRole('button', { name: /^Open attached image/ })).toBeNull();
  });

  describe('per-answer copy', () => {
    const answered = (runId: string, text: string): ActionConversationRunItem => ({
      kind: 'run',
      runId,
      status: 'success',
      startedAt: '2026-08-30T00:00:00.000000Z',
      completedAt: '2026-08-30T00:01:00.000000Z',
      lines: [
        canonicalUser(`ask-${runId}`, 'Summarize'),
        { kind: 'assistant', key: `note-${runId}`, visibility: 'always', text: 'Reading first' },
        { kind: 'final_output', runId, status: 'success', visibility: 'always', text },
      ],
    });
    const failed: ActionConversationRunItem = {
      kind: 'run',
      runId: 'run-failed',
      status: 'error',
      startedAt: '2026-08-30T00:02:00.000000Z',
      completedAt: '2026-08-30T00:03:00.000000Z',
      lines: [
        canonicalUser('ask-failed', 'Retry'),
        { kind: 'assistant', key: 'note-failed', visibility: 'always', text: 'Trying again' },
        {
          kind: 'terminal_outcome',
          runId: 'run-failed',
          status: 'error',
          visibility: 'always',
          code: 'ACTION_FAILED',
          text: 'It failed.',
        },
      ],
    };
    const writeText = vi.fn<(text: string) => Promise<void>>();
    beforeEach(() => {
      writeText.mockReset().mockResolvedValue(undefined);
      Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } });
    });

    it('copies the Markdown source of that answer only, and only final answers offer it', async () => {
      renderView(
        viewWith(
          [answered('run-1', 'First'), answered('run-2', '- **Done**\n- Moved 3 files'), failed],
          'error'
        )
      );
      const buttons = screen.getAllByRole('button', { name: 'Copy this answer' });
      expect(buttons).toHaveLength(2);

      await userEvent.click(
        within(screen.getByRole('region', { name: /^Final answer, Run 2:/ })).getByRole('button', {
          name: 'Copy this answer',
        })
      );

      expect(writeText).toHaveBeenCalledTimes(1);
      expect(writeText).toHaveBeenCalledWith('- **Done**\n- Moved 3 files');
    });

    it('reports a clipboard failure instead of looking copied', async () => {
      writeText.mockRejectedValue(new DOMException('denied', 'NotAllowedError'));
      renderView(viewWith([answered('run-1', 'First')], 'success'));

      await userEvent.click(screen.getByRole('button', { name: 'Copy this answer' }));

      expect(await screen.findByText('Couldn’t copy this answer. Try again.')).toHaveAttribute(
        'role',
        'alert'
      );
    });
  });
});
