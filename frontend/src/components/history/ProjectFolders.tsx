import { FolderOpen, Plus, X } from 'lucide-react';
import { useState } from 'react';

import type { useI18n } from '@/context/useI18n';
import {
  splitPathForMiddleEllipsis,
  type WorkspaceFolder,
} from '@/pages/settings/components/workspaceSettingsModel';

import './historyProjects.css';

const folderRemoveId = (projectId: string, folderId: string) =>
  `project-folder-remove:${projectId}:${folderId}`;
const addFolderId = (projectId: string) => `project-folder-add:${projectId}`;

/**
 * A project's folders: each path cut in its middle (clicked, it shows whole), opened in Finder or
 * removed from the project, and ＋ to add one. Removing moves focus to the next folder's remove,
 * or to ＋ once none is left.
 */
export function ProjectFolders({
  projectId,
  folders,
  isFolderPending,
  onOpenFolder,
  onRemoveFolder,
  onAddFolder,
  autoFocusAdd = false,
  t,
}: {
  projectId: string;
  folders: WorkspaceFolder[];
  /** A folder being added or removed elsewhere cannot be removed until that settles. */
  isFolderPending: (folderId: string) => boolean;
  onOpenFolder: (folderId: string) => void;
  onRemoveFolder: (folderId: string) => Promise<boolean>;
  onAddFolder: () => void;
  /** A project just created opens with ＋ focused. */
  autoFocusAdd?: boolean;
  t: ReturnType<typeof useI18n>['t'];
}) {
  // The folder whose whole path is shown, wrapped, instead of cut to the line.
  const [fullPathId, setFullPathId] = useState<string | null>(null);
  const removeFolder = async (folderId: string) => {
    if (!(await onRemoveFolder(folderId))) return;
    const ids = folders.map((folder) => folder.folder_id);
    const remaining = ids.filter((id) => id !== folderId);
    const next = remaining[Math.min(ids.indexOf(folderId), remaining.length - 1)];
    document
      .getElementById(next ? folderRemoveId(projectId, next) : addFolderId(projectId))
      ?.focus();
  };

  return (
    <div className="project-folders">
      <ul aria-label={t('history.projects.folders')}>
        {folders.map((folder) => {
          const [pathStart, pathEnd] = splitPathForMiddleEllipsis(folder.real_path);
          const pending = isFolderPending(folder.folder_id);
          return (
            <li key={folder.folder_id} className="history-project__folder">
              <button
                type="button"
                className="history-project__path"
                // The two halves are drawn apart; the whole path is what the button is.
                aria-label={folder.real_path}
                title={folder.real_path}
                aria-expanded={fullPathId === folder.folder_id}
                onClick={() =>
                  setFullPathId((shown) => (shown === folder.folder_id ? null : folder.folder_id))
                }
              >
                <span className="history-project__path-start">{pathStart}</span>
                <span className="history-project__path-end">{pathEnd}</span>
              </button>
              <button
                type="button"
                className="history-project__folder-action"
                aria-label={t('history.projects.openInFinder')}
                data-tooltip={t('history.projects.openInFinder')}
                onClick={() => onOpenFolder(folder.folder_id)}
              >
                <FolderOpen size={13} aria-hidden="true" />
              </button>
              <button
                type="button"
                id={folderRemoveId(projectId, folder.folder_id)}
                className="history-project__folder-action"
                aria-label={t('history.projects.removeFolder', { name: folder.display_name })}
                data-tooltip={t('history.projects.removeFolder', { name: folder.display_name })}
                aria-busy={pending}
                disabled={pending}
                onClick={() => void removeFolder(folder.folder_id)}
              >
                <X size={13} aria-hidden="true" />
              </button>
            </li>
          );
        })}
      </ul>
      <button
        type="button"
        id={addFolderId(projectId)}
        className="project-folders__add"
        // Only a project just created asks for this; focus follows it from the name field.
        autoFocus={autoFocusAdd}
        onClick={onAddFolder}
      >
        <Plus size={13} aria-hidden="true" />
        {t('history.projects.addFolder')}
      </button>
    </div>
  );
}
