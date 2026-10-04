import { Plus, RefreshCw, Trash2 } from 'lucide-react';
import { useLayoutEffect, useState } from 'react';

import type { ConversationHistoryListItem } from '../../electron/src/history/historyContracts';
import { resolveToolLine } from '@/components/action-conversation/toolDisplayName';
import { HistoryDeleteDialog } from '@/components/history/HistoryDeleteDialog';
import HistorySearchField from '@/components/history/HistorySearchField';
import { getConversationHistoryStatusMeta } from '@/components/history/statusTokens';
import { ariaKeyShortcuts, acceleratorKeycaps } from '@/components/shortcut/acceleratorKeycaps';
import { ShortcutKeycaps } from '@/components/shortcut/ShortcutHint';
import {
  useGlobalShortcutHint,
  type ShortcutHintState,
} from '@/components/shortcut/useGlobalShortcutHint';
import { useI18n } from '@/context/useI18n';
import { groupHistoryByDay } from '@/history/historyDayGroups';
import type { HistoryLiveStage } from '@/history/historyLiveStage';
import { useHistoryLiveStages } from '@/hooks/useHistoryLiveStages';
import { itemIdentity, useSuggestionHistory } from '@/hooks/useSuggestionHistory';
import { getLocaleForUiLanguage } from '@/i18n/translate';
import type { MessageKey } from '@/i18n/types';

import './suggestionHistoryPage.css';

const NEW_CONVERSATION_BUTTON_ID = 'history-new-conversation';
const SHORTCUT_UNAVAILABLE_ID = 'history-new-conversation-shortcut-unavailable';
const openButtonId = (identity: string) => `history-open:${identity}`;
const deleteButtonId = (identity: string) => `history-delete:${identity}`;

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

function openSuggestionHistory(suggestionId: string): void {
  const showHistory = window.electron?.agentOverlay?.showHistory;
  if (!showHistory) throw new Error('Suggestion history overlay bridge is unavailable.');
  showHistory({ suggestionId, initialUiState: { expand: true }, fromStart: true });
}

async function openConversation(actionId: string): Promise<void> {
  const open = window.electron?.history?.openConversation;
  if (!open) throw new Error('Conversation overlay bridge is unavailable.');
  await open({ actionId });
}

function liveStageText(
  stage: HistoryLiveStage,
  language: 'en' | 'ja',
  t: (key: MessageKey) => string
): string {
  switch (stage.kind) {
    case 'tool':
      return resolveToolLine(stage.label, language, {
        subject: stage.subject,
        running: true,
        outcome: stage.outcome,
      }).text;
    case 'message':
      return stage.text;
    case 'thinking':
      return t('overlay.thinking');
  }
}

async function openNewConversation(): Promise<void> {
  const open = window.electron?.history?.openNewConversation;
  if (!open) throw new Error('New conversation bridge is unavailable.');
  await open();
}

/**
 * Points at the header button, naming the shortcut only while it is registered. The sentence stays
 * one translatable string; `{shortcut}` marks where the keycaps replace it.
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

/**
 * The global shortcut opens the same Overlay, so it sits inside the button the way menus show
 * shortcuts. The keycaps are drawn only; assistive technology gets `aria-keyshortcuts`.
 */
function NewConversationButton({
  shortcutHint,
  t,
  onClick,
}: {
  shortcutHint: ShortcutHintState;
  t: (key: MessageKey, vars?: Record<string, string | number>) => string;
  onClick: () => void;
}) {
  const isUnavailable = shortcutHint.status === 'unavailable';
  const accelerator = shortcutHint.status === 'ready' ? shortcutHint.accelerator : null;
  return (
    <>
      <button
        type="button"
        id={NEW_CONVERSATION_BUTTON_ID}
        className="history-new-conversation-button"
        aria-keyshortcuts={
          accelerator === null
            ? undefined
            : ariaKeyShortcuts(
                acceleratorKeycaps(accelerator, window.electron?.process.platform === 'darwin')
              )
        }
        title={isUnavailable ? t('shortcut.hint.unavailable') : undefined}
        aria-describedby={isUnavailable ? SHORTCUT_UNAVAILABLE_ID : undefined}
        onClick={onClick}
      >
        <Plus size={16} aria-hidden="true" />
        {t('history.newConversation')}
        {accelerator === null ? null : (
          <span className="history-new-conversation-keys" aria-hidden="true">
            <ShortcutKeycaps accelerator={accelerator} t={t} />
          </span>
        )}
      </button>
      {isUnavailable ? (
        <span id={SHORTCUT_UNAVAILABLE_ID} hidden>
          {t('shortcut.hint.unavailable')}
        </span>
      ) : null}
    </>
  );
}

const SuggestionHistoryPage = () => {
  const {
    items,
    loading,
    loadingMore,
    error,
    isRealtimeSyncing,
    searchText,
    setSearchText,
    refresh,
    loadMore,
    hasMore,
    isUnread,
    removeItem,
  } = useSuggestionHistory();
  const { t, language, formatDateTime } = useI18n();
  const liveStages = useHistoryLiveStages();
  const [notice, setNotice] = useState<string | null>(null);
  const [confirmingDelete, setConfirmingDelete] = useState<ConversationHistoryListItem | null>(
    null
  );
  const [deletingIdentity, setDeletingIdentity] = useState<string | null>(null);
  const [focusTargetId, setFocusTargetId] = useState<string | null>(null);
  const shortcutHint = useGlobalShortcutHint();
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
    const identities = items.map(itemIdentity);
    const index = identities.indexOf(identity);
    const successor = identities[index + 1] ?? identities[index - 1];
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
    removeItem(identity);
    setFocusTargetId(successor ? openButtonId(successor) : NEW_CONVERSATION_BUTTON_ID);
  };
  const handleNewConversation = async (): Promise<void> => {
    setNotice(null);
    try {
      await openNewConversation();
    } catch {
      setNotice(t('history.openOverlayFailed'));
    }
  };
  const handleConversation = async (actionId: string): Promise<void> => {
    setNotice(null);
    try {
      await openConversation(actionId);
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

    const locale = getLocaleForUiLanguage(language);
    const formatTime = new Intl.DateTimeFormat(locale, { hour: '2-digit', minute: '2-digit' });
    // Headings and rows are siblings in one flat list keyed by identity, so a live update that
    // adds or moves a day never remounts the rows that stay, nor takes focus from them.
    const headingsPerDay = new Map<string, number>();
    return (
      <div className="history-list">
        {groupHistoryByDay(
          items,
          new Date(),
          { today: t('history.day.today'), yesterday: t('history.day.yesterday') },
          locale
        ).flatMap((day) => {
          // Rows out of date order can bring a day back; its repeat count keeps the key unique.
          const repeat = headingsPerDay.get(day.key) ?? 0;
          headingsPerDay.set(day.key, repeat + 1);
          return [
            <h2 key={`day:${day.key}:${repeat}`} className="history-day">
              {day.label}
            </h2>,
            ...day.items.map((item) => {
              const identity = itemIdentity(item);
              const statusMeta = getConversationHistoryStatusMeta(item.status);
              const liveStage =
                item.kind === 'conversation' ? liveStages.get(item.action_id) : undefined;
              const content = (
                <div className="history-item-body">
                  <div className="history-item-text">
                    <p className="history-item-title">{item.title}</p>
                    <div className="history-item-meta">
                      <span>
                        {day.isRecent
                          ? formatTime.format(new Date(item.updated_at))
                          : formatDateTime(new Date(item.updated_at))}
                      </span>
                    </div>
                    {liveStage ? (
                      // Visual only: it changes on every step, and the badge carries the status.
                      <p className="history-item-live" aria-hidden="true">
                        {liveStageText(liveStage, language, t)}
                      </p>
                    ) : null}
                  </div>
                  <div className="history-item-status">
                    {isUnread(item) ? <span aria-label={t('history.unread')}>●</span> : null}
                    {statusMeta ? (
                      <span className={`badge badge--${statusMeta.tone}`}>
                        {t(statusMeta.labelKey)}
                      </span>
                    ) : null}
                  </div>
                </div>
              );

              return (
                <div key={identity} className="history-item">
                  <button
                    type="button"
                    id={openButtonId(identity)}
                    className="history-item-button"
                    onClick={() => {
                      if (item.kind === 'conversation') {
                        void handleConversation(item.action_id);
                        return;
                      }
                      setNotice(null);
                      try {
                        openSuggestionHistory(item.suggestion_id);
                      } catch {
                        setNotice(t('history.openOverlayFailed'));
                      }
                    }}
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
                    <Trash2 size={16} aria-hidden="true" />
                  </button>
                </div>
              );
            }),
          ];
        })}
        {error ? (
          <div className="history-error" role="alert">
            {error}
          </div>
        ) : null}
        {hasMore ? (
          <button
            type="button"
            className="history-filter-button"
            disabled={loadingMore || isRealtimeSyncing}
            onClick={() => void loadMore()}
          >
            {loadingMore ? t('history.loadingMore') : t('history.loadMore')}
          </button>
        ) : null}
      </div>
    );
  };

  return (
    <div className="history-container">
      <div className="history-header">
        <div className="history-toolbar">
          <HistorySearchField searchText={searchText} onSearch={setSearchText} />
          <button
            type="button"
            className="history-toolbar__icon-button"
            aria-label={t('history.reload')}
            title={t('history.reload')}
            onClick={() => void refresh()}
          >
            <RefreshCw size={16} aria-hidden="true" />
          </button>
          <NewConversationButton
            shortcutHint={shortcutHint}
            t={t}
            onClick={() => void handleNewConversation()}
          />
        </div>
        {isRealtimeSyncing ? <div className="history-sync">{t('history.syncing')}</div> : null}
        {notice ? (
          <div className="history-error" role="alert">
            {notice}
          </div>
        ) : null}
      </div>
      {renderContent()}
      {confirmingDelete ? (
        <HistoryDeleteDialog t={t} onCancel={cancelDelete} onConfirm={() => void confirmDelete()} />
      ) : null}
    </div>
  );
};

export default SuggestionHistoryPage;
