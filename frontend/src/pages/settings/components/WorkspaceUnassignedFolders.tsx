import { useState } from 'react';

import type { Translate } from '../types';
import { WorkspaceFolderRow } from './WorkspaceFolderRow';
import {
  getUnassignedFolders,
  resolveFolderOrganizations,
  successorFocusKey,
  workspaceFocusId,
  type FocusRequest,
  type WorkspaceFolder,
  type WorkspaceOrganization,
  type WorkspaceProject,
} from './workspaceSettingsModel';

interface WorkspaceUnassignedFoldersProps {
  folders: WorkspaceFolder[];
  organizations: WorkspaceOrganization[];
  projects: WorkspaceProject[];
  t: Translate;
  onAssign: (folderId: string, projectId: string, focus: FocusRequest) => Promise<boolean>;
  onDeleteFolder: (folderId: string, focus: FocusRequest) => Promise<void>;
  isAssignBusy: (folderId: string) => boolean;
  isDeleteFolderBusy: (folderId: string) => boolean;
}

export function WorkspaceUnassignedFolders(props: WorkspaceUnassignedFoldersProps) {
  const folders = getUnassignedFolders(props.folders, props.projects);

  return (
    <section className="workspace-detail-body workspace-unassigned-folders">
      <h1 className="workspace-detail-title">{props.t('settings.workspace.unassigned.title')}</h1>
      <div className="workspace-detail-panel workspace-unassigned-list">
        {folders.map((folder) => (
          <UnassignedFolder
            key={folder.folder_id}
            folder={folder}
            folderIds={folders.map((candidate) => candidate.folder_id)}
            organizations={props.organizations}
            projects={props.projects}
            assignBusy={props.isAssignBusy(folder.folder_id)}
            deleteBusy={props.isDeleteFolderBusy(folder.folder_id)}
            t={props.t}
            onAssign={props.onAssign}
            onDelete={props.onDeleteFolder}
          />
        ))}
      </div>
    </section>
  );
}

function UnassignedFolder(props: {
  assignBusy: boolean;
  deleteBusy: boolean;
  folder: WorkspaceFolder;
  folderIds: string[];
  organizations: WorkspaceOrganization[];
  projects: WorkspaceProject[];
  t: Translate;
  onAssign: (folderId: string, projectId: string, focus: FocusRequest) => Promise<boolean>;
  onDelete: (folderId: string, focus: FocusRequest) => Promise<void>;
}) {
  const [projectId, setProjectId] = useState('');
  const organizations = resolveFolderOrganizations(props.folder, props.organizations);
  const sortedProjects = [...props.projects].sort(
    (left, right) =>
      left.display_name.localeCompare(right.display_name) ||
      left.project_id.localeCompare(right.project_id)
  );

  const assign = async () => {
    if (!projectId) return;
    if (
      await props.onAssign(
        props.folder.folder_id,
        projectId,
        focusAfterRemoval(
          props.folderIds,
          props.folder.folder_id,
          workspaceFocusId.unassignedAssign
        )
      )
    ) {
      setProjectId('');
    }
  };

  return (
    <div className="workspace-unassigned-folder">
      <WorkspaceFolderRow
        folder={props.folder}
        busy={props.deleteBusy}
        deleteButtonId={workspaceFocusId.unassignedFolderDelete(props.folder.folder_id)}
        t={props.t}
        onDelete={(folderId) =>
          props.onDelete(
            folderId,
            focusAfterRemoval(props.folderIds, folderId, workspaceFocusId.unassignedFolderDelete)
          )
        }
      />
      {organizations.length > 0 ? (
        <div
          className="workspace-unassigned-organization-badges"
          aria-label={props.t('settings.workspace.organizations')}
        >
          {organizations.map((organization) => (
            <span key={organization.organization_id} className="workspace-organization-badge">
              {organization.display_name}
            </span>
          ))}
        </div>
      ) : null}
      <div className="workspace-folder-assignment">
        <select
          value={projectId}
          disabled={sortedProjects.length === 0}
          aria-label={`${props.t('settings.workspace.unassigned.project')} ${props.folder.display_name}`}
          onChange={(event) => setProjectId(event.target.value)}
        >
          <option value="">{props.t('settings.workspace.unassigned.chooseProject')}</option>
          {sortedProjects.map((project) => (
            <option key={project.project_id} value={project.project_id}>
              {project.display_name}
            </option>
          ))}
        </select>
        <button
          type="button"
          id={workspaceFocusId.unassignedAssign(props.folder.folder_id)}
          className="workspace-button"
          aria-busy={props.assignBusy}
          disabled={!projectId}
          onClick={() => void assign()}
        >
          {props.t('settings.workspace.unassigned.assign')}
        </button>
      </div>
    </div>
  );
}

function focusAfterRemoval(
  folderIds: string[],
  folderId: string,
  toKey: (folderId: string) => string
): FocusRequest {
  return {
    onSuccess: successorFocusKey(folderIds, folderId, toKey, workspaceFocusId.projectAdd),
    onFailure: toKey(folderId),
  };
}
