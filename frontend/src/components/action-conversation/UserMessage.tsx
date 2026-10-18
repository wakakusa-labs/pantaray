import { ArrowUp, X } from 'lucide-react';
import { useContext, useState } from 'react';

import type { ActionConversationUserItem } from '../../../electron/src/actions/actionConversationModel';
import { useI18n } from '@/context/useI18n';
import { AttachedFileChip } from './AttachedFileChip';
import { AttachedImages } from './AttachedImages';
import { CopyTextButton } from './CopyTextButton';
import { QuotedMessageText } from './QuotedMessageText';
import { OpenWorkContext, sentMessageRefs } from './sentMessageRefs';
import type { SendNow } from './useUnadoptedMessages';
import { USER_MESSAGE_COPY as COPY } from './userMessageCopy';

/** No confirmation: the message has not run, so withdrawing it undoes nothing. */
function WithdrawButton({ label, onWithdraw }: { label: string; onWithdraw: () => Promise<void> }) {
  const [busy, setBusy] = useState(false);
  return (
    <button
      type="button"
      className="action-conversation__withdraw"
      aria-label={label}
      data-tooltip={label}
      aria-disabled={busy}
      onClick={() => {
        if (busy) return;
        setBusy(true);
        // A failed withdrawal leaves the message where it is, ready to try again.
        onWithdraw()
          .catch((error: unknown) => console.error('Failed to withdraw the message:', error))
          .finally(() => setBusy(false));
      }}
    >
      <X size={14} strokeWidth={1.75} aria-hidden />
    </button>
  );
}

/** Pressing it again while it runs does nothing: one Send now covers every waiting message. */
function SendNowButton({
  label,
  failedLabel,
  sendNow,
}: {
  label: string;
  failedLabel: string;
  sendNow: SendNow;
}) {
  const failed = sendNow.state === 'failed' ? failedLabel : null;
  return (
    <>
      <button
        type="button"
        className="action-conversation__send-now"
        aria-label={label}
        data-tooltip={failed ?? label}
        aria-disabled={sendNow.state === 'sending'}
        onClick={sendNow.send}
      >
        <ArrowUp size={14} strokeWidth={1.75} aria-hidden />
      </button>
      {failed ? (
        <span className="action-conversation__sr-only" role="alert">
          {failed}
        </span>
      ) : null}
    </>
  );
}

export function UserItem({
  item,
  onWithdraw,
  sendNow = null,
}: {
  item: ActionConversationUserItem;
  /** Offered only where the message can be withdrawn: a canonical one no run has taken. */
  onWithdraw?: (stepId: string) => Promise<void>;
  /** Offered on a message waiting for the running run, while that run can be stopped. */
  sendNow?: SendNow | null;
}) {
  const { language, t } = useI18n();
  const openWork = useContext(OpenWorkContext);
  const copy = COPY[language];
  const message = item.source === 'canonical' ? item.entry : item.submission.request.message;
  const { content, images } = message;
  const files = message.files ?? [];
  const status = item.source === 'canonical' ? item.entry.status : item.submission.state;
  const refs = sentMessageRefs(message.project_refs ?? [], message.work_refs ?? [], t, openWork);
  const state =
    status !== 'adopted' ? (
      <span
        className="action-conversation__state"
        role={item.source === 'optimistic' ? 'status' : undefined}
      >
        {copy.userStatus[status]}
      </span>
    ) : null;
  // Under the message, at its right edge, as the copy button sits under an answer.
  const copyButton =
    content !== null ? (
      <CopyTextButton
        className="action-conversation__user-copy"
        text={content}
        label={t('overlay.copyThisMessage')}
        failedLabel={t('overlay.copyThisMessageFailed')}
      />
    ) : null;
  const withdrawButton =
    item.source === 'canonical' && status !== 'adopted' && onWithdraw ? (
      <WithdrawButton label={copy.withdraw} onWithdraw={() => onWithdraw(item.entry.step_id)} />
    ) : null;
  const sendNowButton =
    item.source === 'canonical' && status === 'pending' && sendNow ? (
      <SendNowButton
        label={copy.sendNow}
        failedLabel={t('overlay.composer.stopFailed')}
        sendNow={sendNow}
      />
    ) : null;
  const actions =
    copyButton || withdrawButton || sendNowButton ? (
      <div className="action-conversation__user-actions">
        {copyButton}
        {sendNowButton}
        {withdrawButton}
      </div>
    ) : null;

  // What Pantaray's chat wrote when it handed this over: Pantaray's, never a user bubble.
  const chatNote = item.source === 'canonical' ? item.entry.chat_note : null;
  const hasUserPart = content !== null || images.length > 0 || files.length > 0;
  const note =
    chatNote !== null ? (
      <section className="action-conversation__chat-note" aria-label={copy.chatNote}>
        <span className="action-conversation__chat-note-label" aria-hidden="true">
          {copy.chatNote}
        </span>
        <p>{chatNote}</p>
        {hasUserPart ? null : state}
      </section>
    ) : null;
  if (!hasUserPart) {
    return (
      <>
        {note ?? state}
        {actions}
      </>
    );
  }
  return (
    <>
      <div className="action-conversation__user-turn">
        <article className="action-conversation__user" aria-label={copy.you}>
          {content !== null ? <QuotedMessageText text={content} refs={refs} /> : null}
          {images.length > 0 ? <AttachedImages images={images} copy={copy.userImages} /> : null}
          {files.length > 0 ? (
            <ul
              className="action-conversation__attachments"
              aria-label={copy.userFiles(files.length)}
            >
              {files.map((file, index) => (
                // A message may carry two files with the same name; the order is stable.
                <li key={index}>
                  <AttachedFileChip name={file.name} byteSize={file.byte_size} />
                </li>
              ))}
            </ul>
          ) : null}
          {state}
        </article>
        {actions}
      </div>
      {note}
    </>
  );
}
