import { useRef, useState } from 'react';

import { isImeKey } from '@/components/ui/imeKey';
import { useI18n } from '@/context/useI18n';
import { oneLine } from '@/todos/todoText';

import './todo.css';

/**
 * The empty line after a list's rows, where a TODO is written as on a note: Enter adds the line
 * as a TODO and leaves the focus here for the next one. Not an IME's Enter, nor Shift+Enter, and
 * not a blank line. Escape clears the line and leaves it. A TODO is one line, so pasted lines are
 * joined. It goes in a list after the `TodoRow`s.
 */
export function TodoNewLine({
  placeName,
  onCreate,
}: {
  /** Where the TODO goes, which names the line for assistive tech. */
  placeName: string;
  /** Resolves true once the TODO has been read into the list. */
  onCreate: (text: string) => Promise<boolean>;
}) {
  const { t } = useI18n();
  const [text, setText] = useState('');
  // The text as last typed, which a send reads once its answer comes.
  const textRef = useRef('');
  const changeText = (next: string) => {
    textRef.current = next;
    setText(next);
  };
  const sendingRef = useRef(false);

  const send = async () => {
    const sent = textRef.current;
    if (sendingRef.current || sent.trim() === '') return;
    sendingRef.current = true;
    try {
      // A refused or unanswered add keeps its text, to send again.
      if (!(await onCreate(sent.trim()))) return;
      // What was typed while the add was answering is the next line's, so only the sent text goes.
      const current = textRef.current;
      if (current.startsWith(sent)) changeText(current.slice(sent.length).trimStart());
    } finally {
      sendingRef.current = false;
    }
  };

  return (
    <li className="todo-row todo-new-line">
      <span className="todo-new-line__check" aria-hidden="true" />
      <input
        type="text"
        className="todo-new-line__field"
        aria-label={t('todo.add.to', { place: placeName })}
        placeholder={t('todo.add.placeholder')}
        value={text}
        onChange={(event) => changeText(event.target.value)}
        onKeyDown={(event) => {
          if (event.key === 'Escape') {
            changeText('');
            event.currentTarget.blur();
            return;
          }
          if (event.key !== 'Enter' || event.shiftKey || isImeKey(event)) return;
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
    </li>
  );
}
