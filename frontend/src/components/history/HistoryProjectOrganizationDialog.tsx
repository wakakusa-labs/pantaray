import { useEffect, useRef, useState } from 'react';

import type { useI18n } from '@/context/useI18n';
import type {
  WorkspaceOrganization,
  WorkspaceProject,
} from '@/pages/settings/components/workspaceSettingsModel';

const NO_ORGANIZATION = '';

/**
 * Chooses the project's organization, or none, and can add a new one by name. A project holds
 * at most one organization (the backend refuses more), so the choices are radio buttons. Mounted
 * only while open; the caller moves focus back once it is gone.
 */
export function HistoryProjectOrganizationDialog({
  project,
  organizations,
  t,
  onAddOrganization,
  onSave,
  onClose,
}: {
  project: WorkspaceProject;
  organizations: WorkspaceOrganization[];
  t: ReturnType<typeof useI18n>['t'];
  onAddOrganization: (displayName: string) => Promise<string | null>;
  onSave: (organizationId: string | null) => Promise<boolean>;
  onClose: () => void;
}) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  const [chosen, setChosen] = useState(project.organization_ids[0] ?? NO_ORGANIZATION);
  const [newName, setNewName] = useState('');
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    const dialog = dialogRef.current;
    if (dialog && !dialog.open) dialog.showModal();
  }, []);

  const addOrganization = async () => {
    if (!newName.trim()) return;
    setBusy(true);
    const organizationId = await onAddOrganization(newName);
    setBusy(false);
    if (!organizationId) return;
    setChosen(organizationId);
    setNewName('');
  };
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
      <form
        className="history-project-organizations__new"
        onSubmit={(event) => {
          event.preventDefault();
          void addOrganization();
        }}
      >
        <input
          type="text"
          value={newName}
          aria-label={t('history.projects.organizationNew')}
          placeholder={t('history.projects.organizationNew')}
          onChange={(event) => setNewName(event.target.value)}
        />
        <button type="submit" className="history-filter-button" disabled={busy || !newName.trim()}>
          {t('history.projects.organizationAdd')}
        </button>
      </form>
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
