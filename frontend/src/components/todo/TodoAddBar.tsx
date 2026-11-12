import { ArrowUp, CalendarDays, FileInput, Paperclip } from 'lucide-react';
import { useRef, useState } from 'react';

import { ComposerAlert, ComposerAttachments } from '@/components/agent-overlay/ComposerAttachments';
import { ATTACHMENT_ACCEPT } from '@/components/agent-overlay/attachmentStaging';
import { isImeKey } from '@/components/ui/imeKey';
import { useI18n } from '@/context/useI18n';
import type { TodoProjectSlot } from '@/todos/groupTodosByProject';
import type { TodoImportAttachments } from '@/todos/useTodoImport';

import type { TodoDue } from '../../../electron/src/todos/todoContracts';
import { TodoDateRangePicker } from './TodoDateRangePicker';
import { compactDates, datesName } from './todoDates';
import { useAddBarFiles } from './useAddBarFiles';

import './todo.css';
import './todoAddBar.css';

/** A TODO is one line, so lines pasted, or kept from an import, are joined. */
const oneLine = (text: string) => text.replace(/[^\S\n]*\n\s*/g, ' ');

/**
 * The bar under the list, in one of two modes that the toggle at its left end switches. Adding
 * takes the text as one TODO, on one line, with its project (while several are in view), start
 * (today at first, or none) and due day (none at first). Importing hands text of any length and
 * documents and images attached, dropped or pasted to Pantaray, which drafts TODOs from them; a
 * file dropped or pasted while adding turns importing on. An IME's Enter does not send. The text
 * empties once it was taken, and a started import turns back to adding; the choices stay.
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
  /** Empty in a scope with no project yet, which takes imports but no single TODO. */
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
  const [importing, setImporting] = useState(false);
  const [text, setText] = useState('');
  // The text as last typed, which a send reads once its answer comes.
  const textRef = useRef('');
  const changeText = (next: string) => {
    textRef.current = next;
    setText(next);
  };
  const [sending, setSending] = useState(false);
  const [chosenProject, setChosenProject] = useState(projects[0]?.projectId ?? '');
  // A TODO was sent where no project can take it.
  const [needsProject, setNeedsProject] = useState(false);
  const [due, setDue] = useState<string | null>(null);
  // 'today' is whichever day that is when the TODO is sent; a null date is no start.
  const [start, setStart] = useState<'today' | { date: string | null }>('today');
  // A project gone from view falls back to the first one.
  const project: TodoProjectSlot | undefined =
    projects.find((slot) => (slot.projectId ?? '') === chosenProject) ?? projects[0];
  const startsOn = start === 'today' ? today : start.date;
  const dates = { start: startsOn, due };
  const filled = text.trim() !== '' || (importing && files.attachments.length > 0);
  const canSend = !sending && !files.busy && filled;
  const sendName = t(importing ? 'todo.add.import' : 'todo.add.send');

  // Files added while a send is answering would miss it and then be cleared with the sent ones.
  const attach = (picked: readonly File[]) => {
    if (disabled || sending || picked.length === 0) return;
    setImporting(true);
    setNeedsProject(false);
    void files.attach(picked);
  };

  const switchMode = () => {
    // Files staged for an import are nobody's once it is off.
    if (importing) {
      files.discardAll();
      changeText(oneLine(textRef.current));
    }
    setImporting(!importing);
    setNeedsProject(false);
  };

  const send = async () => {
    if (!canSend) return;
    if (!importing && project === undefined) {
      setNeedsProject(true);
      return;
    }
    setSending(true);
    const sent = text;
    const taken = importing
      ? await onImport(sent, files.payload)
      : await onSubmit(
          project?.projectId ?? null,
          startsOn,
          sent.trim(),
          due === null ? null : { date: due, repeat: 'none' }
        );
    // Text typed while the send was answering is the next one, so only the sent text is cleared,
    // and an import stays on for newer text, which may have lines no TODO can take.
    if (taken) {
      const cleared = textRef.current === sent;
      if (cleared) changeText('');
      if (importing) {
        files.clearSent();
        if (cleared) setImporting(false);
      }
    }
    setSending(false);
  };

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
      {importing ? (
        <ComposerAttachments
          attachments={files.attachments}
          failure={files.failure}
          readOnly={sending}
          focusAfterLastRemoved={fieldRef}
          onRemove={files.remove}
        />
      ) : null}
      {needsProject ? (
        <ComposerAlert role="alert">{t('todo.add.needsProject')}</ComposerAlert>
      ) : null}
      <textarea
        ref={fieldRef}
        rows={1}
        className="todo-add-bar__field"
        aria-label={t('todo.add')}
        placeholder={t(importing ? 'todo.add.importPlaceholder' : 'todo.add.placeholder')}
        value={text}
        onChange={(event) => {
          const value = event.target.value;
          changeText(importing ? value : oneLine(value));
          setNeedsProject(false);
        }}
        onKeyDown={(event) => {
          if (event.key !== 'Enter' || isImeKey(event)) return;
          // Shift+Enter starts a new line of an import; a TODO has one line.
          if (importing && event.shiftKey) return;
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
        <button
          type="button"
          className="todo-add-bar__icon todo-add-bar__mode"
          aria-label={t('todo.add.importMode')}
          data-tooltip={t('todo.add.importMode')}
          aria-pressed={importing}
          disabled={sending}
          onClick={switchMode}
        >
          <FileInput size={15} strokeWidth={1.8} aria-hidden="true" />
        </button>
        {importing ? (
          <>
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
              className="todo-add-bar__icon"
              aria-label={t('todo.add.attach')}
              data-tooltip={t('todo.add.attach')}
              disabled={sending}
              onClick={() => fileInputRef.current?.click()}
            >
              <Paperclip size={15} strokeWidth={1.8} aria-hidden="true" />
            </button>
          </>
        ) : (
          <>
            {projects.length > 1 ? (
              <span className="todo-add-bar__project">
                <span className="todo-group__dot" aria-hidden="true" />
                <select
                  aria-label={t('todo.add.project')}
                  value={project?.projectId ?? ''}
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
            {/* The icon alone stands for the first dates; others show beside it, start first. */}
            <TodoDateRangePicker
              triggerLabel={datesName(t, language, today, dates)}
              tooltip
              triggerClassName="todo-add-bar__icon todo-add-bar__dates"
              dates={dates}
              today={today}
              opensOn="start"
              onChange={(next) => {
                setStart(next.start === today ? 'today' : { date: next.start });
                setDue(next.due);
              }}
            >
              <CalendarDays size={15} strokeWidth={1.8} aria-hidden="true" />
              {compactDates(t, today, dates)}
            </TodoDateRangePicker>
          </>
        )}
        <button
          type="button"
          className="todo-add-bar__send"
          aria-label={sendName}
          data-tooltip={sendName}
          disabled={!canSend}
          onClick={() => void send()}
        >
          <ArrowUp size={15} strokeWidth={2.25} aria-hidden="true" />
        </button>
      </div>
    </fieldset>
  );
}
