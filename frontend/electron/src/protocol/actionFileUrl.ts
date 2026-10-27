import type { TaskFileTarget } from '../actions/actionFileAccess';

/** Privileged scheme that serves a PDF an Action names or a Suggestion made to the preview. */
export const ACTION_FILE_SCHEME = 'pantaray-action-file';
/** `standard: true` schemes require a host; the owner and the path ride in the query. */
export const ACTION_FILE_URL_HOST = 'local';

/**
 * The preview frame's URL for one file of an Action or a Suggestion: the file itself, or with
 * `renderId` the PDF printed from it (`htmlPreview`).
 */
export function buildActionFileUrl(
  target: TaskFileTarget,
  filePath: string,
  renderId?: string
): string {
  const query = new URLSearchParams({ kind: target.kind, id: target.id, path: filePath });
  if (renderId !== undefined) query.set('render', renderId);
  return `${ACTION_FILE_SCHEME}://${ACTION_FILE_URL_HOST}/?${query.toString()}`;
}
