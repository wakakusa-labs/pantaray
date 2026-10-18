import { useEffect, useId, useRef, useState } from 'react';

import type { IntegrationToolkitStatus } from '../../../../electron/src/settings/integrationSettingsFetch';
import type { Translate } from '../types';
import { IntegrationLogo } from './IntegrationLogo';

/**
 * A native modal dialog: `showModal` traps focus and closes it on Escape. Whoever opens it
 * unmounts it from `onClose` and puts focus back on the button that opened it.
 */
export function IntegrationAddServiceDialog({
  t,
  toolkits,
  onConnect,
  onClose,
}: {
  t: Translate;
  toolkits: IntegrationToolkitStatus[];
  onConnect: (toolkit: string) => void;
  onClose: () => void;
}) {
  const titleId = useId();
  const dialogRef = useRef<HTMLDialogElement>(null);
  const [query, setQuery] = useState('');
  const needle = query.trim().toLocaleLowerCase();
  const shown = toolkits.filter((toolkit) =>
    toolkit.display_name.toLocaleLowerCase().includes(needle)
  );

  useEffect(() => {
    dialogRef.current?.showModal();
  }, []);

  return (
    <dialog
      ref={dialogRef}
      className="integration-dialog"
      aria-labelledby={titleId}
      onClose={onClose}
    >
      <div className="integration-dialog-head">
        <h4 id={titleId} className="integration-dialog-title">
          {t('settings.integrations.addService.title')}
        </h4>
        <button
          type="button"
          className="integration-icon-button"
          aria-label={t('settings.integrations.addService.close')}
          onClick={() => dialogRef.current?.close()}
        >
          <span aria-hidden="true">✕</span>
        </button>
      </div>
      <input
        type="search"
        className="integration-dialog-search"
        aria-label={t('settings.integrations.addService.search')}
        placeholder={t('settings.integrations.addService.search')}
        value={query}
        onChange={(event) => setQuery(event.target.value)}
      />
      <ul className="integration-dialog-list">
        {shown.map((toolkit) => (
          <li key={toolkit.toolkit} className="integration-dialog-item">
            <IntegrationLogo toolkit={toolkit} small />
            <span className="integration-name">{toolkit.display_name}</span>
            <button
              type="button"
              className="settings-action-button"
              aria-label={t('settings.integrations.addService.connectLabel', {
                service: toolkit.display_name,
              })}
              onClick={() => {
                onConnect(toolkit.toolkit);
                dialogRef.current?.close();
              }}
            >
              {t('settings.integrations.addService.connect')}
            </button>
          </li>
        ))}
      </ul>
      {shown.length === 0 ? (
        <p className="ai-note integration-dialog-empty">
          {t('settings.integrations.addService.noMatch')}
        </p>
      ) : null}
    </dialog>
  );
}
