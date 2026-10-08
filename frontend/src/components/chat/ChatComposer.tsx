import { ArrowUp, Plus, X } from 'lucide-react';
import { useRef, type RefObject } from 'react';

import { ComposerAlert, ComposerAttachments } from '@/components/agent-overlay/ComposerAttachments';
import { ATTACHMENT_ACCEPT } from '@/components/agent-overlay/attachmentStaging';
import type { MessageKey } from '@/i18n/types';

import { speakerKey } from './chatTimeline';
import type { ChatSendProblem, useChatComposer } from './useChatComposer';

const MESSAGE_FIELD_ID = 'chat-composer-message';
const PROBLEM_ID = 'chat-composer-problem';

const PROBLEM_KEYS: Record<ChatSendProblem, MessageKey> = {
  text: 'overlay.composer.invalid',
  body: 'history.chat.composer.failed',
  transport: 'history.chat.composer.failed',
  quote_item_id: 'history.chat.composer.quoteRejected',
  images: 'history.chat.composer.attachmentsRejected',
  files: 'history.chat.composer.attachmentsRejected',
};

/**
 * Where the user writes to Pantaray: the quoted message, the attachments, the text and the send
 * button, docked under the chat. Enter sends and Shift+Enter starts a new line, as in the Overlay.
 */
export function ChatComposer({
  composer,
  textareaRef,
  t,
}: {
  composer: ReturnType<typeof useChatComposer>;
  textareaRef: RefObject<HTMLTextAreaElement>;
  t: (key: MessageKey, vars?: Record<string, string | number>) => string;
}) {
  const fileInputRef = useRef<HTMLInputElement>(null);
  const { state } = composer;
  const readOnly = state.pending !== null;
  return (
    <form
      className="chat-dock"
      aria-label={t('history.chat.composer.label')}
      onSubmit={(event) => {
        event.preventDefault();
        composer.send();
      }}
      onDragOver={(event) => event.preventDefault()}
      onDrop={(event) => {
        event.preventDefault();
        void composer.attachFiles(Array.from(event.dataTransfer.files));
      }}
    >
      {state.quote ? (
        <div className="chat-quote-chip">
          <span>
            <b>{t(speakerKey(state.quote))}</b>
            {state.quote.content.text}
          </span>
          {readOnly ? null : (
            <button
              type="button"
              className="chat-icon-button"
              aria-label={t('history.chat.quote.clear')}
              title={t('history.chat.quote.clear')}
              onClick={() => {
                composer.setQuote(null);
                textareaRef.current?.focus();
              }}
            >
              <X size={14} aria-hidden="true" />
            </button>
          )}
        </div>
      ) : null}
      <ComposerAttachments
        attachments={state.attachments}
        failure={state.attachmentFailure}
        readOnly={readOnly}
        focusAfterLastRemoved={textareaRef}
        onRemove={composer.removeAttachment}
      />
      <div className="chat-composer">
        <input
          ref={fileInputRef}
          type="file"
          hidden
          multiple
          accept={ATTACHMENT_ACCEPT}
          onChange={(event) => {
            const files = Array.from(event.target.files ?? []);
            // Reset so picking the same file again still fires a change event.
            event.target.value = '';
            void composer.attachFiles(files);
          }}
        />
        <button
          type="button"
          className="chat-icon-button"
          disabled={!composer.canAttach}
          aria-label={t('overlay.composer.attach')}
          title={t('overlay.composer.attach')}
          onClick={() => fileInputRef.current?.click()}
        >
          <Plus size={18} strokeWidth={1.75} aria-hidden="true" />
        </button>
        <label className="chat-sr-only" htmlFor={MESSAGE_FIELD_ID}>
          {t('overlay.composer.label')}
        </label>
        <textarea
          ref={textareaRef}
          id={MESSAGE_FIELD_ID}
          className="chat-composer__input"
          rows={1}
          value={state.draft}
          readOnly={readOnly}
          placeholder={t('overlay.composer.placeholder')}
          aria-invalid={state.problem === 'text' || undefined}
          aria-describedby={state.problem ? PROBLEM_ID : undefined}
          onChange={(event) => composer.setDraft(event.target.value)}
          onKeyDown={(event) => {
            if (event.key !== 'Enter' || event.shiftKey || event.nativeEvent.isComposing) return;
            event.preventDefault();
            if (composer.canSend) composer.send();
          }}
          onPaste={(event) => {
            const files = Array.from(event.clipboardData.files);
            if (readOnly || files.length === 0) return;
            event.preventDefault();
            void composer.attachFiles(files);
          }}
        />
        <button
          type="submit"
          className="chat-send"
          disabled={!composer.canSend}
          aria-label={t('overlay.composer.send')}
          title={t('overlay.composer.send')}
        >
          <ArrowUp size={16} strokeWidth={2.25} aria-hidden="true" />
        </button>
      </div>
      {state.pending?.state === 'sending' ? (
        <span className="chat-composer__status" role="status">
          {t('overlay.composer.sending')}
        </span>
      ) : null}
      {state.problem ? (
        <div className="chat-composer__status">
          <ComposerAlert id={PROBLEM_ID} role="alert">
            {t(PROBLEM_KEYS[state.problem])}
          </ComposerAlert>
          {state.pending?.state === 'failed' ? (
            <button type="button" className="history-filter-button" onClick={composer.retry}>
              {t('overlay.composer.retry')}
            </button>
          ) : null}
        </div>
      ) : null}
    </form>
  );
}
