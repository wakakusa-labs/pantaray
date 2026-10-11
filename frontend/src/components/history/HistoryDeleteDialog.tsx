import { useEffect, useRef } from 'react';

import type { MessageKey } from '@/i18n/types';

/**
 * Mounted only while the question is open, so closing it is unmounting it; the page moves focus
 * afterwards because it alone knows whether the row still exists.
 */
export function HistoryDeleteDialog({
  t,
  title,
  body,
  onConfirm,
  onCancel,
}: {
  t: (key: MessageKey) => string;
  title: string;
  body: string;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  const cancelRef = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    const dialog = dialogRef.current;
    if (dialog && !dialog.open) dialog.showModal();
    // Deleting cannot be undone, so Enter right after opening must not confirm it.
    cancelRef.current?.focus();
  }, []);

  return (
    <dialog
      ref={dialogRef}
      className="history-delete-dialog"
      aria-labelledby="history-delete-dialog-title"
      aria-describedby="history-delete-dialog-body"
      onCancel={(event) => {
        event.preventDefault();
        onCancel();
      }}
    >
      <h2 id="history-delete-dialog-title">{title}</h2>
      <p id="history-delete-dialog-body">{body}</p>
      <div className="history-delete-dialog__actions">
        <button type="button" ref={cancelRef} className="history-filter-button" onClick={onCancel}>
          {t('common.cancel')}
        </button>
        <button
          type="button"
          className="history-filter-button history-delete-dialog__confirm"
          onClick={onConfirm}
        >
          {t('common.delete')}
        </button>
      </div>
    </dialog>
  );
}
