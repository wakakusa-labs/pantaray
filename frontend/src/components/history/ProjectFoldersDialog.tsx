import { useEffect, useRef } from 'react';

import type { useI18n } from '@/context/useI18n';
import type {
  WorkspaceFolder,
  WorkspaceProject,
} from '@/pages/settings/components/workspaceSettingsModel';

import { ProjectFolders } from './ProjectFolders';

/**
 * A project's folders, opened from its menu: each one opened in Finder or removed, and one added.
 * Mounted only while open; the caller moves focus back once it is gone.
 */
export function ProjectFoldersDialog({
  project,
  folders,
  isFolderPending,
  onOpenFolder,
  onRemoveFolder,
  onAddFolder,
  t,
  onClose,
}: {
  project: WorkspaceProject;
  folders: WorkspaceFolder[];
  isFolderPending: (folderId: string) => boolean;
  onOpenFolder: (folderId: string) => void;
  onRemoveFolder: (folderId: string) => Promise<boolean>;
  onAddFolder: () => void;
  t: ReturnType<typeof useI18n>['t'];
  onClose: () => void;
}) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const dialog = dialogRef.current;
    if (dialog && !dialog.open) dialog.showModal();
  }, []);

  return (
    <dialog
      ref={dialogRef}
      className="history-delete-dialog project-folders-dialog"
      aria-labelledby="project-folders-dialog-title"
      onCancel={(event) => {
        event.preventDefault();
        onClose();
      }}
    >
      <h2 id="project-folders-dialog-title">
        {t('history.projects.foldersTitle', { name: project.display_name })}
      </h2>
      <ProjectFolders
        projectId={project.project_id}
        folders={folders}
        isFolderPending={isFolderPending}
        onOpenFolder={onOpenFolder}
        onRemoveFolder={onRemoveFolder}
        onAddFolder={onAddFolder}
        t={t}
      />
      <div className="history-delete-dialog__actions">
        <button type="button" className="history-filter-button" onClick={onClose}>
          {t('common.close')}
        </button>
      </div>
    </dialog>
  );
}
