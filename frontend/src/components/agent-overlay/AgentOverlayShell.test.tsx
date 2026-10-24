import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useEffect, useState } from 'react';
import { describe, expect, it, vi } from 'vitest';

import type { ActionConversationUserItem } from '../../../electron/src/actions/actionConversationModel';
import { UserItem } from '@/components/action-conversation/UserMessage';
import AgentOverlayShell from './AgentOverlayShell';
import { UiLanguageProvider } from '@/context/UiLanguageContext';
import { expectIconButtonTooltips, expectTooltipIsName } from '@/tests/iconTooltip';

describe('AgentOverlayShell', () => {
  it('removes collapsed conversation controls from tab order without hiding the preview', async () => {
    const onConversationAction = vi.fn();
    const onConversationLink = vi.fn();
    const onCopyAnswer = vi.fn();
    let revealLateControl = () => {};
    let enableStatefulControl = () => {};
    const LateControl = () => {
      const [isVisible, setIsVisible] = useState(false);
      const [isDisabled, setIsDisabled] = useState(true);
      useEffect(() => {
        revealLateControl = () => setIsVisible(true);
        enableStatefulControl = () => setIsDisabled(false);
      }, []);
      return (
        <>
          <button type="button" disabled={isDisabled}>
            Stateful conversation action
          </button>
          {isVisible ? <button type="button">Late conversation action</button> : null}
        </>
      );
    };
    const renderShell = (
      isExpanded: boolean,
      conversationActionDisabled = false,
      conversationLinkHref = 'https://example.com'
    ) => (
      <UiLanguageProvider initialLanguage="en">
        <AgentOverlayShell
          isVisible={true}
          isContentVisible={true}
          isExpanded={isExpanded}
          content={null}
          suggestionText=""
          isSuggestionStreamFinished={true}
          actionText=""
          isActionStreamFinished={false}
          approvalUiState="hidden"
          showBusyIndicator={false}
          conversationContent={
            <>
              <button
                type="button"
                disabled={conversationActionDisabled}
                onClick={onConversationAction}
              >
                Conversation action
              </button>
              <a href={conversationLinkHref} onClick={onConversationLink}>
                Conversation link
              </a>
              <textarea aria-label="Tool output" readOnly value="Visible output" />
              <button type="button" data-collapsed-clickable onClick={onCopyAnswer}>
                Copy answer
              </button>
              <LateControl />
            </>
          }
          onToggleExpand={() => {}}
        />
      </UiLanguageProvider>
    );
    const { rerender } = render(renderShell(true));
    screen.getByRole('button', { name: 'Conversation action' }).focus();

    rerender(renderShell(false));

    const conversationAction = screen.getByRole('button', { name: 'Conversation action' });
    expect(conversationAction).toHaveAttribute('tabindex', '-1');
    expect(conversationAction).toBeDisabled();
    expect(conversationAction).toHaveAttribute('aria-disabled', 'true');
    expect(conversationAction).not.toHaveFocus();
    fireEvent.click(conversationAction);
    expect(onConversationAction).not.toHaveBeenCalled();
    expect(screen.queryByRole('link', { name: 'Conversation link' })).toBeNull();
    fireEvent.click(screen.getByText('Conversation link'));
    expect(onConversationLink).not.toHaveBeenCalled();
    const toolOutput = screen.getByRole('textbox', { name: 'Tool output' });
    expect(toolOutput).toBeEnabled();
    expect(toolOutput).toHaveAttribute('tabindex', '-1');
    expect(toolOutput).not.toHaveAttribute('aria-disabled');
    // The answer's copy button stays out of the tab order but still copies on click.
    const copyAnswer = screen.getByRole('button', { name: 'Copy answer' });
    expect(copyAnswer).toHaveAttribute('tabindex', '-1');
    expect(copyAnswer).toBeEnabled();
    expect(copyAnswer).not.toHaveAttribute('aria-disabled');
    fireEvent.click(copyAnswer);
    expect(onCopyAnswer).toHaveBeenCalledTimes(1);
    expect(screen.getByRole('button', { name: 'Expand' })).toHaveFocus();
    toolOutput.focus();
    expect(toolOutput).toHaveFocus();
    expect(conversationAction.closest('[data-overlay-scroll="true"]')).not.toHaveAttribute('inert');
    await act(async () => enableStatefulControl());
    expect(screen.getByRole('button', { name: 'Stateful conversation action' })).toBeDisabled();
    await act(async () => revealLateControl());
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Late conversation action' })).toHaveAttribute(
        'tabindex',
        '-1'
      )
    );
    expect(screen.getByRole('button', { name: 'Late conversation action' })).toBeDisabled();

    rerender(renderShell(true, true, 'https://example.org/current'));

    const restoredAction = screen.getByRole('button', { name: 'Conversation action' });
    expect(restoredAction).not.toHaveAttribute('tabindex');
    expect(restoredAction).toBeDisabled();
    expect(toolOutput).not.toHaveAttribute('tabindex');
    expect(screen.getByRole('button', { name: 'Stateful conversation action' })).toBeEnabled();
    const restoredLink = screen.getByRole('link', { name: 'Conversation link' });
    expect(restoredLink).toHaveAttribute('href', 'https://example.org/current');
    fireEvent.click(restoredLink);
    expect(onConversationLink).toHaveBeenCalledTimes(1);
  });

  it('announces progress while the run has not started and no stop is offered yet', () => {
    // このファイルは自動 cleanup を持たず、前のテストの DOM が残る。クエリはこの
    // render の container に限定する。
    const { container } = render(
      <UiLanguageProvider initialLanguage="en">
        <AgentOverlayShell
          isVisible={true}
          isContentVisible={true}
          isExpanded={true}
          content={null}
          suggestionText=""
          isSuggestionStreamFinished={true}
          actionText=""
          isActionStreamFinished={false}
          approvalUiState="hidden"
          showBusyIndicator={true}
        />
      </UiLanguageProvider>
    );

    const shell = within(container);
    // 採用直後は run がまだ無く停止も出せない。ここで何も出さないとパネルは止まって見える。
    expect(shell.getByRole('status')).toHaveTextContent('Running');
    expect(shell.queryByRole('button', { name: 'Stop' })).toBeNull();
  });

  it('names each header button in its tooltip, and opens the main window with the pair of Open small', () => {
    // このファイルは自動 cleanup を持たない。クエリはこの render の container に限定する。
    const { container } = render(
      <UiLanguageProvider initialLanguage="ja">
        <AgentOverlayShell
          isVisible={true}
          isContentVisible={true}
          isExpanded={false}
          content={null}
          suggestionText=""
          isSuggestionStreamFinished={true}
          actionText=""
          isActionStreamFinished={false}
          approvalUiState="hidden"
          showBusyIndicator={false}
          actionId="action-1"
          onOpenInMainWindow={() => {}}
          conversationCopy={{ status: 'idle', copy: () => {} }}
          onToggleExpand={() => {}}
          onClose={() => {}}
        />
      </UiLanguageProvider>
    );
    const header = within(container);
    // The link, the main window, the copy; then the panel's own expand and hide.
    const buttons = expectIconButtonTooltips(container);
    expect(buttons.map((button) => button.getAttribute('aria-label'))).toEqual([
      'リンクをコピー',
      'メイン画面で開く',
      '会話をコピー',
      '展開する',
      '非表示',
    ]);
    buttons.forEach(expectTooltipIsName);
    const openInMain = header.getByRole('button', { name: 'メイン画面で開く' });
    expect(openInMain.querySelector('svg.lucide-maximize-2')).not.toBeNull();
  });

  it('copies the link of the suggestion it shows, then of the Action that took its place', async () => {
    const writeText = vi.fn(async () => undefined);
    window.electron = { clipboard: { writeText } } as unknown as Window['electron'];
    const renderShell = (actionId: string | null, suggestionId: string | null) => (
      <UiLanguageProvider initialLanguage="ja">
        <AgentOverlayShell
          isVisible={true}
          isContentVisible={true}
          isExpanded={true}
          content={null}
          suggestionText=""
          isSuggestionStreamFinished={true}
          actionText=""
          isActionStreamFinished={false}
          approvalUiState="hidden"
          showBusyIndicator={false}
          actionId={actionId}
          suggestionId={suggestionId}
        />
      </UiLanguageProvider>
    );
    const { container, rerender } = render(renderShell(null, null));
    const shell = within(container);
    expect(shell.queryByRole('button', { name: 'リンクをコピー' })).toBeNull();

    rerender(renderShell(null, 'sug-1'));
    await userEvent.click(shell.getByRole('button', { name: 'リンクをコピー' }));
    rerender(renderShell('action-1', 'sug-1'));
    await userEvent.click(shell.getByRole('button', { name: 'リンクをコピー' }));
    expect(writeText.mock.calls).toEqual([
      ['pantaray://suggestion/sug-1'],
      ['pantaray://task/action-1'],
    ]);
    delete (window as { electron?: unknown }).electron;
  });

  it('keeps the composer out of the scrolling conversation and never puts Stop in the header', () => {
    // このファイルは自動 cleanup を持たず、前のテストの DOM が残る。クエリはこの
    // render の container に限定する。
    const { container } = render(
      <UiLanguageProvider initialLanguage="en">
        <AgentOverlayShell
          isVisible={true}
          isContentVisible={true}
          isExpanded={true}
          content={null}
          suggestionText=""
          isSuggestionStreamFinished={true}
          actionText=""
          isActionStreamFinished={false}
          approvalUiState="hidden"
          showBusyIndicator={true}
          conversationContent={<p>long conversation</p>}
          composer={<textarea aria-label="Message" />}
        />
      </UiLanguageProvider>
    );

    const shell = within(container);
    expect(
      shell.getByText('long conversation').closest('[data-overlay-scroll="true"]')
    ).not.toBeNull();
    expect(shell.getByLabelText('Message').closest('[data-overlay-scroll="true"]')).toBeNull();

    // 停止は入力欄の主ボタンが担う。ヘッダは実行中であることだけを示す。
    expect(shell.queryByRole('button', { name: 'Stop' })).toBeNull();
    expect(shell.getByRole('status')).toHaveTextContent('Running');
  });

  it('shows a pending approval above the composer, below the conversation', () => {
    // 実行は最新の行で止まっているので、承認は会話の先頭ではなくそこに出す。
    const { container } = render(
      <UiLanguageProvider initialLanguage="en">
        <AgentOverlayShell
          isVisible={true}
          isContentVisible={true}
          isExpanded={true}
          content={null}
          suggestionText=""
          isSuggestionStreamFinished={true}
          actionText=""
          isActionStreamFinished={false}
          approvalUiState="approval_pending"
          approvalBlockers={[
            {
              actionId: 'action-1',
              processId: 'run-1',
              approvalSessionId: 'approval-1',
              toolRequestId: 'tool-request-1',
              toolId: 'bash',
              intentClass: 'process_exec_local',
              commandSummary: { summary_kind: 'bash', command: 'rm -- a.png', cwd: '/tmp' },
            },
          ]}
          showBusyIndicator={false}
          conversationContent={<p>long conversation</p>}
          composer={<textarea aria-label="Message" />}
        />
      </UiLanguageProvider>
    );

    const shell = within(container);
    const approval = shell.getByText('Approval required');
    expect(approval.closest('[data-overlay-scroll="true"]')).toBeNull();
    expect(
      shell.getByText('long conversation').compareDocumentPosition(approval) &
        Node.DOCUMENT_POSITION_FOLLOWING
    ).toBeTruthy();
    expect(
      approval.compareDocumentPosition(shell.getByLabelText('Message')) &
        Node.DOCUMENT_POSITION_FOLLOWING
    ).toBeTruthy();
  });

  it('insets the composer exactly like the conversation, collapsed or expanded', () => {
    // jsdom はレイアウトを持たないので、幅ではなく「横方向の内寄せを作る宣言」を突き合わせる。
    // パネルの padding だけが横位置を決める、という不変条件をここで固定する。
    const horizontalInset = (element: Element) => {
      const style = getComputedStyle(element);
      return [
        style.paddingLeft,
        style.paddingRight,
        style.marginLeft,
        style.marginRight,
        style.borderLeftWidth,
        style.borderRightWidth,
      ].map((value) => value || '0px');
    };

    for (const isExpanded of [true, false]) {
      const { container, unmount } = render(
        <UiLanguageProvider initialLanguage="en">
          <AgentOverlayShell
            isVisible={true}
            isContentVisible={true}
            isExpanded={isExpanded}
            content={null}
            suggestionText=""
            isSuggestionStreamFinished={true}
            actionText=""
            isActionStreamFinished={false}
            approvalUiState="hidden"
            showBusyIndicator={false}
            conversationContent={<p>conversation</p>}
            composer={<textarea aria-label="Message" />}
          />
        </UiLanguageProvider>
      );
      const shell = within(container);
      const conversation = shell.getByText('conversation').closest('[data-overlay-scroll="true"]');
      const dock = shell.getByLabelText('Message').parentElement;

      // 取り違えを防ぐため、styled-components の宣言が実際に解決できていることを先に確かめる。
      expect(conversation).not.toBeNull();
      expect(getComputedStyle(conversation as Element).overflowX).toBe('hidden');
      expect(horizontalInset(dock as Element)).toEqual(horizontalInset(conversation as Element));
      unmount();
    }
  });

  it('fires close on mouse down and does not double fire on click', () => {
    const onClose = vi.fn();
    render(
      <UiLanguageProvider initialLanguage="en">
        <AgentOverlayShell
          isVisible={true}
          isContentVisible={true}
          isExpanded={true}
          content={null}
          suggestionText=""
          isSuggestionStreamFinished={true}
          actionText=""
          isActionStreamFinished={true}
          approvalUiState="hidden"
          showBusyIndicator={false}
          onClose={onClose}
        />
      </UiLanguageProvider>
    );

    const button = screen.getByRole('button', { name: 'Hide Notification' });
    fireEvent.mouseDown(button);
    fireEvent.click(button);

    expect(onClose).toHaveBeenCalledTimes(1);
  });
  it("opens a sent message's task in the main window, as Open in main window does", () => {
    const onOpenInMainWindow = vi.fn();
    const sent: ActionConversationUserItem = {
      kind: 'user',
      source: 'canonical',
      key: 'sent',
      visibility: 'always',
      entry: {
        step_kind: 'user',
        chat_note: null,
        approved_suggestion: null,
        step_id: 'step-sent',
        step_number: 1,
        message_id: 'message-sent',
        accepted_sequence: 1,
        content: 'Check the quote',
        images: [],
        project_refs: [],
        work_refs: [{ kind: 'action', id: 'action-2', title: 'Fix the quote', start: 10, end: 15 }],
        status: 'adopted',
      },
    };
    // このファイルは自動 cleanup を持たない。クエリはこの render の container に限定する。
    const { container } = render(
      <UiLanguageProvider initialLanguage="en">
        <AgentOverlayShell
          isVisible={true}
          isContentVisible={true}
          isExpanded={true}
          content={null}
          suggestionText=""
          isSuggestionStreamFinished={true}
          actionText=""
          isActionStreamFinished={false}
          approvalUiState="hidden"
          showBusyIndicator={false}
          actionId="action-1"
          onOpenInMainWindow={onOpenInMainWindow}
          conversationContent={<UserItem item={sent} />}
          onToggleExpand={() => {}}
        />
      </UiLanguageProvider>
    );

    fireEvent.click(within(container).getByRole('link', { name: 'Open Fix the quote' }));

    expect(onOpenInMainWindow).toHaveBeenCalledExactlyOnceWith({ kind: 'action', id: 'action-2' });
  });
  it("shows a suggestion's files as chips that open its preview or its app", async () => {
    const onOpenInMainWindow = vi.fn();
    const openInApp = vi.fn(async () => ({ kind: 'opened' }) as const);
    Object.defineProperty(window, 'electron', {
      configurable: true,
      value: { actionFiles: { openInApp } },
    });
    const renderNotice = (artifacts: string[]) => (
      <UiLanguageProvider initialLanguage="en">
        <AgentOverlayShell
          isVisible={true}
          isContentVisible={true}
          isExpanded={true}
          content={null}
          suggestionText="I drafted a reply."
          isSuggestionStreamFinished={true}
          artifacts={artifacts}
          actionText=""
          isActionStreamFinished={false}
          approvalUiState="hidden"
          showBusyIndicator={false}
          suggestionId="sug-1"
          onOpenInMainWindow={onOpenInMainWindow}
        />
      </UiLanguageProvider>
    );
    const { rerender } = render(renderNotice([]));
    expect(screen.queryByRole('group', { name: "This suggestion's files" })).toBeNull();

    rerender(renderNotice(['drafts/reply.md', 'budget.xlsx']));
    const chips = within(screen.getByRole('group', { name: "This suggestion's files" }));
    // Plain buttons: the preview opens in the main window, not beside these chips.
    expect(chips.getByRole('button', { name: /^MD\s*reply\.md$/ })).not.toHaveAttribute(
      'aria-pressed'
    );
    fireEvent.click(chips.getByRole('button', { name: /^XLSX\s*budget\.xlsx$/ }));
    expect(onOpenInMainWindow).toHaveBeenCalledWith({
      kind: 'suggestion',
      id: 'sug-1',
      file: 'budget.xlsx',
    });
    fireEvent.click(chips.getByRole('button', { name: 'Open in default app: reply.md' }));
    await waitFor(() =>
      expect(openInApp).toHaveBeenCalledWith({
        target: { kind: 'suggestion', id: 'sug-1' },
        path: 'drafts/reply.md',
      })
    );
    expect(screen.queryByRole('alert')).toBeNull();
  });
});
