import { Reply, RotateCcw } from 'lucide-react';
import { useId } from 'react';

import type { ChatCard as ChatCardData, ChatItem } from '../../../electron/src/chat/chatContracts';
import { AttachedFileChip } from '@/components/action-conversation/AttachedFileChip';
import { AttachedImages } from '@/components/action-conversation/AttachedImages';
import { ProjectRefText } from '@/components/action-conversation/ProjectRefText';
import { USER_MESSAGE_COPY } from '@/components/action-conversation/userMessageCopy';
import { MarkdownBlock } from '@/components/agent-overlay/MarkdownRenderer';
import { useI18n } from '@/context/useI18n';
import type { ChatWorkState } from '@/hooks/useChatWorkStates';
import type { MessageKey } from '@/i18n/types';

import { ChatCard } from './ChatCard';
import {
  cardElementId,
  cardPosition,
  cardWorkKey,
  isChatMessage,
  messageElementId,
  speakerKey,
  type ChatMessageItem,
  type WorkKey,
} from './chatTimeline';

type Translate = (key: MessageKey, vars?: Record<string, string | number>) => string;

/**
 * The quoted message, or a stated absence when it is on a page not read yet. Either way it is a
 * button that jumps to the message.
 */
function QuotedMessage({
  quoted,
  t,
  onJump,
}: {
  quoted: ChatItem | undefined;
  t: Translate;
  onJump: () => void;
}) {
  const contentId = useId();
  return (
    <blockquote className="chat-quote">
      <button
        type="button"
        className="chat-quote__jump"
        aria-label={t('history.chat.quote.jump')}
        aria-describedby={contentId}
        onClick={onJump}
      >
        <span id={contentId} className="chat-quote__content">
          {quoted && isChatMessage(quoted) ? (
            <>
              <b>{t(speakerKey(quoted))}</b>
              <span className="chat-quote__text">{quoted.content.text}</span>
            </>
          ) : (
            <span className="chat-quote__text">{t('history.chat.quoteUnavailable')}</span>
          )}
        </span>
      </button>
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
  onQuote,
  onJumpToQuote,
  found,
  retry,
}: {
  item: ChatMessageItem;
  quoted: ChatItem | undefined;
  time: string;
  latestCards: ReadonlyMap<WorkKey, string>;
  works: ReadonlyMap<WorkKey, ChatWorkState>;
  openWork: WorkKey | null;
  t: Translate;
  onOpenCard: (card: ChatCardData) => void;
  /** Starts a reply that quotes this message; null while the composer cannot take one. */
  onQuote: (() => void) | null;
  /** Takes the chat to the message this one quotes. */
  onJumpToQuote: (quoteItemId: string) => void;
  /** The chat was just taken to this message; it is marked so the eye finds it. */
  found: boolean;
  /** Runs the failed turn this message started again; its label names why there is no reply. */
  retry: { label: string; disabled: boolean; onRetry: () => void } | null;
}) {
  const { language } = useI18n();
  const { content } = item;
  const mine = content.kind === 'user_message';
  const quoteItemId = content.quote_item_id;
  const copy = USER_MESSAGE_COPY[language];
  return (
    <article
      id={messageElementId(item.item_id)}
      tabIndex={-1}
      className={['chat-row', mine && 'chat-row--mine', found && 'chat-row--found']
        .filter(Boolean)
        .join(' ')}
      aria-label={t(speakerKey(item))}
    >
      <div className="chat-stack">
        <div className="chat-bubble">
          {quoteItemId !== null ? (
            <QuotedMessage quoted={quoted} t={t} onJump={() => onJumpToQuote(quoteItemId)} />
          ) : null}
          {mine ? (
            <p className="chat-bubble__text">
              <ProjectRefText text={content.text} refs={content.project_refs} />
            </p>
          ) : (
            <MarkdownBlock text={content.text} isStreamFinished />
          )}
        </div>
        {content.kind === 'user_message' && content.images.length > 0 ? (
          <AttachedImages images={content.images} copy={copy.userImages} />
        ) : null}
        {content.kind === 'user_message' && content.files.length > 0 ? (
          <ul
            className="action-conversation__attachments"
            aria-label={copy.userFiles(content.files.length)}
          >
            {content.files.map((file, index) => (
              // A message may carry two files with the same name; the order is stable.
              <li key={index}>
                <AttachedFileChip name={file.name} byteSize={file.byte_size} />
              </li>
            ))}
          </ul>
        ) : null}
        {retry ? (
          <button
            type="button"
            className="chat-retry"
            aria-label={retry.label}
            title={retry.label}
            disabled={retry.disabled}
            onClick={retry.onRetry}
          >
            <RotateCcw size={12} aria-hidden="true" />
          </button>
        ) : null}
        {content.kind === 'assistant_message'
          ? content.cards.map((card, index) => {
              const work = cardWorkKey(card);
              return (
                <ChatCard
                  key={cardPosition(item.item_id, index)}
                  id={cardElementId(cardPosition(item.item_id, index))}
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
      {onQuote ? (
        <button
          type="button"
          className="chat-reply"
          aria-label={t('history.chat.quote.reply')}
          title={t('history.chat.quote.reply')}
          onClick={onQuote}
        >
          <Reply size={15} aria-hidden="true" />
        </button>
      ) : null}
    </article>
  );
}
