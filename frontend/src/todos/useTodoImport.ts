import { useCallback, useRef, useState } from 'react';

import type {
  ActionFileAttachment,
  ActionImageReference,
} from '../../electron/src/actions/actionContracts';
import type { TodoScopeQuery } from '../../electron/src/todos/todoContracts';
import type {
  OpenTodoImport,
  TodoImportDraft,
  TodoImportRefusal,
  TodoImportResult,
  TodoImportView,
} from '../../electron/src/todos/todoImportContracts';
import { useTodoRead } from './useTodoRead';

type TodoImportsBridge = NonNullable<NonNullable<Window['electron']>['todoImports']>;

/** What the add bar staged for an import: documents to read and images to look at. */
export type TodoImportAttachments = Readonly<{
  files: readonly ActionFileAttachment[];
  images: readonly ActionImageReference[];
}>;

/** Why the last request about an import did nothing: the backend's reason, or no answer. */
export type TodoImportProblem = TodoImportRefusal['reason'] | 'failed';

function importsBridge(): TodoImportsBridge {
  const imports = window.electron?.todoImports;
  if (!imports) throw new Error('TODO import bridge is unavailable.');
  return imports;
}

/**
 * Starts imports from the add bar. A start whose answer never came may have opened the import,
 * so sending the same text and attachments again reuses its `import_id`, and the backend returns that
 * import instead of opening a second one with a second Action.
 */
export function useTodoImportStart() {
  const [problem, setProblem] = useState<TodoImportProblem | null>(null);
  const unansweredRef = useRef<{ key: string; importId: string } | null>(null);

  /** Resolves the new import's id, or null when it did not start. */
  const start = async (
    scope: TodoScopeQuery,
    text: string,
    { files, images }: TodoImportAttachments
  ): Promise<string | null> => {
    const key = JSON.stringify([
      scope,
      text,
      files.map((file) => file.attachment_id),
      images.map((image) => image.storage_path),
    ]);
    const importId =
      unansweredRef.current?.key === key ? unansweredRef.current.importId : crypto.randomUUID();
    unansweredRef.current = { key, importId };
    setProblem(null);
    let result: TodoImportResult;
    try {
      result = await importsBridge().start({
        import_id: importId,
        scope: scope.scope,
        ...(scope.scope === 'organization' ? { organization_id: scope.organization_id } : {}),
        ...(text.trim() === '' ? {} : { text }),
        ...(files.length === 0 ? {} : { files: [...files] }),
        ...(images.length === 0 ? {} : { images: [...images] }),
      });
    } catch {
      setProblem('failed');
      return null;
    }
    unansweredRef.current = null;
    if (result.kind === 'refused') {
      setProblem(result.refusal.reason);
      return null;
    }
    return result.result.import_id;
  };

  return { start, problem };
}

/**
 * The scope's open imports, newest first: those started in it, or every one for `all`. Read again
 * when the TODOs change, as each start, registration and cancel moves their counter; a start's
 * Action is created after that move, so the screen reads again once its start answered.
 */
export function useOpenTodoImports(scope: TodoScopeQuery): {
  items: readonly OpenTodoImport[];
  reload: () => Promise<void>;
} {
  const { scope: scopeKind, organization_id: organizationId } = scope;
  const read = useCallback(
    () =>
      importsBridge().listOpen({
        scope: scopeKind,
        ...(organizationId === undefined ? {} : { organization_id: organizationId }),
      }),
    [scopeKind, organizationId]
  );
  const { data, reload } = useTodoRead(read, false);
  return { items: data ?? [], reload };
}

/**
 * One import's draft, read again whenever the TODOs change, since each of its Action's writes
 * moves the same counter. The user's writes are not anticipated: while one is answering the
 * draft stays as read and `pending` is set, and after it the draft is read again, so a write
 * the backend refused as stale shows the draft as it is now. Any other write that did not land
 * is held as `unsaved` until it is saved again or discarded, so the screen never registers a
 * draft other than the one the user sees edited.
 */
export function useTodoImport(importId: string) {
  const read = useCallback(() => importsBridge().read({ import_id: importId }), [importId]);
  const { data, error, reload } = useTodoRead(read, false);
  const [pending, setPending] = useState(false);
  // A text field's Enter and the blur after it would both write; the second waits for nothing.
  const pendingRef = useRef(false);
  const [problem, setProblem] = useState<TodoImportProblem | null>(null);
  // The edit that did not land, with the revision it was made on: saving it again over a newer
  // draft would erase what the Action wrote since.
  const [unsaved, setUnsaved] = useState<{ draft: TodoImportDraft; revision: number } | null>(null);
  // Bumped when an edit is let go, so fields still showing it go back to the draft as read.
  const [discards, setDiscards] = useState(0);
  const view = data?.kind === 'ok' ? data.result : null;

  /** Resolves the import as the request left it, or why it did not change it. */
  const send = async (
    request: (imports: TodoImportsBridge, view: TodoImportView) => Promise<TodoImportResult>
  ): Promise<TodoImportView | TodoImportProblem | null> => {
    if (view === null || pendingRef.current) return null;
    pendingRef.current = true;
    setPending(true);
    setProblem(null);
    try {
      const result = await request(importsBridge(), view);
      if (result.kind === 'refused') setProblem(result.refusal.reason);
      await reload();
      return result.kind === 'ok' ? result.result : result.refusal.reason;
    } catch {
      setProblem('failed');
      return 'failed';
    } finally {
      pendingRef.current = false;
      setPending(false);
    }
  };

  const landed = (outcome: TodoImportView | TodoImportProblem | null) =>
    typeof outcome === 'object' ? outcome : null;
  const writeOver = async (draft: TodoImportDraft, revision: number) => {
    const outcome = await send((imports) =>
      imports.write({ import_id: importId, expected_revision: revision, draft })
    );
    if (outcome === null) return;
    if (typeof outcome === 'object') {
      setUnsaved(null);
    } else if (outcome === 'stale_revision') {
      // The draft read since replaces the edit, which was made on an older one.
      setUnsaved(null);
      setDiscards((count) => count + 1);
    } else {
      setUnsaved({ draft, revision });
    }
  };

  return {
    view,
    /** The import is gone, or was never opened under this id. */
    missing: data?.kind === 'refused',
    /** The last read failed; `view` is what an earlier one returned. */
    error,
    pending,
    problem,
    /** An edit whose write did not land; nothing registers until it is saved or discarded. */
    unsaved: unsaved !== null,
    /** Changes each time an edit is let go: fields keyed by it show the draft as read. */
    discards,
    /** Writes the draft over the revision on screen, which the edit was made on. */
    write: (draft: TodoImportDraft) =>
      view === null ? Promise.resolve() : writeOver(draft, view.revision),
    saveAgain: () =>
      unsaved === null ? Promise.resolve() : writeOver(unsaved.draft, unsaved.revision),
    discardUnsaved: () => {
      setUnsaved(null);
      setProblem(null);
      setDiscards((count) => count + 1);
    },
    register: async () =>
      landed(
        await send((imports, current) =>
          imports.register({ import_id: importId, expected_revision: current.revision })
        )
      ),
    cancel: async () => landed(await send((imports) => imports.cancel({ import_id: importId }))),
  };
}
