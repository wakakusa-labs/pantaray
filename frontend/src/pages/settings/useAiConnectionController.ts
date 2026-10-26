import { useCallback, useEffect, useRef, useState } from 'react';
import type {
  ConnectionCommand,
  ConnectionStateResult,
  ConnectionUpdateResult,
} from '../../../electron/src/ipc/schemas/aiConnection';

type Attempt = { operation: ConnectionCommand['operation']; settled: boolean; cancelling: boolean };

export function useAiConnectionController() {
  const [state, setState] = useState<Extract<ConnectionStateResult, { ok: true }> | null>(null);
  const [loadFailure, setLoadFailure] = useState<Extract<
    ConnectionStateResult,
    { ok: false }
  > | null>(null);
  const [pendingOperation, setPendingOperation] = useState<ConnectionCommand['operation'] | null>(
    null
  );
  const [outcome, setOutcome] = useState<{
    operation: ConnectionCommand['operation'];
    result: ConnectionUpdateResult;
  } | null>(null);
  const active = useRef(false);
  const generation = useRef(0);
  const latestRead = useRef(Promise.resolve());
  const pending = useRef<Attempt | null>(null);

  const refresh = useCallback(() => {
    const request = ++generation.current;
    const read = (async () => {
      let result: ConnectionStateResult;
      try {
        const bridge = window.electron?.aiConnection;
        result = bridge ? await bridge.getState() : { ok: false, error: 'runtime_unavailable' };
      } catch {
        result = { ok: false, error: 'runtime_unavailable' };
      }
      if (!active.current || request !== generation.current) return;
      if (result.ok) {
        setState(result);
        setLoadFailure(null);
      } else {
        setLoadFailure(result);
      }
    })();
    latestRead.current = read;
    return read;
  }, []);

  useEffect(() => {
    active.current = true;
    // Subscribe first: helper configuration can finish while the initial read is pending.
    const unsubscribe = window.electron?.aiConnection?.onChanged(() => void refresh());
    const unsubscribeAuth = window.electron?.auth?.onStateChanged?.(() => void refresh());
    void refresh();
    return () => {
      active.current = false;
      generation.current += 1;
      unsubscribe?.();
      unsubscribeAuth?.();
      const attempt = pending.current;
      pending.current = null;
      if (attempt?.operation === 'sign_in_chatgpt' && !attempt.settled) {
        // The page owns this login attempt; leaving it must not leave an invisible login running.
        void window.electron?.aiConnection
          ?.update({ operation: 'cancel_chatgpt_sign_in' })
          .then((result) => {
            if (!result.ok) console.error('AI_CONNECTION_CANCEL_FAILED');
          })
          .catch(() => console.error('AI_CONNECTION_CANCEL_FAILED'));
      }
    };
  }, [refresh]);

  async function run(command: ConnectionCommand): Promise<boolean> {
    if (!active.current || pending.current) return false;
    const attempt: Attempt = { operation: command.operation, settled: false, cancelling: false };
    pending.current = attempt;
    setPendingOperation(command.operation);
    setOutcome(null);
    let result: ConnectionUpdateResult;
    try {
      const bridge = window.electron?.aiConnection;
      result = bridge ? await bridge.update(command) : { ok: false, error: 'runtime_unavailable' };
    } catch {
      result = { ok: false, error: 'runtime_unavailable' };
    }
    attempt.settled = true;
    if (pending.current !== attempt) return false;
    setOutcome({ operation: command.operation, result });
    // Persistence can succeed before applying to the helper fails. Always reread saved metadata.
    let read = refresh();
    // Notifications can supersede this read. Keep provider-bound controls locked until
    // the newest read commits either current metadata or the error that hides the controls.
    while (true) {
      await read;
      if (read === latestRead.current || pending.current !== attempt) break;
      read = latestRead.current;
    }
    if (pending.current === attempt) {
      pending.current = null;
      setPendingOperation(null);
    }
    return result.ok;
  }

  async function cancelSignIn(): Promise<void> {
    const attempt = pending.current;
    if (attempt?.operation !== 'sign_in_chatgpt' || attempt.settled || attempt.cancelling) return;
    attempt.cancelling = true;
    setPendingOperation('cancel_chatgpt_sign_in');
    let result: ConnectionUpdateResult;
    try {
      const bridge = window.electron?.aiConnection;
      result = bridge
        ? await bridge.update({ operation: 'cancel_chatgpt_sign_in' })
        : { ok: false, error: 'runtime_unavailable' };
    } catch {
      result = { ok: false, error: 'runtime_unavailable' };
    } finally {
      attempt.cancelling = false;
    }
    if (pending.current !== attempt || attempt.settled) return;
    if (result.ok) {
      // Main invalidates adoption immediately; the old token exchange may still be in flight.
      pending.current = null;
      setPendingOperation(null);
      setOutcome({ operation: attempt.operation, result: { ok: false, error: 'cancelled' } });
      await refresh();
    } else {
      setPendingOperation('sign_in_chatgpt');
      setOutcome({ operation: 'cancel_chatgpt_sign_in', result });
    }
  }

  return {
    state,
    loadError: loadFailure?.error ?? null,
    recovery: loadFailure?.recovery ?? null,
    pendingOperation,
    outcome,
    refresh,
    run,
    cancelSignIn,
  };
}

export type AiConnectionController = ReturnType<typeof useAiConnectionController>;
/** The connection as main last reported it. */
export type ConnectionState = NonNullable<AiConnectionController['state']>;
