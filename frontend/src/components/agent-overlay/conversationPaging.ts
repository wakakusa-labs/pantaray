import { readConversationScrollPosition } from './conversationScrollPosition';
import type { ActionConversationPage } from '../../../electron/src/actions/actionContracts';
import {
  applyActionConversationPage,
  mergeActionConversationLivePage,
  type ActionConversationPageChain,
} from '../../../electron/src/actions/actionConversationModel';

export const ACTION_CONVERSATION_PAGE_LIMIT = 25;
const INITIAL_OLDER_PAGES = 2;
type ActionsApi = NonNullable<NonNullable<typeof window.electron>['actions']>;
export type ConversationPagingState = {
  pages: ActionConversationPageChain | null;
  currentPage: ActionConversationPage | null;
  olderPageState: 'idle' | 'loading' | 'failed';
  boundPageState: 'idle' | 'loading' | 'failed';
};
export const EMPTY_CONVERSATION_PAGING: ConversationPagingState = {
  pages: null,
  currentPage: null,
  olderPageState: 'idle',
  boundPageState: 'idle',
};

/**
 * Reads one conversation from its newest page to its first, apart from the visible
 * history. Null when any page cannot be read, so a caller never sees a partial past.
 */
export async function readWholeConversation(
  actions: Pick<ActionsApi, 'readConversationPage'>,
  actionId: string
): Promise<ActionConversationPageChain | null> {
  let chain: ActionConversationPageChain | null = null;
  let cursor: string | null = null;
  do {
    const result = await actions.readConversationPage({
      actionId,
      cursor,
      limit: ACTION_CONVERSATION_PAGE_LIMIT,
    });
    if ('kind' in result) return null;
    chain = applyActionConversationPage(chain, cursor, result);
    cursor = result.next_cursor;
  } while (cursor !== null);
  return chain;
}

/** Owns the visible history and the lifetime of reads which can replace it. */
export function createConversationPaging(
  actions: Pick<ActionsApi, 'readConversationPage'>,
  publish: (state: ConversationPagingState) => void
) {
  let state = EMPTY_CONVERSATION_PAGING;
  let generation = 0;
  let head: ActionConversationPage | null = null;
  let refreshRequested = false;
  let refreshing = false;
  const emit = (next: ConversationPagingState) => {
    state = next;
    publish(next);
  };

  const refreshPast = async (first: ActionConversationPage): Promise<void> => {
    const scope = ++generation;
    const previous = state.pages;
    const previousRuns = previous?.flatMap((page) => page.runs) ?? [];
    const oldestRunId = previousRuns[previousRuns.length - 1]?.run_id;
    const remainingMessages = new Set(
      previous?.flatMap((page) => page.unadopted_messages.map((entry) => entry.step_id))
    );
    const observeMessages = (page: ActionConversationPage) => {
      for (const entry of [...page.unadopted_messages, ...page.runs.flatMap((run) => run.entries)])
        remainingMessages.delete(entry.step_id);
    };
    observeMessages(first);
    let chain: ActionConversationPageChain = [first];
    const savedPosition = readConversationScrollPosition(first.action.action_id);
    const initialOlderPages = Math.max(
      INITIAL_OLDER_PAGES,
      savedPosition && !savedPosition.atBottom ? savedPosition.pageCount - 1 : 0
    );
    let count = 0;
    refreshing = true;
    refreshRequested = false;
    emit({ ...state, olderPageState: 'loading' });
    try {
      while (
        chain[chain.length - 1].next_cursor !== null &&
        (count < initialOlderPages ||
          remainingMessages.size > 0 ||
          (oldestRunId !== undefined &&
            !chain.some((page) => page.runs.some((run) => run.run_id === oldestRunId))))
      ) {
        const cursor = chain[chain.length - 1].next_cursor!;
        const result = await actions.readConversationPage({
          actionId: first.action.action_id,
          cursor,
          limit: ACTION_CONVERSATION_PAGE_LIMIT,
        });
        if (scope !== generation) return;
        if ('kind' in result) {
          emit({ ...state, olderPageState: 'failed' });
          return;
        }
        chain = applyActionConversationPage(chain, cursor, result);
        observeMessages(result);
        count += 1;
      }
      if (scope !== generation || head === null) return;
      // Canonical updates replace the head. Keep the newer head when the
      // history read finishes, and replace the visible past only as a whole.
      emit({
        ...state,
        pages: [head, ...chain.slice(1)],
        currentPage: head,
        olderPageState: 'idle',
      });
    } catch {
      if (scope === generation) emit({ ...state, olderPageState: 'failed' });
    } finally {
      if (scope === generation) {
        refreshing = false;
        // A cursor can move while its read is in flight. Coalesce those updates
        // into one read of the newest boundary, including an invalidated cursor.
        if (head !== null && refreshRequested) void refreshPast(head);
      }
    }
  };

  const update = (page: ActionConversationPage | null) => {
    if (page === null) {
      generation += 1;
      head = null;
      refreshing = false;
      refreshRequested = false;
      emit({ ...state, currentPage: null, olderPageState: 'idle' });
      return;
    }
    const actionChanged = head?.action.action_id !== page.action.action_id;
    const runChanged = head?.action.latest_run_id !== page.action.latest_run_id;
    if (actionChanged || runChanged) {
      generation += 1;
      refreshing = false;
      refreshRequested = false;
    }
    head = page;
    emit({
      ...state,
      pages: mergeActionConversationLivePage(state.pages, page),
      currentPage: page,
      boundPageState: 'idle',
    });
    // Pending USER messages live outside the latest-run page and can change
    // without changing its cursor. Every canonical page refresh invalidates them.
    refreshRequested = true;
    if (!refreshing) {
      void refreshPast(page);
    }
  };

  const readLatest = async (actionId: string): Promise<ActionConversationPage | null> => {
    const scope = generation;
    try {
      const result = await actions.readConversationPage({
        actionId,
        cursor: null,
        limit: ACTION_CONVERSATION_PAGE_LIMIT,
      });
      if (scope !== generation) return null;
      if ('kind' in result) return null;
      return result;
    } catch {
      return null;
    }
  };
  const loadBound = async (actionId: string) => {
    if (state.boundPageState === 'loading') return;
    emit({ ...state, boundPageState: 'loading' });
    const scope = generation;
    const page = await readLatest(actionId);
    if (scope !== generation) return;
    if (page === null) emit({ ...state, boundPageState: 'failed' });
    else update(page);
    return page;
  };
  const loadOlder = async (): Promise<ActionConversationPage | null> => {
    if (head === null || state.olderPageState === 'loading' || refreshing) return null;
    const actionId = head.action.action_id;
    const cursor = state.pages?.[state.pages.length - 1].next_cursor;
    const wasFailed = state.olderPageState === 'failed';
    if (!wasFailed && !cursor) return null;
    const scope = generation;
    emit({ ...state, olderPageState: 'loading' });
    try {
      const result = wasFailed
        ? null
        : await actions.readConversationPage({
            actionId,
            cursor: cursor!,
            limit: ACTION_CONVERSATION_PAGE_LIMIT,
          });
      if (scope !== generation) return null;
      if (result === null || 'kind' in result) {
        const latest = await readLatest(actionId);
        if (scope !== generation) return null;
        if (latest === null) {
          emit({ ...state, olderPageState: 'failed' });
          return null;
        }
        head = latest;
        emit({
          ...state,
          pages: mergeActionConversationLivePage(state.pages, latest),
          currentPage: latest,
        });
        const pending = refreshPast(latest);
        const refreshGeneration = generation;
        await pending;
        return generation === refreshGeneration &&
          state.olderPageState === 'idle' &&
          state.pages !== null
          ? state.pages[state.pages.length - 1]
          : null;
      }
      emit({
        ...state,
        pages: applyActionConversationPage(state.pages, cursor!, result),
        olderPageState: 'idle',
      });
      return result;
    } catch {
      if (scope === generation) emit({ ...state, olderPageState: 'failed' });
      return null;
    }
  };
  const reset = () => {
    generation += 1;
    head = null;
    refreshRequested = false;
    refreshing = false;
    emit(EMPTY_CONVERSATION_PAGING);
  };
  return {
    update,
    loadBound,
    loadOlder,
    reset,
    dispose: () => {
      generation += 1;
      refreshing = false;
      refreshRequested = false;
    },
  };
}
