import { openActionFileInApp } from '../actions/actionFileAccess';
import type { WorkspaceSettings } from './workspaceSettingsFetch';

/**
 * Opens a registered workspace folder in Finder. The renderer names the folder by id and the
 * path comes from the backend's own list, so no path the renderer sent is ever opened; an app
 * bundle is refused as it is for an Action's files, because opening it would run it.
 */
export async function openRegisteredWorkspaceFolder(
  folderId: string,
  deps: {
    getSettings: () => Promise<WorkspaceSettings>;
    openPath: (realPath: string) => Promise<string>;
  }
): Promise<void> {
  const folder = (await deps.getSettings()).folders.find(
    (candidate) => candidate.folder_id === folderId
  );
  if (!folder) throw new Error('Workspace folder is not registered.');
  const result = await openActionFileInApp(folder.canonical_real_path, deps.openPath);
  if (result.kind !== 'opened') throw new Error(`Workspace folder not opened: ${result.reason}`);
}
