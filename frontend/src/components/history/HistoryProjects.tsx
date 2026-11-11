import { DndContext } from '@dnd-kit/core';
import { SortableContext, verticalListSortingStrategy } from '@dnd-kit/sortable';
import { Plus } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import { flushSync } from 'react-dom';

import { ProjectGoalDialog } from '@/components/todo/progress/ProjectGoalDialog';
import type { useI18n } from '@/context/useI18n';
import {
  workspacePendingKey,
  type useWorkspaceSettingsController,
} from '@/pages/settings/useWorkspaceSettingsController';
import {
  resolveProjectOrganizations,
  type WorkspaceProject,
} from '@/pages/settings/components/workspaceSettingsModel';

import type { TodoScopeQuery } from '../../../electron/src/todos/todoContracts';
import { HistoryDeleteDialog } from './HistoryDeleteDialog';
import { HistoryProjectOrganizationDialog } from './HistoryProjectOrganizationDialog';
import { HistoryProjectRow, ProjectNameField } from './HistoryProjectRow';
import './historyProjects.css';

export type WorkspaceProjects = Pick<
  ReturnType<typeof useWorkspaceSettingsController>,
  | 'settings'
  | 'errorMessage'
  | 'pending'
  | 'createProject'
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

type OpenDialog = { kind: 'delete' | 'organizations' | 'goal'; project: WorkspaceProject };

/** What the TODO list adds to the projects: each one selects its TODOs. */
export type ProjectTodos = {
  /** The project whose TODOs are in view, or null. */
  selectedProjectId: string | null;
  /** Open TODOs by project id, for the week in view. */
  openCounts: ReadonlyMap<string, number>;
  onSelectProject: (projectId: string) => void;
  /** The rail's scope: ＋ creates the project in its organization, or in none. */
  scope: TodoScopeQuery;
};

const menuButtonId = (projectId: string) => `history-project-menu:${projectId}`;

/**
 * The sidebar's projects, managed in place: ＋ names a new project, which then takes its folders;
 * with the TODO list it is made in the rail's organization in view, so ＋ is hidden while all of
 * them are. Each row's menu renames it, adds folders, sets its organization (and, with the TODO list, its
 * goal), or deletes it, and rows are dragged into order. With the TODO list a row's name shows
 * that project's TODOs; without it the name opens its folders, as before the TODO list. The
 * conversations below are not filtered by it.
 */
export function HistoryProjects({
  projects,
  creating,
  onCreatingChange,
  todos,
  t,
}: {
  projects: WorkspaceProjects;
  /** Whether the new project's name field shows; the composers' add opens it as well. */
  creating: boolean;
  onCreatingChange: (creating: boolean) => void;
  /** Null without the TODO list. */
  todos: ProjectTodos | null;
  t: ReturnType<typeof useI18n>['t'];
}) {
  const { settings, errorMessage, pending, dragController } = projects;
  const addButtonRef = useRef<HTMLButtonElement>(null);
  const [dialog, setDialog] = useState<OpenDialog | null>(null);
  const [renamingId, setRenamingId] = useState<string | null>(null);
  const [createdId, setCreatedId] = useState<string | null>(null);
  // Without the TODO list, and from the composers on all organizations, a project has none.
  const scope = todos?.scope;
  const create = async (name: string) => {
    const project = await projects.createProject(
      name,
      scope?.scope === 'organization' ? (scope.organization_id ?? null) : null
    );
    if (!project) return;
    setCreatedId(project.project_id);
    onCreatingChange(false);
  };
  const cancelCreate = () => {
    flushSync(() => onCreatingChange(false));
    addButtonRef.current?.focus();
  };
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
      ...(todos
        ? [
            {
              label: t('history.projects.setGoal'),
              onSelect: () => setDialog({ kind: 'goal' as const, project }),
            },
          ]
        : []),
      { label: t('common.delete'), onSelect: () => setDialog({ kind: 'delete', project }) },
    ];
  };

  return (
    <section className="history-projects" aria-labelledby="history-projects-title">
      <div className="history-projects__head">
        <h2 id="history-projects-title">{t('history.projects.title')}</h2>
        {scope?.scope === 'all' ? null : (
          <button
            type="button"
            ref={addButtonRef}
            className="history-projects__add"
            aria-label={t('history.projects.add')}
            data-tooltip={t('history.projects.add')}
            aria-busy={pending.has(workspacePendingKey.projectCreate)}
            disabled={settings === null}
            onClick={() => onCreatingChange(true)}
          >
            <Plus size={15} aria-hidden="true" />
          </button>
        )}
      </div>
      {errorMessage ? (
        <div className="history-error" role="alert">
          {errorMessage}
        </div>
      ) : null}
      {settings && (settings.projects.length > 0 || creating) ? (
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
              {creating ? (
                <li className="history-item history-project history-project--new">
                  <ProjectNameField
                    initialName=""
                    label={t('history.projects.nameLabel')}
                    saveOnBlur={false}
                    onSave={create}
                    onCancel={cancelCreate}
                  />
                </li>
              ) : null}
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
                  todo={
                    todos && {
                      selected: todos.selectedProjectId === project.project_id,
                      openCount: todos.openCounts.get(project.project_id) ?? 0,
                      onSelect: () => todos.onSelectProject(project.project_id),
                    }
                  }
                  t={t}
                  onRename={(name) => projects.renameProject(project.project_id, name)}
                  onRenameEnd={() => endRename(project.project_id)}
                  onRemoveFolder={(folderId) =>
                    projects.removeFolderFromProject(folderId, project.project_id)
                  }
                  isFolderPending={(folderId) => pending.has(workspacePendingKey.folder(folderId))}
                  onAddFolder={() => void projects.addFolderToProject(project.project_id)}
                  onOpenFolder={(folderId) => void projects.openFolder(folderId)}
                  justCreated={createdId === project.project_id}
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
          body={t(
            todos ? 'history.projects.deleteConfirmBodyTodos' : 'history.projects.deleteConfirmBody'
          )}
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
      {dialog?.kind === 'goal' ? (
        <ProjectGoalDialog
          projectId={dialog.project.project_id}
          projectName={dialog.project.display_name}
          onClose={() => closeDialog(dialog.project)}
        />
      ) : null}
    </section>
  );
}
