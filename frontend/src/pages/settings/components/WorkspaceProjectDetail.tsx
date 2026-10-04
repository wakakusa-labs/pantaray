import { Plus, Trash2 } from 'lucide-react';

import type { Translate } from '../types';
import { WorkspaceFolderRow } from './WorkspaceFolderRow';
import { ProjectOrganizationEditor } from './ProjectOrganizationEditor';
import {
  displayNameFromPath,
  successorFocusKey,
  workspaceFocusId,
  type FocusRequest,
  type WorkspaceFolder,
  type WorkspaceFolderCreateInput,
  type WorkspaceOrganization,
  type WorkspaceProject,
} from './workspaceSettingsModel';

interface WorkspaceProjectDetailProps {
  folders: WorkspaceFolder[];
  organizations: WorkspaceOrganization[];
  project: WorkspaceProject;
  t: Translate;
  onCreateFolder: (input: WorkspaceFolderCreateInput) => Promise<boolean>;
  onCreateOrganization: (displayName: string) => Promise<string | null>;
  onDeleteOrganization: (organizationId: string, focus: FocusRequest) => Promise<void>;
  onDeleteFolder: (folderId: string, focus: FocusRequest) => Promise<void>;
  onDeleteProject: (projectId: string) => Promise<void>;
  onSelectFolder: () => Promise<string | null>;
  onUpdateOrganizations: (
    projectId: string,
    organizationIds: string[],
    focus: FocusRequest
  ) => Promise<boolean>;
  createFolderBusy: boolean;
  deleteProjectBusy: boolean;
  isDeleteFolderBusy: (folderId: string) => boolean;
  isOrganizationDeleteBusy: (organizationId: string) => boolean;
  organizationCreateBusy: boolean;
  projectLinksBusy: boolean;
}

/** The selected project: its name, organizations and folders, and the actions on them. */
export function WorkspaceProjectDetail(props: WorkspaceProjectDetailProps) {
  const chooseFolder = async () => {
    const selectedPath = await props.onSelectFolder();
    if (!selectedPath) return;
    await props.onCreateFolder({
      displayName: displayNameFromPath(selectedPath),
      realPath: selectedPath,
      organizationIds: [],
      projectIds: [props.project.project_id],
    });
  };

  return (
    <article className="workspace-detail-body">
      <div className="workspace-detail-header">
        {/* Focus lands here when the project before it is deleted. */}
        <h1
          id={workspaceFocusId.projectHeading(props.project.project_id)}
          className="workspace-detail-title"
          tabIndex={-1}
        >
          {props.project.display_name}
        </h1>
        <ProjectOrganizationEditor
          busy={props.projectLinksBusy}
          createBusy={props.organizationCreateBusy}
          organizations={props.organizations}
          project={props.project}
          t={props.t}
          onCreateOrganization={props.onCreateOrganization}
          onDeleteOrganization={props.onDeleteOrganization}
          onUpdate={props.onUpdateOrganizations}
          isOrganizationDeleteBusy={props.isOrganizationDeleteBusy}
        />

        <button
          type="button"
          className="workspace-icon-button workspace-project-delete"
          id={workspaceFocusId.projectDelete(props.project.project_id)}
          aria-label={`${props.t('common.delete')} ${props.project.display_name}`}
          aria-busy={props.deleteProjectBusy}
          title={props.t('common.delete')}
          onClick={() => void props.onDeleteProject(props.project.project_id)}
        >
          <Trash2 size={14} aria-hidden="true" />
        </button>
      </div>

      <div className="workspace-detail-panel">
        <div className="workspace-detail-folders">
          {props.folders.map((folder) => (
            <WorkspaceFolderRow
              key={folder.folder_id}
              folder={folder}
              busy={props.isDeleteFolderBusy(folder.folder_id)}
              deleteButtonId={workspaceFocusId.projectFolderDelete(
                props.project.project_id,
                folder.folder_id
              )}
              t={props.t}
              onDelete={(folderId) => {
                const folderIds = props.folders.map((candidate) => candidate.folder_id);
                return props.onDeleteFolder(folderId, {
                  onSuccess: successorFocusKey(
                    folderIds,
                    folderId,
                    (successorId) =>
                      workspaceFocusId.projectFolderDelete(props.project.project_id, successorId),
                    workspaceFocusId.projectFolderAdd(props.project.project_id)
                  ),
                  onFailure: workspaceFocusId.projectFolderDelete(
                    props.project.project_id,
                    folderId
                  ),
                });
              }}
            />
          ))}
        </div>
        <div className="workspace-detail-panel-footer">
          <button
            type="button"
            id={workspaceFocusId.projectFolderAdd(props.project.project_id)}
            aria-busy={props.createFolderBusy}
            onClick={() => void chooseFolder()}
            className="workspace-button workspace-button-primary workspace-project-add-folder"
          >
            <Plus size={14} aria-hidden="true" />
            {props.t('settings.workspace.selectFolder')}
          </button>
        </div>
      </div>
    </article>
  );
}
