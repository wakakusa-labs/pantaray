import { ArrowUp, Paperclip } from 'lucide-react';
import { useRef, useState } from 'react';

import { ComposerAttachments } from '@/components/agent-overlay/ComposerAttachments';
import { ATTACHMENT_ACCEPT } from '@/components/agent-overlay/attachmentStaging';
import { isImeKey } from '@/components/ui/imeKey';
import { useI18n } from '@/context/useI18n';
import type { TodoProjectSlot } from '@/todos/groupTodosByProject';
import type { TodoImportAttachments } from '@/todos/useTodoImport';

import type { TodoDue } from '../../../electron/src/todos/todoContracts';
import { TodoDatePicker } from './TodoDatePicker';
import { dueDay } from './todoDates';
import { useAddBarFiles } from './useAddBarFiles';

import './todo.css';
import './todoAddBar.css';

/**
 * The bar under the list that adds a TODO: its text, its project (while several are in view),
 * due day (none at first) and start (today at first, or none), and send. An IME's Enter does not
 * send; Shift+Enter starts a new line. Text of several lines, or documents and images attached
 * with the paperclip, dropped or pasted on the bar, are imported instead: Pantaray drafts TODOs from them, so the
 * project and dates, which belong to one TODO, step aside. The text empties once it was taken;
 * the choices stay for the next one.
 */
export function TodoAddBar({
  today,
  projects,
  disabled,
  onSubmit,
  onImport,
}: {
  /** The backend's today. */
  today: string;
  /** Not empty: a scope with no project takes no TODO. */
  projects: readonly TodoProjectSlot[];
  /** The list on screen is not the one selected yet, so nothing is added to it. */
  disabled: boolean;
  onSubmit: (
    projectId: string | null,
    startsOn: string | null,
    text: string,
    due: TodoDue | null
  ) => Promise<boolean>;
  /** Resolves true once the import started, which empties the bar. */
  onImport: (text: string, attachments: TodoImportAttachments) => Promise<boolean>;
}) {
  const { t, language } = useI18n();
  const fieldRef = useRef<HTMLTextAreaElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const files = useAddBarFiles();
  const [text, setText] = useState('');
  const [sending, setSending] = useState(false);
  const [chosenProject, setChosenProject] = useState(projects[0].projectId ?? '');
  const [due, setDue] = useState<string | null>(null);
  // 'today' is whichever day that is when the TODO is sent; a null date is no start.
  const [start, setStart] = useState<'today' | { date: string | null }>('today');
  // A project gone from view falls back to the first one.
  const project = projects.find((slot) => (slot.projectId ?? '') === chosenProject) ?? projects[0];
  const startsOn = start === 'today' ? today : start.date;
  const importing = files.attachments.length > 0 || text.trim().includes('\n');
  const canSend = !sending && !files.busy && (text.trim() !== '' || files.attachments.length > 0);

  // Files added while a send is answering would miss it and then be cleared with the sent ones.
  const attach = (picked: readonly File[]) => {
    if (!disabled && !sending) void files.attach(picked);
  };

  const send = async () => {
    if (!canSend) return;
    setSending(true);
    const sent = text;
    const taken = importing
      ? await onImport(sent, files.payload)
      : // A line ended with Shift+Enter is still one line.
        await onSubmit(
          project.projectId,
          startsOn,
          sent.trim(),
          due === null ? null : { date: due, repeat: 'none' }
        );
    // Text typed while the add was deciding is the next TODO, so only the sent text is cleared.
    if (taken) {
      setText((current) => (current === sent ? '' : current));
      if (importing) files.clearSent();
    }
    setSending(false);
  };

  /** One TODO's project and dates, which an import leaves to the draft. */
  const choices = (
    <>
      {projects.length > 1 ? (
        <span className="todo-add-bar__project">
          <span className="todo-group__dot" aria-hidden="true" />
          <select
            aria-label={t('todo.add.project')}
            value={project.projectId ?? ''}
            onChange={(event) => setChosenProject(event.target.value)}
          >
            {projects.map((slot) => (
              <option key={slot.projectId ?? ''} value={slot.projectId ?? ''}>
                {slot.projectName ?? t('todo.noProject')}
              </option>
            ))}
          </select>
        </span>
      ) : null}
      <TodoDatePicker
        label={t('todo.date.due')}
        triggerClassName="todo-add-bar__chip"
        value={due}
        today={today}
        clearLabel={t('todo.date.clearDue')}
        onChoose={setDue}
      >
        {due ? t('todo.add.due', { date: dueDay(language, due) }) : t('todo.add.noDue')}
      </TodoDatePicker>
      <TodoDatePicker
        label={t('todo.date.start')}
        triggerClassName="todo-add-bar__chip"
        value={startsOn}
        today={today}
        clearLabel={t('todo.date.unsetStart')}
        onChoose={(date) => setStart(date === today ? 'today' : { date })}
      >
        {startsOn === null
          ? t('todo.add.startsUnset')
          : startsOn === today
            ? t('todo.add.startsToday')
            : t('todo.add.startsOn', { date: dueDay(language, startsOn) })}
      </TodoDatePicker>
    </>
  );

  return (
    <fieldset
      className="todo-add-bar"
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
        className="todo-add-bar__field"
        aria-label={t('todo.add')}
        placeholder={t('todo.add.placeholder')}
        value={text}
        onChange={(event) => setText(event.target.value)}
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
      <div className="todo-add-bar__choices">
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
          className="todo-add-bar__chip todo-add-bar__attach"
          disabled={sending}
          onClick={() => fileInputRef.current?.click()}
        >
          <Paperclip size={13} strokeWidth={1.8} aria-hidden="true" />
          {t('todo.add.attach')}
        </button>
        {importing ? null : choices}
        <button
          type="button"
          className="todo-add-bar__send"
          aria-label={t(importing ? 'todo.add.import' : 'todo.add.send')}
          data-tooltip={t(importing ? 'todo.add.import' : 'todo.add.send')}
          disabled={!canSend}
          onClick={() => void send()}
        >
          <ArrowUp size={15} strokeWidth={2.25} aria-hidden="true" />
        </button>
      </div>
    </fieldset>
  );
}
