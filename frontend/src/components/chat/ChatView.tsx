import { RefreshCw } from 'lucide-react';
import { useMemo, useState, type ReactNode } from 'react';

import type { ChatCard as ChatCardData } from '../../../electron/src/chat/chatContracts';
import { NewWorkButton } from '@/components/history/NewWorkButton';
import { useGlobalShortcutHint } from '@/components/shortcut/useGlobalShortcutHint';
import { useI18n } from '@/context/useI18n';
import { groupByLocalDay } from '@/history/historyDayGroups';
import { openNewWork } from '@/history/newWork';
import { useChatItems } from '@/hooks/useChatItems';
import { useChatWorkStates } from '@/hooks/useChatWorkStates';
import { getLocaleForUiLanguage } from '@/i18n/translate';

import { ChatMessage } from './ChatMessage';
import {
  cardWorkKey,
  isChatMessage,
  isShownInChat,
  latestCardPositions,
  type WorkKey,
} from './chatTimeline';
import { useChatScroll } from './useChatScroll';

import './chatView.css';

async function openCard(card: ChatCardData): Promise<void> {
  if (card.kind === 'action') {
    const open = window.electron?.history?.openConversation;
    if (!open) throw new Error('Conversation overlay bridge is unavailable.');
    await open({ actionId: card.action_id });
    return;
  }
  const showHistory = window.electron?.agentOverlay?.showHistory;
  if (!showHistory) throw new Error('Suggestion history overlay bridge is unavailable.');
  showHistory({
    suggestionId: card.suggestion_id,
    initialUiState: { expand: true },
    fromStart: true,
  });
}

/**
 * The single chat in the History page: messages with their cards, read newest first and
 * scrolled up for older pages.
 */
export function ChatView({
  modeSwitch,
  turnInProgress,
  onRetryTurn,
}: {
  modeSwitch: ReactNode;
  /** True while a chat turn runs; drawn as a 「…」 bubble after the last message. */
  turnInProgress: boolean;
  /** Runs the failed turn again; null hides the retry button. */
  onRetryTurn: ((failureItemId: string) => void) | null;
}) {
  const { t, language } = useI18n();
  const shortcutHint = useGlobalShortcutHint();
  const chat = useChatItems();
  const works = useChatWorkStates();
  const [notice, setNotice] = useState<string | null>(null);
  const [openWork, setOpenWork] = useState<WorkKey | null>(null);
  const ready = !chat.loading;
  const scroll = useChatScroll({
    items: chat.items,
    ready,
    hasOlder: chat.hasOlder,
    loadingOlder: chat.loadingOlder,
    loadOlder: chat.loadOlder,
  });
  const itemsById = useMemo(
    () => new Map(chat.items.map((item) => [item.item_id, item])),
    [chat.items]
  );
  const latestCards = useMemo(() => latestCardPositions(chat.items), [chat.items]);
  const shown = useMemo(() => chat.items.filter(isShownInChat), [chat.items]);

  const handleNewWork = async (): Promise<void> => {
    setNotice(null);
    try {
      await openNewWork();
    } catch {
      setNotice(t('history.openOverlayFailed'));
    }
  };
  const handleOpenCard = async (card: ChatCardData): Promise<void> => {
    setNotice(null);
    try {
      await openCard(card);
      setOpenWork(cardWorkKey(card));
    } catch {
      setNotice(t('history.openOverlayFailed'));
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
      ...day.items.map((item) => {
        if (isChatMessage(item)) {
          return (
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
                works={works.states}
                openWork={openWork}
                t={t}
                onOpenCard={(card) => void handleOpenCard(card)}
              />
            </li>
          );
        }
        if (item.content.kind !== 'turn_failure') return null;
        return (
          <li key={item.item_id} className="chat-failure">
            <p>{t(`history.chat.failure.${item.content.reason}`)}</p>
            {onRetryTurn ? (
              <button
                type="button"
                className="history-filter-button"
                onClick={() => onRetryTurn(item.item_id)}
              >
                {t('history.chat.retry')}
              </button>
            ) : null}
          </li>
        );
      }),
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
      <div ref={scroll.scrollRef} className="chat-scroll" onScroll={scroll.onScroll}>
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
    <div className="history-container">
      <div className="history-header">
        <div className="history-toolbar">
          {modeSwitch}
          <span className="history-toolbar__spacer" />
          <button
            type="button"
            className="history-toolbar__icon-button"
            aria-label={t('history.reload')}
            title={t('history.reload')}
            onClick={() => {
              void chat.reload();
              void works.reload();
            }}
          >
            <RefreshCw size={16} aria-hidden="true" />
          </button>
          <NewWorkButton shortcutHint={shortcutHint} t={t} onClick={() => void handleNewWork()} />
        </div>
        {notice ? (
          <div className="history-error" role="alert">
            {notice}
          </div>
        ) : null}
      </div>
      {renderBody()}
      <div className="chat-announcer" role="status">
        {chat.arrived?.content.kind === 'assistant_message'
          ? t('history.chat.announce', {
              name: t('history.chat.pantaray'),
              text: chat.arrived.content.text,
            })
          : null}
      </div>
    </div>
  );
}
