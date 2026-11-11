import { useEffect, useId, useRef, useState } from 'react';

import { useI18n } from '@/context/useI18n';
import type { WorkspaceOrganization } from '@/pages/settings/components/workspaceSettingsModel';

import { organizationIconFrom } from './organizationIcon';

/** Clear of the control the dialog opens beside. */
const ANCHOR_GAP_PX = 12;

/**
 * Adds an organization, or edits one: its name and an optional icon image, shrunk to a small
 * square here. A name another organization has is refused before anything is sent. Mounted only
 * while open, as a modal beside the control that opened it; the caller moves focus back once it
 * is gone.
 */
export function OrganizationDialog({
  organization,
  organizations,
  anchorId,
  errorMessage,
  onSave,
  onClose,
}: {
  /** Null to add one. */
  organization: WorkspaceOrganization | null;
  organizations: readonly WorkspaceOrganization[];
  /** The control it opens beside. */
  anchorId: string;
  /** Why the last save failed, as the workspace said. */
  errorMessage: string | null;
  /** False when it was not saved; the dialog stays to be fixed. */
  onSave: (displayName: string, iconDataUrl: string | null) => Promise<boolean>;
  onClose: () => void;
}) {
  const { t } = useI18n();
  const dialogRef = useRef<HTMLDialogElement>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const nameRef = useRef<HTMLInputElement>(null);
  const titleId = useId();
  const errorId = useId();
  const [name, setName] = useState(organization?.display_name ?? '');
  const [icon, setIcon] = useState(organization?.icon_data_url ?? null);
  const [error, setError] = useState<string | null>(null);
  const [nameInvalid, setNameInvalid] = useState(false);
  const [busy, setBusy] = useState(false);
  const [saveFailed, setSaveFailed] = useState(false);

  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog || dialog.open) return;
    const anchor = document.getElementById(anchorId)?.getBoundingClientRect();
    if (anchor) {
      dialog.style.top = `${anchor.top}px`;
      dialog.style.left = `${anchor.right + ANCHOR_GAP_PX}px`;
    }
    dialog.showModal();
    // The modal focuses its first control, the image; the name is what is asked first.
    nameRef.current?.focus();
  }, [anchorId]);

  const pickIcon = async (file: File | undefined) => {
    if (!file) return;
    const picked = await organizationIconFrom(file);
    if (picked.kind === 'ok') {
      setIcon(picked.dataUrl);
      setError(null);
    } else {
      setError(
        t(
          picked.kind === 'unreadable'
            ? 'todo.organization.iconUnreadable'
            : 'todo.organization.iconTooLarge'
        )
      );
    }
    setNameInvalid(false);
  };
  const save = async () => {
    const trimmed = name.trim();
    const taken = organizations.some(
      (other) =>
        other.display_name === trimmed && other.organization_id !== organization?.organization_id
    );
    if (taken) {
      setError(t('history.organizations.nameTaken', { name: trimmed }));
      setNameInvalid(true);
      return;
    }
    setBusy(true);
    const saved = await onSave(trimmed, icon);
    setBusy(false);
    if (saved) onClose();
    else setSaveFailed(true);
  };
  const shownError = saveFailed ? (errorMessage ?? t('settings.workspace.saveFailed')) : error;

  return (
    <dialog
      ref={dialogRef}
      className="history-delete-dialog organization-dialog"
      aria-labelledby={titleId}
      onCancel={(event) => {
        event.preventDefault();
        onClose();
      }}
    >
      <h2 id={titleId}>
        {organization
          ? t('todo.organization.edit', { name: organization.display_name })
          : t('todo.rail.add')}
      </h2>
      <form
        onSubmit={(event) => {
          event.preventDefault();
          void save();
        }}
      >
        <div className="organization-dialog__fields">
          <button
            type="button"
            className="organization-dialog__icon"
            aria-label={t('todo.organization.icon')}
            onClick={() => fileRef.current?.click()}
          >
            {icon ? <img src={icon} alt="" /> : t('todo.organization.icon')}
          </button>
          <input
            ref={fileRef}
            type="file"
            accept="image/*"
            hidden
            aria-label={t('todo.organization.icon')}
            onChange={(event) => {
              void pickIcon(event.target.files?.[0]);
              // The same file picked again is read again.
              event.target.value = '';
            }}
          />
          <input
            ref={nameRef}
            type="text"
            className="history-filter-input organization-dialog__name"
            value={name}
            required
            aria-label={t('todo.organization.name')}
            placeholder={t('todo.organization.name')}
            aria-invalid={nameInvalid}
            aria-describedby={shownError ? errorId : undefined}
            onChange={(event) => {
              setName(event.target.value);
              setSaveFailed(false);
              if (nameInvalid) {
                setNameInvalid(false);
                setError(null);
              }
            }}
          />
        </div>
        {icon ? (
          <button
            type="button"
            className="organization-dialog__remove-icon"
            onClick={() => setIcon(null)}
          >
            {t('todo.organization.iconRemove')}
          </button>
        ) : null}
        {shownError ? (
          <p id={errorId} className="organization-dialog__error" role="alert">
            {shownError}
          </p>
        ) : null}
        <div className="history-delete-dialog__actions">
          <button type="button" className="history-filter-button" onClick={onClose}>
            {t('common.cancel')}
          </button>
          <button
            type="submit"
            className="history-filter-button organization-dialog__save"
            disabled={busy || !name.trim()}
            aria-busy={busy}
          >
            {organization ? t('common.save') : t('todo.organization.add')}
          </button>
        </div>
      </form>
    </dialog>
  );
}
