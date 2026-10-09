import { ArrowUpRight } from 'lucide-react';
import { useId } from 'react';

import type { ChatCard as ChatCardData } from '../../../electron/src/chat/chatContracts';
import {
  badgeClassName,
  getConversationHistoryStatusMeta,
} from '@/components/history/statusTokens';
import type { ChatWorkState } from '@/hooks/useChatWorkStates';
import type { MessageKey } from '@/i18n/types';

/**
 * A work an assistant message points at: its current title, the summary written with the
 * message, and the work's status when this is its latest card.
 */
export function ChatCard({
  id,
  card,
  work,
  isLatest,
  isOpen,
  t,
  onOpen,
}: {
  /** The element id a request to show this work scrolls to. */
  id: string;
  card: ChatCardData;
  /** Undefined when the work was deleted or is older than the states read for the chat. */
  work: ChatWorkState | undefined;
  isLatest: boolean;
  isOpen: boolean;
  t: (key: MessageKey, vars?: Record<string, string | number>) => string;
  onOpen: () => void;
}) {
  const summaryId = useId();
  const statusId = useId();
  const statusMeta = isLatest && work ? getConversationHistoryStatusMeta(work.status) : null;
  return (
    <button
      id={id}
      type="button"
      className={isOpen ? 'chat-card chat-card--open' : 'chat-card'}
      aria-label={t('history.chat.card.openLabel', { title: work?.title ?? card.summary })}
      aria-describedby={statusMeta ? `${summaryId} ${statusId}` : summaryId}
      aria-current={isOpen ? 'true' : undefined}
      onClick={onOpen}
    >
      {work ? (
        <span className="chat-card__top">
          <span className="chat-card__title">{work.title}</span>
          {statusMeta ? (
            <span id={statusId} className={badgeClassName(statusMeta.tone)}>
              {t(statusMeta.labelKey)}
            </span>
          ) : null}
        </span>
      ) : null}
      <span id={summaryId} className="chat-card__summary">
        {card.summary}
      </span>
      <span className="chat-card__foot" aria-hidden="true">
        <span className="chat-card__open">
          {t('history.chat.card.open')}
          <ArrowUpRight size={12} />
        </span>
      </span>
    </button>
  );
}
