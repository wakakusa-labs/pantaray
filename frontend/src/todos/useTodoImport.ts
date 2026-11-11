import { useRef, useState } from 'react';

import type { ActionFileAttachment } from '../../electron/src/actions/actionContracts';
import type { TodoScopeQuery } from '../../electron/src/todos/todoContracts';
import type {
  TodoImportRefusal,
  TodoImportResult,
} from '../../electron/src/todos/todoImportContracts';

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
