import { Plus, X } from 'lucide-react';
import { useCallback, useRef, useState } from 'react';
import { flushSync } from 'react-dom';

import type { Translate } from '../types';
import { useDismissablePopover } from '../useDismissablePopover';
import { OrganizationSelect } from './OrganizationSelect';
import { InlineTextForm } from './WorkspaceSettingsFormControls';
import {
  resolveProjectOrganizations,
  successorFocusKey,
  workspaceFocusId,
  type FocusRequest,
  type WorkspaceOrganization,
  type WorkspaceProject,
} from './workspaceSettingsModel';

interface ProjectOrganizationEditorProps {
  busy: boolean;
  createBusy: boolean;
  organizations: WorkspaceOrganization[];
  project: WorkspaceProject;
  t: Translate;
  onCreateOrganization: (displayName: string) => Promise<string | null>;
  onDeleteOrganization: (organizationId: string, focus: FocusRequest) => Promise<void>;
  onUpdate: (projectId: string, organizationIds: string[], focus: FocusRequest) => Promise<boolean>;
  isOrganizationDeleteBusy: (organizationId: string) => boolean;
}

export function ProjectOrganizationEditor(props: ProjectOrganizationEditorProps) {
  const addTriggerRef = useRef<HTMLButtonElement>(null);
  const [isOpen, setIsOpen] = useState(false);
  const organizations = resolveProjectOrganizations(props.project, props.organizations);
  const pickerLabel = props.t('settings.workspace.projectOrganization.select', {
    name: props.project.display_name,
  });

  const closePicker = useCallback(() => setIsOpen(false), []);
  const { dismiss, popoverRef } = useDismissablePopover({
    isOpen,
    triggerRef: addTriggerRef,
    onDismiss: closePicker,
  });

  const togglePicker = () => {
    if (isOpen) dismiss();
    else setIsOpen(true);
  };

  const selectOrganization = async (organizationId: string) => {
    dismiss();
    await props.onUpdate(props.project.project_id, [organizationId], {
      onSuccess: workspaceFocusId.projectOrganizationRemove(
        props.project.project_id,
        organizationId
      ),
      onFailure: workspaceFocusId.projectOrganizationAdd(props.project.project_id),
    });
  };

  const createOrganization = async (displayName: string) => {
    const organizationId = await props.onCreateOrganization(displayName);
    if (organizationId) await selectOrganization(organizationId);
  };

  const removeOrganization = async (organizationId: string) => {
    await props.onUpdate(
      props.project.project_id,
      props.project.organization_ids.filter((currentId) => currentId !== organizationId),
      {
        onSuccess: successorFocusKey(
          organizations.map((organization) => organization.organization_id),
          organizationId,
          (successorId) =>
            workspaceFocusId.projectOrganizationRemove(props.project.project_id, successorId),
          workspaceFocusId.projectOrganizationAdd(props.project.project_id)
        ),
        onFailure: workspaceFocusId.projectOrganizationRemove(
          props.project.project_id,
          organizationId
        ),
      }
    );
  };

  return (
    <div className="workspace-project-organization-editor">
      <div
        className="workspace-project-badges"
        aria-label={props.t('settings.workspace.organizations')}
      >
        {organizations.map((organization) => (
          <span key={organization.organization_id} className="workspace-organization-badge">
            <span className="workspace-organization-badge-label">{organization.display_name}</span>
            <button
              type="button"
              id={workspaceFocusId.projectOrganizationRemove(
                props.project.project_id,
                organization.organization_id
              )}
              className="workspace-organization-remove"
              aria-busy={props.busy}
              aria-label={props.t('settings.workspace.projectOrganization.remove', {
                name: organization.display_name,
              })}
              onKeyDown={(event) => {
                // Choosing or creating an organization with Enter moves focus here, so the
                // repeats of a held Enter must not remove the organization just added.
                if (event.key === 'Enter' && event.repeat) event.preventDefault();
              }}
              onClick={() => void removeOrganization(organization.organization_id)}
            >
              <X size={9} strokeWidth={3} aria-hidden="true" />
            </button>
          </span>
        ))}
        {organizations.length === 0 ? (
          <button
            ref={addTriggerRef}
            type="button"
            id={workspaceFocusId.projectOrganizationAdd(props.project.project_id)}
            className="workspace-organization-badge workspace-organization-add-trigger"
            aria-busy={props.busy}
            aria-label={pickerLabel}
            aria-haspopup="dialog"
            aria-expanded={isOpen}
            onClick={togglePicker}
          >
            <Plus size={11} strokeWidth={2.5} aria-hidden="true" />
          </button>
        ) : null}
      </div>

      {isOpen ? (
        <div
          ref={popoverRef}
          className="workspace-project-organization-popover"
          role="dialog"
          aria-label={pickerLabel}
          tabIndex={-1}
        >
          <OrganizationPicker
            createBusy={props.createBusy}
            organizations={props.organizations}
            t={props.t}
            onCreate={createOrganization}
            onDelete={props.onDeleteOrganization}
            onSelect={(organizationId) => void selectOrganization(organizationId)}
            isDeleteBusy={props.isOrganizationDeleteBusy}
          />
        </div>
      ) : null}
    </div>
  );
}

interface OrganizationPickerProps {
  createBusy: boolean;
  organizations: WorkspaceOrganization[];
  t: Translate;
  onCreate: (displayName: string) => Promise<void>;
  onDelete: (organizationId: string, focus: FocusRequest) => Promise<void>;
  onSelect: (organizationId: string) => void;
  isDeleteBusy: (organizationId: string) => boolean;
}

// Mounted only while the popover is open, so an abandoned draft is discarded with it.
function OrganizationPicker(props: OrganizationPickerProps) {
  const createOptionRef = useRef<HTMLButtonElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  // `null` is the list; a string is the name being typed for a new organization.
  const [draftName, setDraftName] = useState<string | null>(null);

  // The control that should take focus only exists after the swap renders.
  const startCreating = () => {
    flushSync(() => setDraftName(''));
    inputRef.current?.focus();
  };
  const cancelCreating = () => {
    flushSync(() => setDraftName(null));
    createOptionRef.current?.focus();
  };

  if (draftName === null) {
    return (
      <>
        <OrganizationSelect
          organizations={props.organizations}
          selectedOrganizationId={null}
          emptyFocusKey={workspaceFocusId.organizationCreateOption}
          t={props.t}
          onSelect={props.onSelect}
          onDelete={props.onDelete}
          isDeleteBusy={props.isDeleteBusy}
        />
        <button
          ref={createOptionRef}
          type="button"
          id={workspaceFocusId.organizationCreateOption}
          className="workspace-organization-create-option"
          onClick={startCreating}
        >
          {props.t('settings.workspace.addOrganization')}
        </button>
      </>
    );
  }

  return (
    <div
      onKeyDown={(event) => {
        if (event.key !== 'Escape') return;
        // Escape leaves the draft, not the popover, whose own listener sits on the document.
        event.stopPropagation();
        cancelCreating();
      }}
    >
      <InlineTextForm
        buttonLabel={props.t('settings.workspace.addOrganization')}
        busy={props.createBusy}
        disabled={!draftName.trim()}
        onSubmit={() => void props.onCreate(draftName)}
        placeholder={props.t('settings.workspace.organizationPlaceholder')}
        value={draftName}
        onChange={setDraftName}
        inputRef={inputRef}
      />
    </div>
  );
}
