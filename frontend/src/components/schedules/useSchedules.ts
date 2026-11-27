import { useCallback, useEffect, useRef, useState } from 'react';

import type { HookList } from '../../../electron/src/settings/hooksFetch';

/** Changes come in bursts (a run starting, then its steps), so they are read once together. */
const CHANGE_REFRESH_DELAY_MS = 150;
/**
 * A schedule stops a few seconds after its last turn ends, or with no turn at all when one
 * could not start, and nothing tells the window, so a shown list is read again this often.
 * Design limit: one local read per 5 s per screen showing schedules, only while the window is
 * visible; replace it with a pushed change notification from the runtime once the runtime
 * sends one for hooks, or if the read shows up in profiles.
 */
export const SCHEDULES_POLL_MS = 5_000;

export type Schedules = {
  /** Null until the first read answers. */
  list: HookList | null;
  /** The last read failed; `list` is what an earlier read returned. */
  loadFailed: boolean;
  /** Switches one on or off (on also runs a stopped one again); false when main refused it. */
  setEnabled: (hookId: string, enabled: boolean) => Promise<boolean>;
};

/**
 * The owner's schedules, read again whenever conversations change: a schedule is set up, runs
 * and goes with its conversation as a turn ends, starts or is deleted. Recording turning on or
 * off changes whether a window schedule watches, a stop lands after its turn (`SCHEDULES_POLL_MS`),
 * and a WS session that started over may have missed changes, so each of those reads again too.
 * Only the latest read lands.
 */
export function useSchedules(): Schedules {
  const [list, setList] = useState<HookList | null>(null);
  const [loadFailed, setLoadFailed] = useState(false);
  const generationRef = useRef(0);
  const reloadRef = useRef<() => Promise<void>>(async () => undefined);

  useEffect(() => {
    const reload = async () => {
      const generation = ++generationRef.current;
      try {
        const hooks = window.electron?.hooks;
        if (!hooks) throw new Error('Schedules bridge is unavailable.');
        const next = await hooks.list();
        if (generation !== generationRef.current) return;
        setList(next);
        setLoadFailed(false);
      } catch {
        if (generation !== generationRef.current) return;
        setLoadFailed(true);
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
    const visible = () => document.visibilityState === 'visible';
    const poll = setInterval(() => {
      if (visible()) void reload();
    }, SCHEDULES_POLL_MS);
    const onVisibility = () => {
      if (visible()) void reload();
    };
    document.addEventListener('visibilitychange', onVisibility);
    const unsubscribes = [
      window.electron?.history?.onChanged?.(onChange),
      window.electron?.screenshot?.onStatusChanged?.(onChange),
      window.electron?.orchestration?.onStatus?.((status) => {
        if (status.status === 'session_started') void reload();
      }),
    ];
    return () => {
      for (const unsubscribe of unsubscribes) unsubscribe?.();
      clearInterval(poll);
      document.removeEventListener('visibilitychange', onVisibility);
      if (timer !== null) clearTimeout(timer);
      // An answer still on its way belongs to a screen that is gone.
      generationRef.current += 1;
    };
  }, []);

  const setEnabled = useCallback(async (hookId: string, enabled: boolean) => {
    try {
      const hooks = window.electron?.hooks;
      if (!hooks) throw new Error('Schedules bridge is unavailable.');
      await hooks.setEnabled({ hook_id: hookId, enabled });
    } catch {
      return false;
    }
    await reloadRef.current();
    return true;
  }, []);

  return { list, loadFailed, setEnabled };
}
