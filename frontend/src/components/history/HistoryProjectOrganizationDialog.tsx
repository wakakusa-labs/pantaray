import { useEffect, useRef, useState } from 'react';

import type { useI18n } from '@/context/useI18n';
import type {
  WorkspaceOrganization,
  WorkspaceProject,
} from '@/pages/settings/components/workspaceSettingsModel';

const NO_ORGANIZATION = '';

/**
 * Moves the project to an existing organization, or to none; organizations are added from the
 * rail. A project holds
 * at most one organization (the backend refuses more), so the choices are radio buttons. Mounted
 * only while open; the caller moves focus back once it is gone.
 */
export function HistoryProjectOrganizationDialog({
  project,
  organizations,
  t,
  onSave,
  onClose,
}: {
  project: WorkspaceProject;
  organizations: WorkspaceOrganization[];
  t: ReturnType<typeof useI18n>['t'];
  onSave: (organizationId: string | null) => Promise<boolean>;
  onClose: () => void;
}) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  const [chosen, setChosen] = useState(project.organization_ids[0] ?? NO_ORGANIZATION);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    const dialog = dialogRef.current;
    if (dialog && !dialog.open) dialog.showModal();
  }, []);

  const save = async () => {
    setBusy(true);
    const saved = await onSave(chosen === NO_ORGANIZATION ? null : chosen);
    setBusy(false);
    if (saved) onClose();
  };

  const choices = [
    { id: NO_ORGANIZATION, name: t('history.projects.organizationNone') },
    ...organizations.map((organization) => ({
      id: organization.organization_id,
      name: organization.display_name,
    })),
  ];
  return (
    <dialog
      ref={dialogRef}
      className="history-delete-dialog history-project-organizations"
      aria-labelledby="history-project-organizations-title"
      onCancel={(event) => {
        event.preventDefault();
        onClose();
      }}
    >
      <h2 id="history-project-organizations-title">
        {t('history.projects.organizationTitle', { name: project.display_name })}
      </h2>
      <fieldset className="history-project-organizations__choices" aria-busy={busy}>
        <legend>{t('history.projects.organization')}</legend>
        {choices.map((choice) => (
          <label key={choice.id} className="history-project-organizations__choice">
            <input
              type="radio"
              name="history-project-organization"
              value={choice.id}
              checked={chosen === choice.id}
              onChange={() => setChosen(choice.id)}
            />
            {choice.name}
          </label>
        ))}
      </fieldset>
      <div className="history-delete-dialog__actions">
        <button type="button" className="history-filter-button" onClick={onClose}>
          {t('common.cancel')}
        </button>
        <button
          type="button"
          className="history-filter-button history-project-organizations__save"
          disabled={busy}
          onClick={() => void save()}
        >
          {t('common.save')}
        </button>
      </div>
    </dialog>
  );
}
