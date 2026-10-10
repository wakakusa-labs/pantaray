import { useEffect, useRef } from 'react';
import type { PointerEvent, ReactNode, RefObject } from 'react';
import overlayBackground from '@/assets/images/agentoverlay_back.png';
import { useI18n } from '@/context/useI18n';
import { PopupContainer } from './OverlayBackground';
import HeaderRow, { HeaderButtonGroup } from './HeaderBar';
import { ApprovalPanel as ApprovalPanelView } from './ApprovalPanel';
import type { ApprovalDecision } from './approvalDisplayModel';
import {
  ContentFade,
  ScrollableContent,
  ContentInner,
  ContentWrapper,
  NotificationContent,
  AnswerArea,
  ComposerDock,
} from './ContentLayout';
import { MarkdownBlock } from './MarkdownRenderer';
import { HeaderIconButton } from './IconButton';
import {
  Check,
  ChevronDown,
  ChevronUp,
  CircleAlert,
  Clipboard,
  MessageCircle,
  Minus,
} from 'lucide-react';
import styled, { keyframes } from 'styled-components';
import type { ActionApprovalBlocker } from '../../../electron/src/actions/actionLiveCore';
import { useCollapsedFocusBoundary } from './useCollapsedFocusBoundary';
import type { ClipboardCopyStatus } from './useClipboardCopy';

const SuggestionAcceptedStatus = styled.span`
  display: flex;
  width: fit-content;
  align-items: center;
  gap: var(--space-xs);
  margin-top: var(--space-xs);
  margin-left: auto;
  color: var(--text-secondary);
  font-size: var(--text-meta-size);
`;

const SCROLL_INDICATOR_HIDE_DELAY_MS = 700;

/** The chat button holds the header's left end; the other buttons stay right. */
const ShowChatButton = styled(HeaderIconButton)`
  margin-right: auto;
`;

const VisuallyHidden = styled.span`
  position: absolute;
  width: 1px;
  height: 1px;
  overflow: hidden;
  clip-path: inset(50%);
  white-space: nowrap;
`;

/**
 * Pending approvals sit above the composer, outside the scrolling conversation: the
 * run is stopped at the newest line, so that is where the user is looking. A long
 * command scrolls inside the dock instead of pushing the conversation away.
 */
const ApprovalDock = styled.div`
  display: grid;
  gap: var(--space-sm);
  max-height: 45vh;
  margin-bottom: var(--space-sm);
  overflow-y: auto;
`;

const thinkingPulse = keyframes`
  0%, 100% { opacity: 0.55; }
  50% { opacity: 1; }
`;

/**
 * 依頼の直後、その実行の最初のツールが出るまでだけ会話の末尾に出す。履歴には残さない。
 * ツールは一瞬で終わり、以降は作業の件数が進みを示すので、ツールの合間には出さない。
 * 実行中であることはヘッダーと会話がすでに読み上げるので、画面にだけ出す。
 */
const ThinkingLine = styled.p`
  margin: var(--space-md) 0 0;
  color: var(--text-secondary);
  animation: ${thinkingPulse} 1.6s ease-in-out infinite;

  &:first-child {
    margin-top: 0;
  }

  @media (prefers-reduced-motion: reduce) {
    animation: none;
  }
`;

type AgentOverlayShellProps = {
  isVisible: boolean;
  isContentVisible: boolean;
  isExpanded: boolean;
  content: string | null;
  suggestionText: string;
  isSuggestionStreamFinished: boolean;
  isSuggestionAccepted?: boolean;
  actionText: string;
  conversationContent?: ReactNode;
  /** パネル下端に固定する入力欄。会話履歴と一緒にはスクロールしない。 */
  composer?: ReactNode;
  isActionStreamFinished: boolean;
  approvalUiState: 'hidden' | 'approval_pending';
  approvalBlockers?: readonly ActionApprovalBlocker[];
  isSubmittingApproval?: boolean;
  approvalErrorMessage?: string | null;
  /**
   * Only announces that the Action is running; on screen the conversation's shimmer and
   * the thinking line show it.
   */
  showBusyIndicator: boolean;
  showThinking?: boolean;
  /** The Action the main window's chat can show; null until one exists. */
  /** The task this panel started could not be shown in the main window. */
  openTaskFailed?: boolean;
  chatActionId?: string | null;
  onShowChat?: (request: { actionId: string }) => Promise<void>;
  fadeDurationMs?: number;
  onToggleExpand?: () => void;
  onClose?: () => void;
  onDecideApproval?: (decision: ApprovalDecision, blocker: ActionApprovalBlocker) => void;
  onOpenWorkspaceSettings?: () => void;
  /** Present while a conversation is shown; copies the whole conversation. */
  conversationCopy?: Readonly<{ status: ClipboardCopyStatus; copy: () => void }>;
  onHeaderPointerDown?: (event: PointerEvent<HTMLDivElement>) => void;
  onHeaderPointerMove?: (event: PointerEvent<HTMLDivElement>) => void;
  onHeaderPointerUp?: (event: PointerEvent<HTMLDivElement>) => void;
  onHeaderPointerCancel?: (event: PointerEvent<HTMLDivElement>) => void;
  headerRef?: RefObject<HTMLDivElement>;
  scrollableRef?: RefObject<HTMLDivElement>;
  contentInnerRef?: RefObject<HTMLDivElement>;
  answerAreaRef?: RefObject<HTMLDivElement>;
  composerRef?: RefObject<HTMLDivElement>;
  containerRef?: RefObject<HTMLDivElement>;
};

/** Electron通知とWebモーダルで共有するAgentOverlayの表示・操作シェル。 */
const AgentOverlayShell = ({
  isVisible,
  isContentVisible,
  isExpanded,
  content,
  suggestionText,
  isSuggestionStreamFinished,
  isSuggestionAccepted = false,
  actionText,
  conversationContent = null,
  composer = null,
  isActionStreamFinished,
  approvalUiState,
  approvalBlockers = [],
  isSubmittingApproval = false,
  approvalErrorMessage = null,
  showBusyIndicator,
  showThinking = false,
  openTaskFailed = false,
  chatActionId = null,
  onShowChat,
  fadeDurationMs = 600,
  onToggleExpand,
  onClose,
  onDecideApproval,
  onOpenWorkspaceSettings,
  conversationCopy,
  onHeaderPointerDown,
  onHeaderPointerMove,
  onHeaderPointerUp,
  onHeaderPointerCancel,
  headerRef,
  scrollableRef,
  contentInnerRef,
  answerAreaRef,
  composerRef,
  containerRef,
}: AgentOverlayShellProps) => {
  const internalScrollableRef = useRef<HTMLDivElement>(null);
  const resolvedScrollableRef = scrollableRef ?? internalScrollableRef;
  const expandButtonRef = useRef<HTMLButtonElement>(null);
  useCollapsedFocusBoundary(resolvedScrollableRef, !isExpanded, expandButtonRef);
  const { t } = useI18n();
  const showApprovalPanel = approvalUiState === 'approval_pending' && approvalBlockers.length > 0;
  const copyConversationFailed =
    conversationCopy?.status === 'failed' ? t('overlay.copyConversationFailed') : null;
  const suppressNextCloseClickRef = useRef(false);
  const scrollIndicatorHideTimeoutRef = useRef<number | null>(null);
  const handleCloseMouseDown = (event: React.MouseEvent<HTMLButtonElement>) => {
    if (!onClose) return;
    suppressNextCloseClickRef.current = true;
    event.preventDefault();
    event.stopPropagation();
    onClose();
  };
  const handleCloseClick = (event: React.MouseEvent<HTMLButtonElement>) => {
    if (!onClose) return;
    // Mouse path is handled on mousedown so the overlay can hide before macOS/Electron
    // reassigns key window focus. Keep keyboard activation via click when no preceding
    // mousedown was observed.
    if (suppressNextCloseClickRef.current) {
      suppressNextCloseClickRef.current = false;
      event.preventDefault();
      event.stopPropagation();
      return;
    }
    onClose();
  };

  useEffect(() => {
    return () => {
      if (scrollIndicatorHideTimeoutRef.current !== null) {
        window.clearTimeout(scrollIndicatorHideTimeoutRef.current);
      }
    };
  }, []);

  const handleScrollableContentScroll = (event: React.UIEvent<HTMLDivElement>) => {
    const target = event.currentTarget;
    target.dataset.scrolling = 'true';

    if (scrollIndicatorHideTimeoutRef.current !== null) {
      window.clearTimeout(scrollIndicatorHideTimeoutRef.current);
    }

    scrollIndicatorHideTimeoutRef.current = window.setTimeout(() => {
      delete target.dataset.scrolling;
      scrollIndicatorHideTimeoutRef.current = null;
    }, SCROLL_INDICATOR_HIDE_DELAY_MS);
  };

  return (
    <PopupContainer
      ref={containerRef}
      $bg={overlayBackground}
      $isVisible={isVisible}
      $fadeMs={fadeDurationMs}
      data-overlay-panel="true"
    >
      <HeaderRow
        ref={headerRef}
        onPointerDown={onHeaderPointerDown}
        onPointerMove={onHeaderPointerMove}
        onPointerUp={onHeaderPointerUp}
        onPointerCancel={onHeaderPointerCancel}
      >
        {showBusyIndicator && (
          <VisuallyHidden role="status">{t('overlay.actioning')}</VisuallyHidden>
        )}
        {chatActionId !== null && onShowChat && (
          <ShowChatButton
            $visible={isVisible}
            onClick={() => void onShowChat({ actionId: chatActionId })}
            aria-label={t('overlay.showInChat')}
            title={t('overlay.showInChat')}
          >
            <MessageCircle strokeWidth={1.75} aria-hidden />
          </ShowChatButton>
        )}
        <HeaderButtonGroup>
          {conversationCopy && (
            <>
              <HeaderIconButton
                $visible={isVisible}
                onClick={conversationCopy.copy}
                aria-label={t('overlay.copyConversation')}
                title={copyConversationFailed ?? t('overlay.copyConversation')}
              >
                {conversationCopy.status === 'copied' ? (
                  <Check strokeWidth={1.75} />
                ) : conversationCopy.status === 'failed' ? (
                  <CircleAlert strokeWidth={1.75} />
                ) : (
                  <Clipboard strokeWidth={1.75} />
                )}
              </HeaderIconButton>
              {copyConversationFailed ? (
                <VisuallyHidden role="alert">{copyConversationFailed}</VisuallyHidden>
              ) : null}
            </>
          )}
          {onToggleExpand && (
            <HeaderIconButton
              ref={expandButtonRef}
              $visible={isVisible}
              onClick={onToggleExpand}
              aria-label={isExpanded ? t('overlay.collapse') : t('overlay.expand')}
              title={isExpanded ? t('overlay.collapse') : t('overlay.expand')}
            >
              {isExpanded ? <ChevronUp strokeWidth={1.75} /> : <ChevronDown strokeWidth={1.75} />}
            </HeaderIconButton>
          )}
          {onClose && (
            <HeaderIconButton
              $visible={isVisible}
              onMouseDown={handleCloseMouseDown}
              onClick={handleCloseClick}
              aria-label={t('overlay.hideNotification')}
              title={t('overlay.hideNotification')}
            >
              <Minus strokeWidth={1.75} />
            </HeaderIconButton>
          )}
        </HeaderButtonGroup>
      </HeaderRow>

      <ContentFade $visible={isContentVisible}>
        <ScrollableContent
          ref={resolvedScrollableRef}
          $collapsed={!isExpanded}
          onScroll={handleScrollableContentScroll}
          data-overlay-scroll="true"
        >
          <ContentInner ref={contentInnerRef}>
            {typeof content === 'string' && content.trim().length > 0 ? (
              <ContentWrapper>
                <NotificationContent>
                  <MarkdownBlock text={content} isStreamFinished={true} />
                </NotificationContent>
              </ContentWrapper>
            ) : null}

            {suggestionText && (
              <ContentWrapper>
                <NotificationContent>
                  <MarkdownBlock
                    text={suggestionText}
                    isStreamFinished={isSuggestionStreamFinished}
                  />
                  {isSuggestionAccepted ? (
                    <SuggestionAcceptedStatus>
                      <Check size={14} aria-hidden="true" />
                      {t('overlay.suggestionAccepted')}
                    </SuggestionAcceptedStatus>
                  ) : null}
                </NotificationContent>
              </ContentWrapper>
            )}

            {(conversationContent || actionText || showThinking) && (
              <ContentWrapper>
                <AnswerArea ref={answerAreaRef} tabIndex={-1}>
                  {conversationContent ??
                    (actionText ? (
                      <MarkdownBlock text={actionText} isStreamFinished={isActionStreamFinished} />
                    ) : null)}
                  {showThinking ? (
                    <ThinkingLine aria-hidden>{t('overlay.thinking')}</ThinkingLine>
                  ) : null}
                </AnswerArea>
              </ContentWrapper>
            )}
          </ContentInner>
        </ScrollableContent>
      </ContentFade>

      <ComposerDock ref={composerRef}>
        {showApprovalPanel ? (
          <ApprovalDock>
            {approvalBlockers.map((blocker) => (
              <ApprovalPanelView
                key={`${blocker.approvalSessionId}:${blocker.toolRequestId}`}
                approvalPanel={blocker}
                approvalErrorMessage={approvalErrorMessage}
                isSubmittingApproval={isSubmittingApproval}
                onDecide={
                  onDecideApproval ? (decision) => onDecideApproval(decision, blocker) : undefined
                }
                onOpenWorkspaceSettings={onOpenWorkspaceSettings}
                t={t}
              />
            ))}
          </ApprovalDock>
        ) : null}
        {openTaskFailed ? <p role="alert">{t('overlay.openTaskFailed')}</p> : null}
        {composer}
      </ComposerDock>
    </PopupContainer>
  );
};

export default AgentOverlayShell;
