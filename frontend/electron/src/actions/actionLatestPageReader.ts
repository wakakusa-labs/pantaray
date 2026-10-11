import type { ActionConversationPage } from './actionContracts';

/**
 * A latest page and the number its read was given when it started. Reads are numbered across
 * all Actions in the order they start, so a live event observed while `readsStarted()` was
 * below `readNumber` happened before this read began and is reflected in its page.
 */
export type ActionLatestPageRead = { page: ActionConversationPage; readNumber: number };

export type ActionLatestPages = {
  read: (actionId: string) => Promise<ActionLatestPageRead>;
  /** How many reads have started so far; a live event is stamped with it. */
  readsStarted: () => number;
};

type Follower = {
  promise: Promise<ActionLatestPageRead>;
  resolve: (read: ActionLatestPageRead) => void;
  reject: (error: unknown) => void;
};
type Flight = { follower: Follower | null };

function createFollower(): Follower {
  let resolve!: Follower['resolve'];
  let reject!: Follower['reject'];
  const promise = new Promise<ActionLatestPageRead>((onResolve, onReject) => {
    resolve = onResolve;
    reject = onReject;
  });
  return { promise, resolve, reject };
}

/**
 * Reads an Action's latest page at most once at a time. Every caller gets a read that began
 * after it asked: one arriving while a read is in flight joins the single read queued behind
 * it. So each read settles its own callers as it lands, and no caller waits on more than two
 * reads however often the Action keeps changing.
 */
export function createActionLatestPageReader(
  readLatestPage: (actionId: string) => Promise<ActionConversationPage>
): ActionLatestPages & { clear: () => void } {
  const flights = new Map<string, Flight>();
  let readsStarted = 0;

  const begin = (actionId: string): Promise<ActionLatestPageRead> => {
    const flight: Flight = { follower: null };
    flights.set(actionId, flight);
    const readNumber = ++readsStarted;
    // The executor turns a synchronous throw into this read's rejection.
    const read = new Promise<ActionConversationPage>((resolve) =>
      resolve(readLatestPage(actionId))
    ).then((page) => ({ page, readNumber }));
    const advance = (settleDetached: (follower: Follower) => void) => {
      const { follower } = flight;
      if (flights.get(actionId) !== flight) {
        // clear() detached this flight: its queued callers take this read rather than read
        // again for a subject that is gone.
        if (follower) settleDetached(follower);
        return;
      }
      if (!follower) {
        flights.delete(actionId);
        return;
      }
      begin(actionId).then(follower.resolve, follower.reject);
    };
    read.then(
      (result) => advance((follower) => follower.resolve(result)),
      (error: unknown) => advance((follower) => follower.reject(error))
    );
    return read;
  };

  const read = (actionId: string): Promise<ActionLatestPageRead> => {
    const flight = flights.get(actionId);
    if (!flight) return begin(actionId);
    flight.follower ??= createFollower();
    return flight.follower.promise;
  };

  return {
    read,
    readsStarted: () => readsStarted,
    clear: () => flights.clear(),
  };
}
