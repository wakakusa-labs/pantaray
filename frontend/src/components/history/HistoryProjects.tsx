import { DndContext } from '@dnd-kit/core';
import { SortableContext, verticalListSortingStrategy } from '@dnd-kit/sortable';
import { Plus } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import { flushSync } from 'react-dom';

import type { useI18n } from '@/context/useI18n';
import {
  workspacePendingKey,
  type useWorkspaceSettingsController,
} from '@/pages/settings/useWorkspaceSettingsController';
import {
  resolveProjectOrganizations,
  type WorkspaceProject,
} from '@/pages/settings/components/workspaceSettingsModel';

import { HistoryDeleteDialog } from './HistoryDeleteDialog';
import { HistoryProjectOrganizationDialog } from './HistoryProjectOrganizationDialog';
import { HistoryProjectRow } from './HistoryProjectRow';
import './historyProjects.css';

export type WorkspaceProjects = Pick<
  ReturnType<typeof useWorkspaceSettingsController>,
  | 'settings'
  | 'errorMessage'
  | 'pending'
  | 'addProjectFromFolder'
  | 'removeProject'
  | 'renameProject'
  | 'addFolderToProject'
  | 'removeFolderFromProject'
  | 'openFolder'
  | 'addOrganization'
  | 'updateProjectOrganizations'
  | 'dragController'
  | 'busy'
>;

type OpenDialog = { kind: 'delete' | 'organizations'; project: WorkspaceProject };

const menuButtonId = (projectId: string) => `history-project-menu:${projectId}`;

/**
 * The sidebar's projects, managed in place: ＋ adds a folder as a project, each row's menu
 * renames it, adds folders, sets its organization, opens it in Finder or deletes it, and rows
 * are dragged into order. They do not filter or group the tasks below.
 */
export function HistoryProjects({
  projects,
  t,
}: {
  projects: WorkspaceProjects;
  t: ReturnType<typeof useI18n>['t'];
}) {
  const { settings, errorMessage, pending, dragController } = projects;
  const addButtonRef = useRef<HTMLButtonElement>(null);
  const [dialog, setDialog] = useState<OpenDialog | null>(null);
  const [renamingId, setRenamingId] = useState<string | null>(null);
  // A rename answers after the user may have started renaming another project.
  const renamingIdRef = useRef(renamingId);
  useEffect(() => {
    renamingIdRef.current = renamingId;
  });
  const endRename = (projectId: string) => {
    if (renamingIdRef.current !== projectId) return false;
    flushSync(() => setRenamingId(null));
    return true;
  };
  const focusMenuButton = (projectId: string) =>
    document.getElementById(menuButtonId(projectId))?.focus();

  // A modal dialog holds focus until it is gone, so it closes before focus moves.
  const closeDialog = (project: WorkspaceProject) => {
    flushSync(() => setDialog(null));
    focusMenuButton(project.project_id);
  };
  const confirmDelete = async (project: WorkspaceProject) => {
    setDialog(null);
    if (await projects.removeProject(project.project_id)) addButtonRef.current?.focus();
    else focusMenuButton(project.project_id);
  };

  const foldersOf = (projectId: string) =>
    (settings?.folders ?? []).filter((folder) => folder.project_ids.includes(projectId));
  const menuItems = (project: WorkspaceProject) => {
    const firstFolder = foldersOf(project.project_id)[0];
    return [
      { label: t('history.projects.rename'), onSelect: () => setRenamingId(project.project_id) },
      {
        label: t('history.projects.addFolder'),
        onSelect: () => void projects.addFolderToProject(project.project_id),
      },
      {
        label: t('history.projects.setOrganization'),
        onSelect: () => setDialog({ kind: 'organizations', project }),
      },
      {
        label: t('history.projects.openInFinder'),
        disabled: !firstFolder,
        onSelect: () => firstFolder && void projects.openFolder(firstFolder.folder_id),
      },
      { label: t('common.delete'), onSelect: () => setDialog({ kind: 'delete', project }) },
    ];
  };

  return (
    <section className="history-projects" aria-labelledby="history-projects-title">
      <div className="history-projects__head">
        <h2 id="history-projects-title">{t('history.projects.title')}</h2>
        <button
          type="button"
          ref={addButtonRef}
          className="history-projects__add"
          aria-label={t('history.projects.add')}
          title={t('history.projects.add')}
          aria-busy={pending.has(workspacePendingKey.projectCreate)}
          disabled={settings === null}
          onClick={() => void projects.addProjectFromFolder()}
        >
          <Plus size={15} aria-hidden="true" />
        </button>
      </div>
      {errorMessage ? (
        <div className="history-error" role="alert">
          {errorMessage}
        </div>
      ) : null}
      {settings && settings.projects.length > 0 ? (
        <DndContext
          sensors={dragController.sensors}
          accessibility={dragController.accessibility}
          onDragStart={dragController.onDragStart}
          onDragOver={dragController.onDragOver}
          onDragEnd={dragController.onDragEnd}
          onDragCancel={dragController.onDragCancel}
        >
          <SortableContext
            items={settings.projects.map((project) => project.project_id)}
            strategy={verticalListSortingStrategy}
          >
            <ul className="history-projects__list">
              {settings.projects.map((project) => (
                <HistoryProjectRow
                  key={project.project_id}
                  project={project}
                  folders={foldersOf(project.project_id)}
                  organizations={resolveProjectOrganizations(project, settings.organizations)}
                  menuItems={menuItems(project)}
                  menuButtonId={menuButtonId(project.project_id)}
                  renaming={renamingId === project.project_id}
                  dragDisabled={projects.busy}
                  t={t}
                  onRename={(name) => projects.renameProject(project.project_id, name)}
                  onRenameEnd={() => endRename(project.project_id)}
                  onRemoveFolder={(folderId) =>
                    projects.removeFolderFromProject(folderId, project.project_id)
                  }
                  isFolderPending={(folderId) => pending.has(workspacePendingKey.folder(folderId))}
                />
              ))}
            </ul>
          </SortableContext>
        </DndContext>
      ) : null}
      {dialog?.kind === 'delete' ? (
        <HistoryDeleteDialog
          t={t}
          title={t('history.projects.deleteConfirmTitle', { name: dialog.project.display_name })}
          body={t('history.projects.deleteConfirmBody')}
          onCancel={() => closeDialog(dialog.project)}
          onConfirm={() => void confirmDelete(dialog.project)}
        />
      ) : null}
      {dialog?.kind === 'organizations' && settings ? (
        <HistoryProjectOrganizationDialog
          project={dialog.project}
          organizations={settings.organizations}
          t={t}
          onAddOrganization={projects.addOrganization}
          onSave={(organizationId) =>
            projects.updateProjectOrganizations(
              dialog.project.project_id,
              organizationId ? [organizationId] : []
            )
          }
          onClose={() => closeDialog(dialog.project)}
        />
      ) : null}
    </section>
  );
}
