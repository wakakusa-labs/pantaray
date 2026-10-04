import { useLayoutEffect, type ReactNode } from 'react';

import type { Translate } from '../types';
import {
  useWorkspaceSettingsController,
  workspacePendingKey,
  type WorkspacePendingKey,
} from '../useWorkspaceSettingsController';
import { useWorkspaceSelection } from '../useWorkspaceSelection';
import { WorkspaceListToolbar } from './WorkspaceListToolbar';
import { WorkspaceProjectDetail } from './WorkspaceProjectDetail';
import { WorkspaceProjectList } from './WorkspaceProjectList';
import { WorkspaceUnassignedFolders } from './WorkspaceUnassignedFolders';
import {
  getUnassignedFolders,
  successorFocusKey,
  workspaceFocusId,
} from './workspaceSettingsModel';
import './workspaceSettings.css';
import './workspaceProjectList.css';
import './workspaceOrganizations.css';
import './workspaceProjectDnd.css';

type WorkspaceSettingsSectionProps = {
  /** Shown at the top of the detail column (the app's AI-connection notice). */
  notice?: ReactNode;
  t: Translate;
};

/** Master-detail: projects (and the folders no project holds) on the left, the selection on
 * the right. */
export function WorkspaceSettingsSection({ notice, t }: WorkspaceSettingsSectionProps) {
  const controller = useWorkspaceSettingsController(t);
  const { clearFocusRequest, focusRequest, settings } = controller;
  const isPending = (key: WorkspacePendingKey) => controller.pending.has(key);
  const projectIds = settings?.projects.map((project) => project.project_id) ?? null;
  const unassignedCount = settings
    ? getUnassignedFolders(settings.folders, settings.projects).length
    : 0;
  const { selection, select } = useWorkspaceSelection(projectIds, unassignedCount > 0);
  const selectedProject =
    selection?.kind === 'project'
      ? settings?.projects.find((project) => project.project_id === selection.projectId)
      : undefined;

  useLayoutEffect(() => {
    const request = focusRequest;
    if (!request) return;
    document.getElementById(request.key)?.focus();
    clearFocusRequest(request);
  }, [clearFocusRequest, focusRequest]);

  return (
    <div className="app-split">
      <aside className="app-split-master workspace-master">
        <div className="workspace-master-header">
          <h2 className="app-split-title">{t('settings.workspace.title')}</h2>
          {settings ? (
            <WorkspaceListToolbar
              organizations={settings.organizations}
              projects={settings.projects}
              folders={settings.folders}
              organizationCreateBusy={isPending(workspacePendingKey.organizationCreate)}
              projectCreateBusy={isPending(workspacePendingKey.projectCreate)}
              t={t}
              onCreateOrganization={controller.addOrganization}
              onCreateProject={controller.createProject}
              onDeleteOrganization={controller.deleteOrganization}
              isOrganizationDeleteBusy={(organizationId) =>
                isPending(workspacePendingKey.organizationDelete(organizationId))
              }
            />
          ) : null}
        </div>
        {settings && settings.projects.length === 0 ? (
          <p className="workspace-list-toolbar-hint">{t('settings.workspace.gettingStarted')}</p>
        ) : null}
        {settings ? (
          <>
            <div className="workspace-master-scroll">
              <WorkspaceProjectList
                projects={settings.projects}
                folders={settings.folders}
                disabled={controller.isProjectStructurePending}
                dragController={controller.dragController}
                selectedProjectId={selectedProject?.project_id ?? null}
                t={t}
                onSelect={(projectId) => select({ kind: 'project', projectId })}
              />
            </div>
            {unassignedCount > 0 ? (
              <div className="workspace-master-item workspace-master-unassigned">
                <button
                  type="button"
                  className="workspace-master-select"
                  aria-current={selection?.kind === 'unassigned' ? 'true' : undefined}
                  onClick={() => select({ kind: 'unassigned' })}
                >
                  {t('settings.workspace.unassigned.title')}
                </button>
                <span className="workspace-project-folder-count">{unassignedCount}</span>
              </div>
            ) : null}
          </>
        ) : null}
      </aside>

      <section className="app-split-detail">
        {notice}
        {settings === null ? (
          controller.showLoading ? (
            <div className="history-loading">{t('common.loading')}</div>
          ) : (
            // Nothing was read, so there is no workspace to edit here: showing the
            // empty lists would offer defaults nobody confirmed as the current ones.
            <div className="history-error" role="alert">
              {controller.errorMessage}
            </div>
          )
        ) : (
          <>
            {controller.errorMessage ? (
              <div className="workspace-status-error" role="alert">
                {controller.errorMessage}
              </div>
            ) : null}
            {selectedProject ? (
              <WorkspaceProjectDetail
                key={selectedProject.project_id}
                project={selectedProject}
                organizations={settings.organizations}
                folders={settings.folders.filter((folder) =>
                  folder.project_ids.includes(selectedProject.project_id)
                )}
                t={t}
                onCreateFolder={controller.createFolder}
                onCreateOrganization={controller.addOrganization}
                onDeleteFolder={controller.deleteFolder}
                onDeleteProject={(projectId) =>
                  controller.deleteProject(projectId, {
                    // The project that takes its place is selected (useWorkspaceSelection).
                    onSuccess: successorFocusKey(
                      settings.projects.map((project) => project.project_id),
                      projectId,
                      workspaceFocusId.projectHeading,
                      workspaceFocusId.projectAdd
                    ),
                    onFailure: workspaceFocusId.projectDelete(projectId),
                  })
                }
                onSelectFolder={controller.selectFolder}
                onUpdateOrganizations={controller.updateProjectOrganizations}
                createFolderBusy={isPending(
                  workspacePendingKey.folderCreate(selectedProject.project_id)
                )}
                deleteProjectBusy={isPending(
                  workspacePendingKey.projectDelete(selectedProject.project_id)
                )}
                isDeleteFolderBusy={(folderId) =>
                  isPending(workspacePendingKey.folderDelete(folderId))
                }
                organizationCreateBusy={isPending(workspacePendingKey.organizationCreate)}
                projectLinksBusy={isPending(
                  workspacePendingKey.projectLinks(selectedProject.project_id)
                )}
              />
            ) : selection?.kind === 'unassigned' ? (
              <WorkspaceUnassignedFolders
                organizations={settings.organizations}
                projects={settings.projects}
                folders={settings.folders}
                t={t}
                onAssign={controller.assignFolderToProject}
                onDeleteFolder={controller.deleteFolder}
                isAssignBusy={(folderId) => isPending(workspacePendingKey.folderLinks(folderId))}
                isDeleteFolderBusy={(folderId) =>
                  isPending(workspacePendingKey.folderDelete(folderId))
                }
              />
            ) : null}
          </>
        )}
      </section>
    </div>
  );
}
