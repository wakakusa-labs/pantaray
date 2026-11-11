import { useCallback, useRef, useState } from 'react';

import type { ActionFileAttachment } from '../../electron/src/actions/actionContracts';
import type { TodoScopeQuery } from '../../electron/src/todos/todoContracts';
import type {
  TodoImportDraft,
  TodoImportRefusal,
  TodoImportResult,
  TodoImportView,
} from '../../electron/src/todos/todoImportContracts';
import { useTodoRead } from './useTodoRead';

type TodoImportsBridge = NonNullable<NonNullable<Window['electron']>['todoImports']>;

/** Why the last request about an import did nothing: the backend's reason, or no answer. */
export type TodoImportProblem = TodoImportRefusal['reason'] | 'failed';

function importsBridge(): TodoImportsBridge {
  const imports = window.electron?.todoImports;
  if (!imports) throw new Error('TODO import bridge is unavailable.');
  return imports;
}

/**
 * Starts imports from the add bar. A start whose answer never came may have opened the import,
 * so sending the same text and files again reuses its `import_id`, and the backend returns that
 * import instead of opening a second one with a second Action.
 */
export function useTodoImportStart() {
  const [problem, setProblem] = useState<TodoImportProblem | null>(null);
  const unansweredRef = useRef<{ key: string; importId: string } | null>(null);

  /** Resolves the new import's id, or null when it did not start. */
  const start = async (
    scope: TodoScopeQuery,
    text: string,
    files: readonly ActionFileAttachment[]
  ): Promise<string | null> => {
    const key = JSON.stringify([scope, text, files.map((file) => file.attachment_id)]);
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
 * One import's draft, read again whenever the TODOs change, since each of its Action's writes
 * moves the same counter. The user's writes are not anticipated: while one is answering the
 * draft stays as read and `pending` is set, and after it the draft is read again, so a write
 * the backend refused as stale shows the draft as it is now.
 */
export function useTodoImport(importId: string) {
  const read = useCallback(() => importsBridge().read({ import_id: importId }), [importId]);
  const { data, error, reload } = useTodoRead(read, false);
  const [pending, setPending] = useState(false);
  // A text field's Enter and the blur after it would both write; the second waits for nothing.
  const pendingRef = useRef(false);
  const [problem, setProblem] = useState<TodoImportProblem | null>(null);
  const view = data?.kind === 'ok' ? data.result : null;

  /** Resolves the import as the request left it, or null when it was refused or unanswered. */
  const send = async (
    request: (imports: TodoImportsBridge, view: TodoImportView) => Promise<TodoImportResult>
  ): Promise<TodoImportView | null> => {
    if (view === null || pendingRef.current) return null;
    pendingRef.current = true;
    setPending(true);
    setProblem(null);
    try {
      const result = await request(importsBridge(), view);
      if (result.kind === 'refused') setProblem(result.refusal.reason);
      await reload();
      return result.kind === 'ok' ? result.result : null;
    } catch {
      setProblem('failed');
      return null;
    } finally {
      pendingRef.current = false;
      setPending(false);
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
    write: (draft: TodoImportDraft) =>
      send((imports, current) =>
        imports.write({ import_id: importId, expected_revision: current.revision, draft })
      ),
    register: () =>
      send((imports, current) =>
        imports.register({ import_id: importId, expected_revision: current.revision })
      ),
    cancel: () => send((imports) => imports.cancel({ import_id: importId })),
  };
}
