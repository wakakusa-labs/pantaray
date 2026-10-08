import type { ChatCard as ChatCardData, ChatItem } from '../../../electron/src/chat/chatContracts';
import { MarkdownBlock } from '@/components/agent-overlay/MarkdownRenderer';
import type { ChatWorkState } from '@/hooks/useChatWorkStates';
import type { MessageKey } from '@/i18n/types';

import { ChatCard } from './ChatCard';
import {
  cardPosition,
  cardWorkKey,
  isChatMessage,
  type ChatMessageItem,
  type WorkKey,
} from './chatTimeline';

type Translate = (key: MessageKey, vars?: Record<string, string | number>) => string;
function speakerName(item: ChatMessageItem, t: Translate): string {
  return item.content.kind === 'user_message' ? t('history.chat.you') : t('history.chat.pantaray');
}

/** The quoted message, or a stated absence when it is on a page not read yet. */
function QuotedMessage({ quoted, t }: { quoted: ChatItem | undefined; t: Translate }) {
  if (quoted && isChatMessage(quoted)) {
    return (
      <blockquote className="chat-quote">
        <b>{speakerName(quoted, t)}</b>
        <span>{quoted.content.text}</span>
      </blockquote>
    );
  }
  return (
    <blockquote className="chat-quote">
      <span>{t('history.chat.quoteUnavailable')}</span>
    </blockquote>
  );
}

/** One message: the user's on the right, Pantaray's on the left with its cards. */
export function ChatMessage({
  item,
  quoted,
  time,
  latestCards,
  works,
  openWork,
  t,
  onOpenCard,
}: {
  item: ChatMessageItem;
  quoted: ChatItem | undefined;
  time: string;
  latestCards: ReadonlyMap<WorkKey, string>;
  works: ReadonlyMap<WorkKey, ChatWorkState>;
  openWork: WorkKey | null;
  t: Translate;
  onOpenCard: (card: ChatCardData) => void;
}) {
  const { content } = item;
  const mine = content.kind === 'user_message';
  return (
    <article
      className={mine ? 'chat-row chat-row--mine' : 'chat-row'}
      aria-label={speakerName(item, t)}
    >
      <div className="chat-stack">
        <div className="chat-bubble">
          {content.quote_item_id !== null ? <QuotedMessage quoted={quoted} t={t} /> : null}
          {mine ? (
            <p className="chat-bubble__text">{content.text}</p>
          ) : (
            <MarkdownBlock text={content.text} isStreamFinished />
          )}
        </div>
        {content.kind === 'assistant_message'
          ? content.cards.map((card, index) => {
              const work = cardWorkKey(card);
              return (
                <ChatCard
                  key={cardPosition(item.item_id, index)}
                  card={card}
                  work={works.get(work)}
                  isLatest={latestCards.get(work) === cardPosition(item.item_id, index)}
                  isOpen={openWork === work}
                  t={t}
                  onOpen={() => onOpenCard(card)}
                />
              );
            })
          : null}
      </div>
      <time className="chat-time" dateTime={item.created_at}>
        {time}
      </time>
    </article>
  );
}
