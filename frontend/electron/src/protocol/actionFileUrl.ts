/** Privileged scheme that serves a PDF an Action names to the main window's preview. */
export const ACTION_FILE_SCHEME = 'pantaray-action-file';
/** `standard: true` schemes require a host; the Action and the path ride in the query. */
export const ACTION_FILE_URL_HOST = 'local';

/** The preview frame's URL for one file an Action names. */
export function buildActionFileUrl(actionId: string, filePath: string): string {
  const query = new URLSearchParams({ action: actionId, path: filePath });
  return `${ACTION_FILE_SCHEME}://${ACTION_FILE_URL_HOST}/?${query.toString()}`;
}
