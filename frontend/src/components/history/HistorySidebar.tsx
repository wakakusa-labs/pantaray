import { MessageCircle, Trash2 } from 'lucide-react';
import { useEffect, useLayoutEffect, useRef, useState } from 'react';

import type { ConversationHistoryListItem } from '../../../electron/src/history/historyContracts';
import { useChatUnreadCount } from '@/components/chat/chatUnread';
import { ShortcutKeycaps } from '@/components/shortcut/ShortcutHint';
import {
  useGlobalShortcutHint,
  type ShortcutHintState,
} from '@/components/shortcut/useGlobalShortcutHint';
import { useI18n } from '@/context/useI18n';
import { groupHistoryByDay } from '@/history/historyDayGroups';
import { historyItemSelection, type HistorySelection } from '@/history/historySelection';
import { NEW_WORK_BUTTON_ID, openNewWork } from '@/history/newWork';
import { useHistoryLiveStages } from '@/hooks/useHistoryLiveStages';
import { itemIdentity, type useSuggestionHistory } from '@/hooks/useSuggestionHistory';
import { getLocaleForUiLanguage } from '@/i18n/translate';
import type { MessageKey } from '@/i18n/types';

import { HistoryDeleteDialog } from './HistoryDeleteDialog';
import HistorySearchField from './HistorySearchField';
import { liveStageText } from './liveStageText';
import { NewWorkButton } from './NewWorkButton';

const openButtonId = (identity: string) => `history-open:${identity}`;
const deleteButtonId = (identity: string) => `history-delete:${identity}`;
const CHAT_UNREAD_ID = 'history-chat-unread';
const MAX_SHOWN_UNREAD = 99;

/**
 * Why a row needs the user's eye, for its dot: an Action waiting for approval, a suggestion waiting
 * for an answer, or a completion not viewed yet. Null for a row that needs nothing.
 */
function attentionLabelKey(
  item: ConversationHistoryListItem,
  completionUnread: boolean
): MessageKey | null {
  if (item.status === 'approval_pending')
    return item.kind === 'suggestion'
      ? 'history.attention.suggestion'
      : 'history.status.approvalPending';
  return completionUnread ? 'history.unread' : null;
}

/**
 * A running or approval-waiting conversation is one whose own run the backend refuses to delete
 * (409 `CONVERSATION_BUSY`). A Suggestion's `approval_pending` only waits for the user's answer.
 */
function isDeleteBlocked(item: ConversationHistoryListItem): boolean {
  return item.kind === 'conversation' && item.status !== 'idle';
}

function deleteFailureMessageKey(errorCode: string | null): MessageKey {
  if (errorCode === 'CONVERSATION_BUSY') return 'history.delete.busy';
  if (errorCode === 'AUTHENTICATION_REQUIRED') return 'history.error.authenticationRequired';
  return 'history.delete.failed';
}

async function deleteHistoryItem(item: ConversationHistoryListItem): Promise<MessageKey | null> {
  const deleteItem = window.electron?.history?.deleteItem;
  if (!deleteItem) throw new Error('History delete bridge is unavailable.');
  const result = await deleteItem(
    item.kind === 'conversation'
      ? { kind: 'conversation', id: item.action_id }
      : { kind: 'suggestion', id: item.suggestion_id }
  );
  return result.ok ? null : deleteFailureMessageKey(result.errorCode);
}

/**
 * Points at the New task button, naming the shortcut only while it is registered. The sentence
 * stays one translatable string; `{shortcut}` marks where the keycaps replace it.
 */
function EmptyStateHint({
  state,
  t,
}: {
  state: ShortcutHintState;
  t: (key: MessageKey, vars?: Record<string, string | number>) => string;
}) {
  if (state.status !== 'ready')
    return <p className="history-empty-hint">{t('history.empty.startWithButton')}</p>;
  const [before, after] = t('history.empty.startWithShortcut').split('{shortcut}');
  return (
    <p className="history-empty-hint">
      {before}
      <ShortcutKeycaps accelerator={state.accelerator} t={t} />
      {after}
    </p>
  );
}

/** The chat's row, with Pantaray's unread messages counted. */
function ChatRow({
  t,
  current,
  onShow,
}: {
  t: (key: MessageKey, vars?: Record<string, string | number>) => string;
  current: boolean;
  onShow: () => void;
}) {
  const unread = useChatUnreadCount();
  return (
    <div className="history-sidebar__chat">
      <button
        type="button"
        className="history-chat-row"
        aria-current={current ? 'true' : undefined}
        aria-describedby={unread > 0 ? CHAT_UNREAD_ID : undefined}
        onClick={onShow}
      >
        <MessageCircle size={16} strokeWidth={1.8} aria-hidden="true" />
        <span className="history-chat-row__label">{t('history.chat.title')}</span>
        {unread > 0 ? (
          <span className="history-chat-row__count" aria-hidden="true">
            {unread > MAX_SHOWN_UNREAD ? `${MAX_SHOWN_UNREAD}+` : unread}
          </span>
        ) : null}
      </button>
      {unread > 0 ? (
        <span id={CHAT_UNREAD_ID} hidden>
          {t('history.chat.unread', { count: unread })}
        </span>
      ) : null}
    </div>
  );
}

/**
 * The History page's sidebar: New task, search, the chat, and the user's tasks by day. A row
 * selects what the detail pane shows; the selected one is marked current.
 */
export function HistorySidebar({
  history,
  selected,
  onSelect,
}: {
  /** The page reads the list too, for the selected task's title. */
  history: ReturnType<typeof useSuggestionHistory>;
  selected: HistorySelection;
  onSelect: (selection: HistorySelection, options?: { replace: true }) => void;
}) {
  const {
    items,
    loading,
    loadingMore,
    error,
    isRealtimeSyncing,
    searchText,
    setSearchText,
    loadMore,
    hasMore,
    isUnread,
    removeItem,
  } = history;
  const { t, language } = useI18n();
  const liveStages = useHistoryLiveStages();
  const [notice, setNotice] = useState<string | null>(null);
  const [confirmingDelete, setConfirmingDelete] = useState<ConversationHistoryListItem | null>(
    null
  );
  const [deletingIdentity, setDeletingIdentity] = useState<string | null>(null);
  const [focusTargetId, setFocusTargetId] = useState<string | null>(null);
  const shortcutHint = useGlobalShortcutHint();
  // A delete answers after the user may have moved on: its outcome reads the page as it is then.
  const latest = useRef({ items, selected, onSelect });
  useEffect(() => {
    latest.current = { items, selected, onSelect };
  });
  useLayoutEffect(() => {
    if (focusTargetId === null) return;
    document.getElementById(focusTargetId)?.focus();
    setFocusTargetId(null);
  }, [focusTargetId]);

  const cancelDelete = (): void => {
    if (confirmingDelete) setFocusTargetId(deleteButtonId(itemIdentity(confirmingDelete)));
    setConfirmingDelete(null);
  };
  const confirmDelete = async (): Promise<void> => {
    const item = confirmingDelete;
    if (!item) return;
    const identity = itemIdentity(item);
    setConfirmingDelete(null);
    setDeletingIdentity(identity);
    setNotice(null);
    let failureKey: MessageKey | null;
    try {
      failureKey = await deleteHistoryItem(item);
    } catch {
      failureKey = 'history.delete.failed';
    } finally {
      setDeletingIdentity(null);
    }
    if (failureKey !== null) {
      setNotice(t(failureKey));
      setFocusTargetId(deleteButtonId(identity));
      return;
    }
    const { items: rows, selected: shown, onSelect: select } = latest.current;
    const index = rows.findIndex((candidate) => itemIdentity(candidate) === identity);
    // A refresh may already have taken the row out; then nothing stands in its place.
    const successor: ConversationHistoryListItem | undefined =
      index === -1 ? undefined : (rows[index + 1] ?? rows[index - 1]);
    removeItem(identity);
    setFocusTargetId(successor ? openButtonId(itemIdentity(successor)) : NEW_WORK_BUTTON_ID);
    // The pane cannot keep showing a deleted task; this entry stands in for it in the history.
    if (historyItemSelection(item) === shown)
      select(successor ? historyItemSelection(successor) : 'chat', { replace: true });
  };
  const handleNewConversation = async (): Promise<void> => {
    setNotice(null);
    try {
      await openNewWork();
    } catch {
      setNotice(t('history.openOverlayFailed'));
    }
  };
  const renderContent = () => {
    if (loading) return <div className="history-loading">{t('history.loading')}</div>;
    if (error && items.length === 0)
      return (
        <div className="history-error" role="alert">
          {error}
        </div>
      );
    if (items.length === 0) {
      return (
        <div className="history-empty">
          <span>{t('history.empty')}</span>
          <EmptyStateHint state={shortcutHint} t={t} />
        </div>
      );
    }

    // Headings and rows are siblings in one flat list keyed by identity, so a live update that
    // adds or moves a day never remounts the rows that stay, nor takes focus from them.
    const headingsPerDay = new Map<string, number>();
    return (
      <>
        <ul className="history-list">
          {groupHistoryByDay(
            items,
            new Date(),
            { today: t('history.day.today'), yesterday: t('history.day.yesterday') },
            getLocaleForUiLanguage(language)
          ).flatMap((day) => {
            // Rows out of date order can bring a day back; its repeat count keeps the key unique.
            const repeat = headingsPerDay.get(day.key) ?? 0;
            headingsPerDay.set(day.key, repeat + 1);
            return [
              <li key={`day:${day.key}:${repeat}`} className="history-day">
                <h2>{day.label}</h2>
              </li>,
              ...day.items.map((item) => {
                const identity = itemIdentity(item);
                const selection = historyItemSelection(item);
                const attention = attentionLabelKey(item, isUnread(item));
                const liveStage =
                  item.kind === 'conversation' ? liveStages.get(item.action_id) : undefined;
                const content = (
                  <>
                    <span className="history-item-head">
                      <span className="history-item-title">{item.title}</span>
                      {item.kind === 'conversation' && item.status === 'running' ? (
                        // Heard, not seen: the live line shows the running task, out of hearing.
                        <span className="history-item-status">{t('history.status.running')}</span>
                      ) : null}
                      {attention ? (
                        <span
                          className="history-item-unread"
                          role="img"
                          aria-label={t(attention)}
                        />
                      ) : null}
                    </span>
                    {liveStage ? (
                      // Visual only: it changes on every step.
                      <span className="history-item-live" aria-hidden="true">
                        {liveStageText(liveStage, language, t)}
                      </span>
                    ) : null}
                  </>
                );

                return (
                  <li key={identity} className="history-item">
                    <button
                      type="button"
                      id={openButtonId(identity)}
                      className="history-item-button"
                      aria-current={selection === selected ? 'true' : undefined}
                      onClick={() => onSelect(selection)}
                    >
                      {content}
                    </button>
                    <button
                      type="button"
                      id={deleteButtonId(identity)}
                      className="history-item-delete"
                      aria-label={`${t('common.delete')} ${item.title}`}
                      title={t('common.delete')}
                      aria-busy={deletingIdentity === identity}
                      disabled={isDeleteBlocked(item) || deletingIdentity !== null}
                      onClick={() => setConfirmingDelete(item)}
                    >
                      <Trash2 size={15} aria-hidden="true" />
                    </button>
                  </li>
                );
              }),
            ];
          })}
        </ul>
        {error ? (
          <div className="history-error" role="alert">
            {error}
          </div>
        ) : null}
        {hasMore ? (
          <button
            type="button"
            className="history-filter-button history-load-more"
            disabled={loadingMore || isRealtimeSyncing}
            onClick={() => void loadMore()}
          >
            {loadingMore ? t('history.loadingMore') : t('history.loadMore')}
          </button>
        ) : null}
      </>
    );
  };

  return (
    <aside className="history-sidebar" aria-label={t('history.sidebar.label')}>
      <div className="history-sidebar__head">
        <NewWorkButton
          shortcutHint={shortcutHint}
          t={t}
          onClick={() => void handleNewConversation()}
        />
        <HistorySearchField searchText={searchText} onSearch={setSearchText} />
        {notice ? (
          <div className="history-error" role="alert">
            {notice}
          </div>
        ) : null}
      </div>
      <ChatRow t={t} current={selected === 'chat'} onShow={() => onSelect('chat')} />
      <div className="history-sidebar__tasks">{renderContent()}</div>
      {confirmingDelete ? (
        <HistoryDeleteDialog t={t} onCancel={cancelDelete} onConfirm={() => void confirmDelete()} />
      ) : null}
    </aside>
  );
}
