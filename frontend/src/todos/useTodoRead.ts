import { useCallback, useEffect, useRef, useState } from 'react';

export type TodosBridge = NonNullable<NonNullable<Window['electron']>['todos']>;

export type TodoRead<T> = {
  /** Null until the first read answers. */
  data: T | null;
  /** The last read failed; `data` is what an earlier read returned. */
  error: boolean;
  reload: () => Promise<void>;
};

/** Changes come in bursts (a write, then its Action starting), so they are read once together. */
const CHANGE_REFRESH_DELAY_MS = 150;

/**
 * Reads from the TODO bridge, and again after `todos:changed` and, when asked, `history:changed`.
 * Only the latest read lands, so an answer to an earlier query or change never replaces a newer
 * one. `read` names what is read: a new function reads again.
 */
export function useTodoRead<T>(
  read: (todos: TodosBridge) => Promise<T>,
  rereadOnHistory: boolean
): TodoRead<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState(false);
  const generationRef = useRef(0);
  const reloadRef = useRef<() => Promise<void>>(async () => undefined);

  useEffect(() => {
    const reload = async () => {
      const generation = ++generationRef.current;
      try {
        const todos = window.electron?.todos;
        if (!todos) throw new Error('TODO bridge is unavailable.');
        const next = await read(todos);
        if (generation !== generationRef.current) return;
        setData(next);
        setError(false);
      } catch {
        // The screen keeps what it last read and says the read failed; the next change reads again.
        if (generation === generationRef.current) setError(true);
      }
    };
    reloadRef.current = reload;
    void reload();

    let timer: ReturnType<typeof setTimeout> | null = null;
    const onChange = () => {
      if (timer !== null) clearTimeout(timer);
      timer = setTimeout(() => {
        timer = null;
        void reload();
      }, CHANGE_REFRESH_DELAY_MS);
    };
    const unsubscribes = [
      window.electron?.todos?.onChanged(onChange),
      rereadOnHistory ? window.electron?.history?.onChanged?.(onChange) : undefined,
    ];
    return () => {
      for (const unsubscribe of unsubscribes) unsubscribe?.();
      if (timer !== null) clearTimeout(timer);
      // An answer still on its way belongs to what is no longer read.
      generationRef.current += 1;
    };
  }, [read, rereadOnHistory]);

  // Stable across queries, so a caller can hand it down without reading again.
  const reload = useCallback(() => reloadRef.current(), []);
  return { data, error, reload };
}
