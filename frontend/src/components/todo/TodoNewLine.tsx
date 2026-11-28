import { useRef, useState } from 'react';

import { isImeKey } from '@/components/ui/imeKey';
import { useI18n } from '@/context/useI18n';
import { oneLine } from '@/todos/todoText';

import type { TodoDue } from '../../../electron/src/todos/todoContracts';
import { TodoDateRangePicker } from './TodoDateRangePicker';
import { dueDay } from './todoDates';

import './todo.css';

/**
 * The empty line after a list's rows, where a TODO is written as on a note: Enter adds the line
 * as a TODO, with the due day picked on the line, and leaves the focus here for the next one,
 * which has no due day. Not an IME's Enter, nor Shift+Enter, and not a blank line. Escape clears
 * the line and leaves it. A TODO is one line, so pasted lines are joined. It goes in a list after
 * the `TodoRow`s, its due day where theirs is.
 */
export function TodoNewLine({
  id,
  placeName,
  today,
  onCreate,
}: {
  /** The field's id, by which the list moves the focus here. */
  id: string;
  /** Where the TODO goes, which names the line for assistive tech. */
  placeName: string;
  /** The backend's today: the line's TODO starts on it, so its due day is not before it. */
  today: string;
  /** Resolves true once the TODO has been read into the list. */
  onCreate: (text: string, due: TodoDue | null) => Promise<boolean>;
}) {
  const { t, language } = useI18n();
  const [text, setText] = useState('');
  // The text as last typed, which a send reads once its answer comes.
  const textRef = useRef('');
  const changeText = (next: string) => {
    textRef.current = next;
    setText(next);
  };
  const [due, setDue] = useState<string | null>(null);
  // The due day as last picked, which a send reads once its answer comes, as the text.
  const dueRef = useRef<string | null>(null);
  const changeDue = (next: string | null) => {
    dueRef.current = next;
    setDue(next);
  };
  const sendingRef = useRef(false);

  const send = async () => {
    const sent = textRef.current;
    const sentDue = dueRef.current;
    if (sendingRef.current || sent.trim() === '') return;
    sendingRef.current = true;
    try {
      // A refused or unanswered add keeps its text and due day, to send again.
      const added = await onCreate(
        sent.trim(),
        sentDue === null ? null : { date: sentDue, repeat: 'none' }
      );
      if (!added) return;
      // What was typed while the add was answering is the next line's, so only the sent text goes,
      // and a due day picked meanwhile stays with it.
      const current = textRef.current;
      if (current.startsWith(sent)) changeText(current.slice(sent.length).trimStart());
      if (dueRef.current === sentDue) changeDue(null);
    } finally {
      sendingRef.current = false;
    }
  };

  const lineName = t('todo.add.to', { place: placeName });
  const dueText = due === null ? t('todo.add.noDue') : dueDay(language, due);

  return (
    <li className="todo-row todo-new-line">
      <span className="todo-new-line__check" aria-hidden="true" />
      <input
        id={id}
        type="text"
        className="todo-new-line__field"
        aria-label={lineName}
        placeholder={t('todo.add.placeholder')}
        value={text}
        onChange={(event) => changeText(event.target.value)}
        onKeyDown={(event) => {
          // Keys an IME takes while composing, its Escape among them, are the IME's.
          if (isImeKey(event)) return;
          if (event.key === 'Escape') {
            changeText('');
            changeDue(null);
            event.currentTarget.blur();
            return;
          }
          if (event.key !== 'Enter' || event.shiftKey) return;
          event.preventDefault();
          void send();
        }}
        onPaste={(event) => {
          // A text field drops line breaks with nothing between the lines, so they are joined here.
          const pasted = event.clipboardData.getData('text');
          if (!pasted.includes('\n')) return;
          event.preventDefault();
          const field = event.currentTarget;
          field.setRangeText(
            oneLine(pasted),
            field.selectionStart ?? field.value.length,
            field.selectionEnd ?? field.value.length,
            'end'
          );
          changeText(field.value);
        }}
      />
      {/* As a row's due day, but the line's TODO starts today, so only the due day is picked. */}
      <TodoDateRangePicker
        triggerLabel={
          due === null
            ? t('todo.row.noDue', { text: lineName })
            : t('todo.row.due', { date: dueText, text: lineName })
        }
        triggerClassName={
          due === null
            ? 'todo-row__date todo-row__due todo-row__due--none'
            : 'todo-row__date todo-row__due'
        }
        dates={{ start: today, due }}
        today={today}
        opensOn="due"
        startFixed
        onChange={(next) => changeDue(next.due)}
      >
        {dueText}
      </TodoDateRangePicker>
    </li>
  );
}
