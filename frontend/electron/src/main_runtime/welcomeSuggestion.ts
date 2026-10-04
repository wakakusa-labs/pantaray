import type { LocalBackendRequest } from '../localBackend/client';
import { LocalBackendRequestError } from '../localBackend/client';

/**
 * Waits between attempts. Recording asks once per owner in a run, so a runtime that is busy
 * or still settling right after activation gets a few chances before the next launch. That
 * includes the 503 the runtime answers while this app's WebSocket session is not open yet:
 * it stores no greeting that nothing could show.
 */
export const WELCOME_SUGGESTION_RETRY_DELAYS_MS: readonly number[] = [2_000, 10_000, 30_000];

/** A 4xx answer (owner changed, malformed text) says the same thing on every attempt. */
const isFinalRefusal = (error: unknown) =>
  error instanceof LocalBackendRequestError &&
  error.status !== null &&
  error.status >= 400 &&
  error.status < 500;

export async function postWelcomeSuggestion(params: {
  requestJson: <T>(request: LocalBackendRequest) => Promise<T>;
  userId: string;
  answer: string;
  retryDelaysMs?: readonly number[];
  sleep?: (ms: number) => Promise<void>;
}): Promise<void> {
  const delays = params.retryDelaysMs ?? WELCOME_SUGGESTION_RETRY_DELAYS_MS;
  const sleep = params.sleep ?? ((ms: number) => new Promise((resolve) => setTimeout(resolve, ms)));
  for (let attempt = 0; ; attempt += 1) {
    try {
      // The runtime keys the welcome to the user, so a retry after a lost response is a no-op.
      await params.requestJson({
        path: `/v1/agents/users/${encodeURIComponent(params.userId)}/suggestions/welcome`,
        method: 'POST',
        body: { answer: params.answer },
        timeoutMs: 10_000,
      });
      return;
    } catch (error) {
      if (attempt >= delays.length || isFinalRefusal(error)) throw error;
      await sleep(delays[attempt]);
    }
  }
}
