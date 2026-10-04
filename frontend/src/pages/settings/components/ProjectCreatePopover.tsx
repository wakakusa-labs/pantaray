import type { RefObject } from 'react';

import type { Translate } from '../types';
import { useDismissablePopover } from '../useDismissablePopover';
import { OrganizationSelect } from './OrganizationSelect';
import {
  workspaceFocusId,
  type FocusRequest,
  type WorkspaceOrganization,
} from './workspaceSettingsModel';

export interface ProjectDraft {
  displayName: string;
  organizationId: string | null;
}

interface ProjectCreatePopoverProps {
  busy: boolean;
  draft: ProjectDraft;
  isOpen: boolean;
  organizations: WorkspaceOrganization[];
  triggerRef: RefObject<HTMLButtonElement>;
  t: Translate;
  onDismiss: () => void;
  onCreate: (displayName: string, organizationIds: string[]) => Promise<boolean>;
  onDraftChange: (draft: ProjectDraft) => void;
  onManageOrganizations: () => void;
  onDeleteOrganization: (organizationId: string, focus: FocusRequest) => Promise<void>;
  isOrganizationDeleteBusy: (organizationId: string) => boolean;
}

export function ProjectCreatePopover(props: ProjectCreatePopoverProps) {
  if (!props.isOpen) return null;
  return <OpenProjectCreatePopover {...props} />;
}

function OpenProjectCreatePopover(props: ProjectCreatePopoverProps) {
  const {
    busy,
    draft,
    organizations,
    onCreate,
    onDismiss,
    onDraftChange,
    onManageOrganizations,
    onDeleteOrganization,
    isOrganizationDeleteBusy,
    t,
    triggerRef,
  } = props;
  const { dismiss, popoverRef } = useDismissablePopover({ isOpen: true, triggerRef, onDismiss });

  const selectOrganization = (organizationId: string) => {
    onDraftChange({
      ...draft,
      organizationId: draft.organizationId === organizationId ? null : organizationId,
    });
  };

  const submit = async () => {
    const trimmedName = draft.displayName.trim();
    if (!trimmedName) return;
    const organizationIds = draft.organizationId ? [draft.organizationId] : [];
    if (await onCreate(trimmedName, organizationIds)) dismiss();
  };

  return (
    <div
      ref={popoverRef}
      className="workspace-project-create-popover"
      role="dialog"
      aria-label={t('settings.workspace.projectCreate.title')}
    >
      <label className="workspace-popover-field">
        <span>{t('settings.workspace.projectPlaceholder')}</span>
        <input
          className="workspace-input"
          value={draft.displayName}
          onChange={(event) => onDraftChange({ ...draft, displayName: event.target.value })}
          onKeyDown={(event) => {
            if (event.key === 'Enter') void submit();
          }}
        />
      </label>
      <OrganizationSelect
        organizations={organizations}
        selectedOrganizationId={draft.organizationId}
        emptyFocusKey={workspaceFocusId.organizationManagerOpen}
        t={t}
        onSelect={selectOrganization}
        onDelete={onDeleteOrganization}
        isDeleteBusy={isOrganizationDeleteBusy}
      />
      <button
        type="button"
        id={workspaceFocusId.organizationManagerOpen}
        className="workspace-popover-link"
        onClick={onManageOrganizations}
      >
        {t('settings.workspace.organizationManager.open')}
      </button>
      <button
        type="button"
        className="workspace-button"
        aria-busy={busy}
        disabled={!draft.displayName.trim()}
        onClick={() => void submit()}
      >
        {t('settings.workspace.addProject')}
      </button>
    </div>
  );
}
