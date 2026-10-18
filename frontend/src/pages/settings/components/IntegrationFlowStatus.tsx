import { useEffect, useId, useRef } from 'react';

import type { Translate } from '../types';
import type { IntegrationFlow } from './IntegrationConnectFlow';
import { denialText } from './IntegrationSettingsController';

/** The running or last connect, reconnect or disconnect, with Cancel while the browser has it. */
export function IntegrationFlowStatus({
  t,
  flow,
  service,
  showService,
  onCancel,
}: {
  t: Translate;
  flow: IntegrationFlow;
  service: string;
  /** Away from the service's own view, the line says which service it is about. */
  showService: boolean;
  onCancel: () => void;
}) {
  if (flow.phase === 'active') return null;
  const running =
    flow.phase === 'starting' || flow.phase === 'pending' || flow.phase === 'disconnecting';
  const status =
    flow.phase === 'not_allowed'
      ? denialText(t, flow.reason, service)
      : t(`settings.integrations.flow.${flow.phase}`);
  return (
    <p
      role={running ? 'status' : 'alert'}
      className={`integration-flow ${running ? 'ai-note' : 'settings-section-error'}`}
    >
      {showService ? t('settings.integrations.flow.service', { service, status }) : status}
      {flow.phase === 'pending' ? (
        <>
          {' '}
          <button type="button" className="integration-text-button" onClick={onCancel}>
            {t('settings.integrations.flow.cancel')}
          </button>
        </>
      ) : null}
    </p>
  );
}

/** Mounted only while asking; Cancel has focus first, since disconnecting cannot be undone. */
export function IntegrationDisconnectDialog({
  t,
  service,
  account,
  onConfirm,
  onClose,
}: {
  t: Translate;
  service: string;
  account: string;
  onConfirm: () => void;
  onClose: () => void;
}) {
  const id = useId();
  const dialogRef = useRef<HTMLDialogElement>(null);
  const cancelRef = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    dialogRef.current?.showModal();
    cancelRef.current?.focus();
  }, []);

  return (
    <dialog
      ref={dialogRef}
      className="integration-dialog integration-confirm"
      aria-labelledby={`${id}-title`}
      aria-describedby={`${id}-body`}
      onClose={onClose}
    >
      <h4 id={`${id}-title`} className="integration-dialog-title">
        {t('settings.integrations.disconnectDialog.title', { account })}
      </h4>
      <p id={`${id}-body`} className="dashboard-section-description">
        {t('settings.integrations.disconnectDialog.body', { service })}
      </p>
      <div className="integration-confirm-actions">
        <button
          type="button"
          ref={cancelRef}
          className="settings-text-button"
          onClick={() => dialogRef.current?.close()}
        >
          {t('common.cancel')}
        </button>
        <button
          type="button"
          className="settings-action-button"
          onClick={() => {
            onConfirm();
            dialogRef.current?.close();
          }}
        >
          {t('settings.integrations.disconnect')}
        </button>
      </div>
    </dialog>
  );
}
