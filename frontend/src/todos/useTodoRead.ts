import { useCallback, useEffect, useRef, useState } from 'react';

export type TodosBridge = NonNullable<NonNullable<Window['electron']>['todos']>;

export type TodoRead<T> = {
  /** Null until the first read answers. */
  data: T | null;
  /** The last read failed; `data` is what an earlier read returned. */
  error: boolean;
  /** Resolves once a read started at or after the call has landed or failed. */
  reload: () => Promise<void>;
};

/** Changes come in bursts (a write, then its Action starting), so they are read once together. */
const CHANGE_REFRESH_DELAY_MS = 150;

/**
 * Reads from the TODO bridge, and again after `todos:changed` and, when asked, `history:changed`.
 * A WS session that started over may have missed changes (the relay does not replay them), so
 * `session_started` reads again too. Only the latest read lands, so an answer to an earlier query
 * or change never replaces a newer one. `read` names what is read: a new function reads again.
 */
export function useTodoRead<T>(
  read: (todos: TodosBridge) => Promise<T>,
  rereadOnHistory: boolean
): TodoRead<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState(false);
  const generationRef = useRef(0);
  const reloadRef = useRef<() => Promise<void>>(async () => undefined);
  // Each `reload()` waits for a read started at or after it to land or fail: a read a later one
  // superseded never lands, so its caller waits for the later one.
  const waitersRef = useRef<{ generation: number; resolve: () => void }[]>([]);

  useEffect(() => {
    const settle = (generation: number) => {
      waitersRef.current = waitersRef.current.filter((waiter) => {
        if (waiter.generation > generation) return true;
        waiter.resolve();
        return false;
      });
    };
    const reload = () => {
      const generation = ++generationRef.current;
      const settled = new Promise<void>((resolve) =>
        waitersRef.current.push({ generation, resolve })
      );
      void (async () => {
        try {
          const todos = window.electron?.todos;
          if (!todos) throw new Error('TODO bridge is unavailable.');
          const next = await read(todos);
          if (generation !== generationRef.current) return;
          setData(next);
          setError(false);
        } catch {
          if (generation !== generationRef.current) return;
          // The screen keeps what it last read and says the read failed; the next change reads again.
          setError(true);
        }
        settle(generation);
      })();
      return settled;
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
      window.electron?.orchestration?.onStatus?.((status) => {
        if (status.status === 'session_started') void reload();
      }),
    ];
    return () => {
      for (const unsubscribe of unsubscribes) unsubscribe?.();
      if (timer !== null) clearTimeout(timer);
      // An answer still on its way belongs to what is no longer read.
      generationRef.current += 1;
    };
  }, [read, rereadOnHistory]);

  // Nothing reads once the screen is gone, so no caller is left waiting for a read.
  useEffect(
    () => () => {
      for (const waiter of waitersRef.current) waiter.resolve();
      waitersRef.current = [];
    },
    []
  );

  // Stable across queries, so a caller can hand it down without reading again.
  const reload = useCallback(() => reloadRef.current(), []);
  return { data, error, reload };
}
