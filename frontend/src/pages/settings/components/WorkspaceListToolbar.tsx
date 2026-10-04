import { Plus } from 'lucide-react';
import { useCallback, useRef, useState } from 'react';

import type { Translate } from '../types';
import { OrganizationManagerDialog } from './OrganizationManagerDialog';
import { ProjectCreatePopover, type ProjectDraft } from './ProjectCreatePopover';
import { workspaceFocusId } from './workspaceSettingsModel';
import type {
  FocusRequest,
  WorkspaceFolder,
  WorkspaceOrganization,
  WorkspaceProject,
} from './workspaceSettingsModel';

const EMPTY_PROJECT_DRAFT: ProjectDraft = { displayName: '', organizationId: null };

interface WorkspaceListToolbarProps {
  folders: WorkspaceFolder[];
  organizationCreateBusy: boolean;
  organizations: WorkspaceOrganization[];
  projectCreateBusy: boolean;
  projects: WorkspaceProject[];
  t: Translate;
  onCreateOrganization: (displayName: string) => Promise<string | null>;
  onCreateProject: (displayName: string, organizationIds: string[]) => Promise<boolean>;
  onDeleteOrganization: (organizationId: string, focus: FocusRequest) => Promise<void>;
  isOrganizationDeleteBusy: (organizationId: string) => boolean;
}

export function WorkspaceListToolbar(props: WorkspaceListToolbarProps) {
  const {
    folders,
    organizationCreateBusy,
    organizations,
    projectCreateBusy,
    projects,
    t,
    onCreateOrganization,
    onCreateProject,
    onDeleteOrganization,
  } = props;
  const projectTriggerRef = useRef<HTMLButtonElement>(null);
  const [isProjectPopoverOpen, setIsProjectPopoverOpen] = useState(false);
  const [isOrganizationDialogOpen, setIsOrganizationDialogOpen] = useState(false);
  // The draft outlives the popover so that managing organizations mid-draft does not discard it.
  const [projectDraft, setProjectDraft] = useState<ProjectDraft>(EMPTY_PROJECT_DRAFT);

  const closeProjectPopover = useCallback(() => {
    setIsProjectPopoverOpen(false);
    setProjectDraft(EMPTY_PROJECT_DRAFT);
  }, []);
  const openOrganizationDialog = useCallback(() => {
    setIsProjectPopoverOpen(false);
    setIsOrganizationDialogOpen(true);
  }, []);
  const closeOrganizationDialog = useCallback(() => {
    setIsOrganizationDialogOpen(false);
    setIsProjectPopoverOpen(true);
  }, []);
  const createOrganization = useCallback(
    async (displayName: string) => {
      const organizationId = await onCreateOrganization(displayName);
      if (organizationId) setProjectDraft((draft) => ({ ...draft, organizationId }));
      return organizationId !== null;
    },
    [onCreateOrganization]
  );
  // The manager can delete a drafted organization, so the selection is resolved against the
  // organizations the popover actually offers.
  const draftOrganizationId = organizations.some(
    (organization) => organization.organization_id === projectDraft.organizationId
  )
    ? projectDraft.organizationId
    : null;

  return (
    <div className="workspace-list-toolbar">
      {/* A closing modal returns focus to whatever was focused before it opened, so the dialog
          commits its close before the popover it reopens focuses its first field. */}
      <OrganizationManagerDialog
        createBusy={organizationCreateBusy}
        folders={folders}
        isOpen={isOrganizationDialogOpen}
        organizations={organizations}
        projects={projects}
        t={t}
        onClose={closeOrganizationDialog}
        onCreate={createOrganization}
        onDelete={onDeleteOrganization}
        isDeleteBusy={props.isOrganizationDeleteBusy}
      />

      <div className="workspace-project-create-anchor">
        <button
          ref={projectTriggerRef}
          id={workspaceFocusId.projectAdd}
          type="button"
          className="workspace-icon-button workspace-project-add"
          aria-label={t('settings.workspace.addProject')}
          title={t('settings.workspace.addProject')}
          aria-expanded={isProjectPopoverOpen}
          onClick={() =>
            isProjectPopoverOpen ? closeProjectPopover() : setIsProjectPopoverOpen(true)
          }
        >
          <Plus size={16} aria-hidden="true" />
        </button>
        <ProjectCreatePopover
          busy={projectCreateBusy}
          draft={{ ...projectDraft, organizationId: draftOrganizationId }}
          isOpen={isProjectPopoverOpen}
          organizations={organizations}
          triggerRef={projectTriggerRef}
          t={t}
          onDismiss={closeProjectPopover}
          onCreate={onCreateProject}
          onDraftChange={setProjectDraft}
          onManageOrganizations={openOrganizationDialog}
          onDeleteOrganization={onDeleteOrganization}
          isOrganizationDeleteBusy={props.isOrganizationDeleteBusy}
        />
      </div>
    </div>
  );
}
