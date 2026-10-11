import type { ActionConversationPage } from './actionContracts';

type Follower = {
  promise: Promise<ActionConversationPage>;
  resolve: (page: ActionConversationPage) => void;
  reject: (error: unknown) => void;
};
type Flight = { follower: Follower | null };

function createFollower(): Follower {
  let resolve!: Follower['resolve'];
  let reject!: Follower['reject'];
  const promise = new Promise<ActionConversationPage>((onResolve, onReject) => {
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
): {
  read: (actionId: string) => Promise<ActionConversationPage>;
  clear: () => void;
} {
  const flights = new Map<string, Flight>();

  const begin = (actionId: string): Promise<ActionConversationPage> => {
    const flight: Flight = { follower: null };
    flights.set(actionId, flight);
    // The executor turns a synchronous throw into this read's rejection.
    const read = new Promise<ActionConversationPage>((resolve) =>
      resolve(readLatestPage(actionId))
    );
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
      (page) => advance((follower) => follower.resolve(page)),
      (error: unknown) => advance((follower) => follower.reject(error))
    );
    return read;
  };

  const read = (actionId: string): Promise<ActionConversationPage> => {
    const flight = flights.get(actionId);
    if (!flight) return begin(actionId);
    flight.follower ??= createFollower();
    return flight.follower.promise;
  };

  return {
    read,
    clear: () => flights.clear(),
  };
}
