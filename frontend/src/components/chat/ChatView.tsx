import {
  forwardRef,
  useEffect,
  useImperativeHandle,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from 'react';

import { useI18n } from '@/context/useI18n';
import { groupByLocalDay } from '@/history/historyDayGroups';
import type { ChatItemsResult } from '@/hooks/useChatItems';
import { useChatWorkStates } from '@/hooks/useChatWorkStates';
import { getLocaleForUiLanguage } from '@/i18n/translate';

import { ChatComposer } from './ChatComposer';
import { ChatMessage } from './ChatMessage';
import {
  cardElementId,
  cardWorkKey,
  isChatMessage,
  latestCardPositions,
  messageElementId,
  retryableFailure,
  type WorkKey,
} from './chatTimeline';
import type { ChatComposerControl } from './useChatComposer';
import { useChatReadMark } from './useChatReadMark';
import { useChatReveal, type ChatReveal } from './useChatReveal';
import { useChatScroll } from './useChatScroll';

import './chatView.css';

/**
 * A jump to a message: from a quote to the quoted message, with `returnTo` the quote's message,
 * or back to that message. `shown` once the message is in view.
 */
type ChatJump = Readonly<{ key: string; itemId: string; returnTo: string | null; shown: boolean }>;

/** What the page asks of the chat from outside it: the sidebar's Chat row shows the newest. */
export type ChatViewHandle = { showNewest: () => void };

/**
 * The single chat in the History page's detail pane: messages with their cards, read newest first
 * and scrolled up for older pages.
 */
export const ChatView = forwardRef<
  ChatViewHandle,
  {
    /** Shown under the pane's title (the app's AI-connection notice). */
    notice: ReactNode;
    /** The chat and its composer outlive this view, so leaving the page loses neither. */
    chat: ChatItemsResult;
    composer: ChatComposerControl;
    /** The Action whose latest card the Overlay or its task pane asked to show, or null. */
    reveal: ChatReveal | null;
    /** A card selects its work for the detail pane. */
    onOpenWork: (work: WorkKey) => void;
  }
>(function ChatView({ notice: pageNotice, chat, composer, reveal, onOpenWork }, ref) {
  const { t, language } = useI18n();
  const works = useChatWorkStates();
  const [notice, setNotice] = useState<string | null>(null);
  // The work whose card was asked to be shown; its card is outlined.
  const [openWork, setOpenWork] = useState<WorkKey | null>(null);
  const [revealSeen, setRevealSeen] = useState<string | null>(null);
  const [jump, setJump] = useState<ChatJump | null>(null);
  // An Overlay request the reader left for the newest message; only a new request shows a card.
  const [revealDropped, setRevealDropped] = useState<string | null>(null);
  if (reveal && reveal.key !== revealSeen) {
    setRevealSeen(reveal.key);
    setOpenWork(`action:${reveal.actionId}`);
    // The Overlay's request is the newer one; a jump still looking for its message gives way.
    setJump(null);
  }
  const ready = !chat.loading;
  const scroll = useChatScroll({
    items: chat.items,
    typing: chat.turnRunning,
    ready,
    hasOlder: chat.hasOlder,
    failed: chat.failed,
    loadingOlder: chat.loadingOlder,
    loadOlder: chat.loadOlder,
  });
  const markRead = useChatReadMark(chat.items, scroll.isAtNewest);
  const itemsById = useMemo(
    () => new Map(chat.items.map((item) => [item.item_id, item])),
    [chat.items]
  );
  const latestCards = useMemo(() => latestCardPositions(chat.items), [chat.items]);
  // Bridge events and turn failures feed the chat's model; the user sees the messages.
  const shown = useMemo(() => chat.items.filter(isChatMessage), [chat.items]);
  const failure = useMemo(() => retryableFailure(chat.items), [chat.items]);
  // The retry sits under the user's last message, so that message has to be on a page read.
  const { hasOlder, failed: readFailed, loadingOlder, loadOlder } = chat;
  useEffect(() => {
    if (
      failure !== null &&
      failure.messageItemId === null &&
      hasOlder &&
      !readFailed &&
      !loadingOlder
    )
      void loadOlder();
  }, [failure, hasOlder, readFailed, loadingOlder, loadOlder]);
  const [retrying, setRetrying] = useState(false);
  const turnInProgress = chat.turnRunning;
  const reading = {
    ready,
    hasOlder,
    failed: readFailed,
    loadingOlder,
    // Pages read on the way to the target keep the reader's place, in case it is never found.
    loadOlder: scroll.loadOlderKeepingPlace,
    readPlace: scroll.readPlace,
  };
  const revealCard = reveal ? latestCards.get(`action:${reveal.actionId}`) : undefined;
  useChatReveal({
    ...reading,
    target:
      reveal && reveal.key !== revealDropped
        ? {
            key: reveal.key,
            elementId: revealCard === undefined ? null : cardElementId(revealCard),
          }
        : null,
  });
  const jumpCount = useRef(0);
  const jumpTo = (itemId: string, returnTo: string | null) => {
    jumpCount.current += 1;
    setJump({ key: String(jumpCount.current), itemId, returnTo, shown: false });
  };
  useChatReveal({
    ...reading,
    target: jump && {
      key: jump.key,
      elementId: itemsById.has(jump.itemId) ? messageElementId(jump.itemId) : null,
    },
    onShown: () => setJump((current) => current && { ...current, shown: true }),
  });
  const returnTo = jump?.shown ? jump.returnTo : null;
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  // Back at the newest message the reader sees it, so the existing check marks it read.
  useImperativeHandle(ref, () => ({
    showNewest: () => {
      setJump(null);
      setRevealDropped(reveal?.key ?? null);
      scroll.followNewest();
      markRead();
    },
  }));

  const handleRetryTurn = async (failureItemId: string): Promise<void> => {
    setNotice(null);
    setRetrying(true);
    try {
      await chat.retryTurn(failureItemId);
    } catch {
      setNotice(t('history.chat.retryFailed'));
    } finally {
      setRetrying(false);
    }
  };

  const locale = getLocaleForUiLanguage(language);
  const formatTime = new Intl.DateTimeFormat(locale, { hour: '2-digit', minute: '2-digit' });
  const renderItems = () =>
    groupByLocalDay(
      shown,
      (item) => item.created_at,
      new Date(),
      { today: t('history.day.today'), yesterday: t('history.day.yesterday') },
      locale
    ).flatMap((day) => [
      // Items are in `sequence` order, which only moves forward in time, so a day comes once.
      <li key={`day:${day.key}`} className="chat-day">
        <h2>{day.label}</h2>
      </li>,
      ...day.items.map((item) => (
        <li key={item.item_id}>
          <ChatMessage
            item={item}
            quoted={
              item.content.quote_item_id === null
                ? undefined
                : itemsById.get(item.content.quote_item_id)
            }
            time={formatTime.format(new Date(item.created_at))}
            latestCards={latestCards}
            works={works}
            openWork={openWork}
            t={t}
            onOpenCard={(card) => onOpenWork(cardWorkKey(card))}
            onJumpToQuote={(quoteItemId) => jumpTo(quoteItemId, item.item_id)}
            found={jump?.shown === true && jump.itemId === item.item_id}
            onQuote={
              composer.state.pending === null
                ? () => {
                    composer.setQuote(item);
                    textareaRef.current?.focus();
                  }
                : null
            }
            retry={
              failure !== null && failure.messageItemId === item.item_id && !turnInProgress
                ? {
                    label: t(`history.chat.retry.${failure.reason}`),
                    disabled: retrying,
                    onRetry: () => void handleRetryTurn(failure.failureItemId),
                  }
                : null
            }
          />
        </li>
      )),
    ]);

  const renderBody = () => {
    if (chat.loading) return <div className="history-loading">{t('history.chat.loading')}</div>;
    if (chat.failed && chat.items.length === 0)
      return (
        <div className="history-error" role="alert">
          {t('history.chat.loadFailed')}
        </div>
      );
    if (shown.length === 0 && !chat.hasOlder && !turnInProgress)
      return <div className="history-empty">{t('history.chat.empty')}</div>;
    return (
      <div
        ref={scroll.scrollRef}
        className="chat-scroll"
        onScroll={() => {
          scroll.onScroll();
          markRead();
        }}
      >
        <div className="chat-column">
          {chat.hasOlder ? (
            <button
              ref={scroll.olderTriggerRef}
              type="button"
              className="history-filter-button chat-older"
              disabled={chat.loadingOlder}
              onClick={scroll.loadOlderKeepingPlace}
            >
              {chat.loadingOlder ? t('history.loadingMore') : t('history.chat.loadOlder')}
            </button>
          ) : null}
          <ol className="chat-items" aria-label={t('history.chat.label')}>
            {renderItems()}
            {turnInProgress ? (
              <li className="chat-row">
                <div className="chat-typing" role="status" aria-label={t('history.chat.typing')}>
                  <i />
                  <i />
                  <i />
                </div>
              </li>
            ) : null}
          </ol>
          {chat.failed ? (
            <div className="history-error" role="alert">
              {t('history.chat.loadFailed')}
            </div>
          ) : null}
        </div>
      </div>
    );
  };

  return (
    <div className="history-container chat-view">
      <header className="history-pane-header">
        <h1>{t('history.chat.title')}</h1>
      </header>
      {pageNotice}
      {notice ? (
        <div className="history-error history-pane-notice" role="alert">
          {notice}
        </div>
      ) : null}
      {renderBody()}
      {returnTo !== null ? (
        <button
          type="button"
          className="history-filter-button chat-return"
          onClick={() => jumpTo(returnTo, null)}
        >
          {t('history.chat.quote.back')}
        </button>
      ) : null}
      <ChatComposer
        composer={composer}
        textareaRef={textareaRef}
        t={t}
        onSend={() => {
          setJump(null);
          scroll.followNewest();
          composer.send();
        }}
      />
      <div className="chat-announcer" role="status">
        {chat.arrived?.content.kind === 'assistant_message'
          ? t('history.chat.announce', {
              name: t('history.chat.pantaray'),
              text: chat.arrived.content.text,
            })
          : chat.arrived?.content.kind === 'turn_failure'
            ? t(`history.chat.failure.${chat.arrived.content.reason}`)
            : null}
      </div>
    </div>
  );
});
