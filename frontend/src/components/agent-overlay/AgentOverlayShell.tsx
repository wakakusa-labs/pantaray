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
import { Check, ChevronDown, ChevronUp, CircleAlert, Clipboard, Minus } from 'lucide-react';
import styled, { keyframes } from 'styled-components';
import type { ActionApprovalBlocker } from '../../../electron/src/actions/actionLiveCore';
import { useCollapsedFocusBoundary } from './useCollapsedFocusBoundary';
import type { ClipboardCopyStatus } from './useClipboardCopy';
import { SuggestionDecisionControls, type SuggestionDecision } from './SuggestionDecisionControls';

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

const busyPulse = keyframes`
  0%, 80%, 100% { opacity: 0.25; }
  40% { opacity: 1; }
`;

/**
 * 停止がまだ出せない間（採用直後、run が始まるまで）の進行表示。
 *
 * この局面では会話も本文もまだ無いので、何も出さないとパネルは止まって見える。
 * 停止と場所も幅も共有し、どちらか一方だけが出る。文字は読み上げにだけ残す。
 */
const BusyIndicator = styled.span`
  margin-right: auto;
  display: inline-flex;
  align-items: center;
  gap: 4px;
  height: 32px;
  padding: 0 10px;
`;

const BusyDot = styled.span`
  width: 4px;
  height: 4px;
  border-radius: 50%;
  background: rgba(255, 255, 255, 0.9);
  animation: ${busyPulse} 1.2s ease-in-out infinite;

  @media (prefers-reduced-motion: reduce) {
    animation: none;
    opacity: 0.7;
  }
`;

const VisuallyHidden = styled.span`
  position: absolute;
  width: 1px;
  height: 1px;
  overflow: hidden;
  clip-path: inset(50%);
  white-space: nowrap;
`;

const BUSY_DOT_DELAYS_S = [0, 0.15, 0.3];

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
  suggestionId?: string | null;
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
  showBusyIndicator: boolean;
  showThinking?: boolean;
  showFooterActions?: boolean;
  fadeDurationMs?: number;
  onToggleExpand?: () => void;
  onClose?: () => void;
  suggestionDecision?: SuggestionDecision;
  onReject?: () => void;
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
  footerRef?: RefObject<HTMLDivElement>;
  composerRef?: RefObject<HTMLDivElement>;
  containerRef?: RefObject<HTMLDivElement>;
};

/** Electron通知とWebモーダルで共有するAgentOverlayの表示・操作シェル。 */
const AgentOverlayShell = ({
  suggestionId,
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
  showFooterActions = true,
  fadeDurationMs = 600,
  onToggleExpand,
  onClose,
  suggestionDecision,
  onReject,
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
  footerRef,
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
          <BusyIndicator role="status">
            <VisuallyHidden>{t('overlay.actioning')}</VisuallyHidden>
            {BUSY_DOT_DELAYS_S.map((delay) => (
              <BusyDot key={delay} style={{ animationDelay: `${delay}s` }} aria-hidden />
            ))}
          </BusyIndicator>
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
          $hasFooter={Boolean(showFooterActions)}
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
        {composer}
      </ComposerDock>

      {showFooterActions && suggestionDecision && (
        <SuggestionDecisionControls
          key={suggestionId}
          isVisible={isVisible}
          isBusy={showBusyIndicator}
          compact={!isExpanded}
          footerRef={footerRef}
          decision={suggestionDecision}
          onReject={onReject}
        />
      )}
    </PopupContainer>
  );
};

export default AgentOverlayShell;
