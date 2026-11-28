import { useRef, useState } from 'react';

import { isImeKey } from '@/components/ui/imeKey';
import { useI18n } from '@/context/useI18n';

import './todo.css';

/**
 * The empty heading after an organization's last group, where a project is named as a heading is
 * written on a note: Enter creates it, and the line empties once it is made. Not an IME's Enter,
 * nor Shift+Enter, and not a blank name. Escape clears the line and leaves it. A refused name
 * stays on the line with the reason under it.
 */
export function TodoProjectLine({
  failure,
  onCreate,
}: {
  /** Why the last name sent from this line was refused, if it was. */
  failure: string | null;
  /** Resolves true once the project is made. */
  onCreate: (name: string) => Promise<boolean>;
}) {
  const { t } = useI18n();
  const [name, setName] = useState('');
  // The name as last typed, which a send reads once its answer comes.
  const nameRef = useRef('');
  const changeName = (next: string) => {
    nameRef.current = next;
    setName(next);
  };
  const sendingRef = useRef(false);
  const [refused, setRefused] = useState(false);

  const send = async () => {
    const sent = nameRef.current;
    if (sendingRef.current || sent.trim() === '') return;
    sendingRef.current = true;
    setRefused(false);
    try {
      if (!(await onCreate(sent.trim()))) {
        setRefused(true);
        return;
      }
      // What was typed while the project was being made stays, as on a TODO's new line.
      const current = nameRef.current;
      if (current.startsWith(sent)) changeName(current.slice(sent.length).trimStart());
    } finally {
      sendingRef.current = false;
    }
  };

  return (
    <div className="todo-project-line">
      <input
        type="text"
        className="todo-project-line__field"
        aria-label={t('todo.project.add')}
        placeholder={t('todo.project.add')}
        value={name}
        onChange={(event) => changeName(event.target.value)}
        onKeyDown={(event) => {
          // Keys an IME takes while composing, its Escape among them, are the IME's.
          if (isImeKey(event)) return;
          if (event.key === 'Escape') {
            changeName('');
            event.currentTarget.blur();
            return;
          }
          if (event.key !== 'Enter' || event.shiftKey) return;
          event.preventDefault();
          void send();
        }}
      />
      {refused && failure ? (
        <p className="todo-project-line__failure" role="alert">
          {failure}
        </p>
      ) : null}
    </div>
  );
}
