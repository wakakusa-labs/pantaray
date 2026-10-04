import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { useEffect, useState } from 'react';
import { describe, expect, it, vi } from 'vitest';

import AgentOverlayShell from './AgentOverlayShell';
import { UiLanguageProvider } from '@/context/UiLanguageContext';

describe('AgentOverlayShell', () => {
  it('does not show suggestion status text even while stop action is visible', () => {
    render(
      <UiLanguageProvider initialLanguage="en">
        <AgentOverlayShell
          isVisible={true}
          isContentVisible={true}
          isExpanded={true}
          content={null}
          suggestionText="提案です"
          isSuggestionStreamFinished={true}
          actionText=""
          isActionStreamFinished={false}
          approvalUiState="hidden"
          showBusyIndicator={true}
          showFooterActions={true}
        />
      </UiLanguageProvider>
    );

    expect(screen.queryByText('承認')).toBeNull();
  });

  it('removes collapsed conversation controls from tab order without hiding the preview', async () => {
    const onConversationAction = vi.fn();
    const onConversationLink = vi.fn();
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
          showFooterActions={false}
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
          showFooterActions={false}
        />
      </UiLanguageProvider>
    );

    const shell = within(container);
    // 採用直後は run がまだ無く停止も出せない。ここで何も出さないとパネルは止まって見える。
    expect(shell.getByRole('status')).toHaveTextContent('Running');
    expect(shell.queryByRole('button', { name: 'Stop' })).toBeNull();
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
          showFooterActions={false}
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
          showFooterActions={false}
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
            showFooterActions={false}
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
          showFooterActions={false}
          onClose={onClose}
        />
      </UiLanguageProvider>
    );

    const button = screen.getByRole('button', { name: 'Hide Notification' });
    fireEvent.mouseDown(button);
    fireEvent.click(button);

    expect(onClose).toHaveBeenCalledTimes(1);
  });
});
