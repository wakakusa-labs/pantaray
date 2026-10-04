import { Check, Trash2 } from 'lucide-react';

import type { Translate } from '../types';
import {
  compareOrganizations,
  successorFocusKey,
  workspaceFocusId,
  type FocusKey,
  type FocusRequest,
  type WorkspaceOrganization,
} from './workspaceSettingsModel';

interface OrganizationSelectProps {
  organizations: WorkspaceOrganization[];
  selectedOrganizationId: string | null;
  /** Takes focus when the last organization is deleted and the list disappears. */
  emptyFocusKey: FocusKey;
  t: Translate;
  onSelect: (organizationId: string) => void;
  onDelete: (organizationId: string, focus: FocusRequest) => Promise<void>;
  isDeleteBusy: (organizationId: string) => boolean;
}

export function OrganizationSelect(props: OrganizationSelectProps) {
  const sortedOrganizations = [...props.organizations].sort(compareOrganizations);

  if (sortedOrganizations.length === 0) return null;

  const deleteOrganization = (organizationId: string) => {
    void props.onDelete(organizationId, {
      onSuccess: successorFocusKey(
        sortedOrganizations.map((organization) => organization.organization_id),
        organizationId,
        workspaceFocusId.organizationOptionDelete,
        props.emptyFocusKey
      ),
      onFailure: workspaceFocusId.organizationOptionDelete(organizationId),
    });
  };

  return (
    <fieldset className="workspace-organization-select">
      <legend>{props.t('settings.workspace.organizations')}</legend>
      {sortedOrganizations.map((organization) => {
        const isSelected = props.selectedOrganizationId === organization.organization_id;
        return (
          <div key={organization.organization_id} className="workspace-organization-option">
            <button
              type="button"
              className="workspace-organization-option-select"
              aria-pressed={isSelected}
              onClick={() => props.onSelect(organization.organization_id)}
            >
              <span>{organization.display_name}</span>
              {isSelected ? <Check size={13} strokeWidth={2.5} aria-hidden="true" /> : null}
            </button>
            <button
              type="button"
              className="workspace-icon-button workspace-organization-option-delete"
              id={workspaceFocusId.organizationOptionDelete(organization.organization_id)}
              aria-label={`${props.t('common.delete')} ${organization.display_name}`}
              aria-busy={props.isDeleteBusy(organization.organization_id)}
              title={props.t('common.delete')}
              onClick={() => deleteOrganization(organization.organization_id)}
            >
              <Trash2 size={13} aria-hidden="true" />
            </button>
          </div>
        );
      })}
    </fieldset>
  );
}
