import { ArrowUp, Paperclip } from 'lucide-react';
import { useRef, useState } from 'react';

import { ComposerAttachments } from '@/components/agent-overlay/ComposerAttachments';
import { ATTACHMENT_ACCEPT } from '@/components/agent-overlay/attachmentStaging';
import { isImeKey } from '@/components/ui/imeKey';
import { useI18n } from '@/context/useI18n';
import type { TodoImportAttachments } from '@/todos/useTodoImport';

import { useImportBarFiles } from './useImportBarFiles';

import './todo.css';
import './todoImportBar.css';

/**
 * The bar under the list that hands text of any length and documents and images attached,
 * dropped or pasted to Pantaray, which drafts TODOs from them. Enter sends; Shift+Enter starts a
 * new line and an IME's Enter is the IME's. The text and files empty once the import started.
 */
export function TodoImportBar({
  disabled,
  onImport,
}: {
  /** The list on screen is not the one selected yet, so nothing is imported into it. */
  disabled: boolean;
  /** Resolves true once the import started, which empties the bar. */
  onImport: (text: string, attachments: TodoImportAttachments) => Promise<boolean>;
}) {
  const { t } = useI18n();
  const fieldRef = useRef<HTMLTextAreaElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const files = useImportBarFiles();
  const [text, setText] = useState('');
  // The text as last typed, which a send reads once its answer comes.
  const textRef = useRef('');
  const changeText = (next: string) => {
    textRef.current = next;
    setText(next);
  };
  const [sending, setSending] = useState(false);
  const filled = text.trim() !== '' || files.attachments.length > 0;
  const canSend = !sending && !files.busy && filled;

  // Files added while a send is answering would miss it and then be cleared with the sent ones.
  const attach = (picked: readonly File[]) => {
    if (disabled || sending || picked.length === 0) return;
    void files.attach(picked);
  };

  const send = async () => {
    if (!canSend) return;
    setSending(true);
    const sent = text;
    // Text typed while the send was answering is the next one, so only the sent text is cleared.
    if (await onImport(sent, files.payload)) {
      if (textRef.current === sent) changeText('');
      files.clearSent();
    }
    setSending(false);
  };

  return (
    <fieldset
      className="todo-import-bar"
      disabled={disabled}
      onDragOver={(event) => event.preventDefault()}
      onDrop={(event) => {
        event.preventDefault();
        attach(Array.from(event.dataTransfer.files));
      }}
    >
      <ComposerAttachments
        attachments={files.attachments}
        failure={files.failure}
        readOnly={sending}
        focusAfterLastRemoved={fieldRef}
        onRemove={files.remove}
      />
      <textarea
        ref={fieldRef}
        rows={1}
        className="todo-import-bar__field"
        aria-label={t('todo.add.importLabel')}
        placeholder={t('todo.add.importPlaceholder')}
        value={text}
        onChange={(event) => changeText(event.target.value)}
        onKeyDown={(event) => {
          if (event.key !== 'Enter' || event.shiftKey || isImeKey(event)) return;
          event.preventDefault();
          void send();
        }}
        onPaste={(event) => {
          const pasted = Array.from(event.clipboardData.files);
          if (pasted.length === 0) return;
          event.preventDefault();
          attach(pasted);
        }}
      />
      <div className="todo-import-bar__choices">
        <input
          ref={fileInputRef}
          type="file"
          hidden
          multiple
          accept={ATTACHMENT_ACCEPT}
          onChange={(event) => {
            const picked = Array.from(event.target.files ?? []);
            // Reset so picking the same file again still fires a change event.
            event.target.value = '';
            attach(picked);
          }}
        />
        <button
          type="button"
          className="todo-import-bar__icon"
          aria-label={t('todo.add.attach')}
          data-tooltip={t('todo.add.attach')}
          disabled={sending}
          onClick={() => fileInputRef.current?.click()}
        >
          <Paperclip size={15} strokeWidth={1.8} aria-hidden="true" />
        </button>
        <button
          type="button"
          className="todo-import-bar__send"
          aria-label={t('todo.add.import')}
          data-tooltip={t('todo.add.import')}
          disabled={!canSend}
          onClick={() => void send()}
        >
          <ArrowUp size={15} strokeWidth={2.25} aria-hidden="true" />
        </button>
      </div>
    </fieldset>
  );
}
