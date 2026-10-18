import { useEffect, useRef, useState } from 'react';

import type { IntegrationToolkitStatus } from '../../../../electron/src/settings/integrationSettingsFetch';
import type { Translate } from '../types';
import { IntegrationAddServiceDialog } from './IntegrationAddServiceDialog';
import { IntegrationLogo } from './IntegrationLogo';
import {
  IntegrationServiceDetail,
  type IntegrationAccountCallbacks,
} from './IntegrationServiceDetail';
import type {
  IntegrationAccountRowState,
  IntegrationSettingsActions,
} from './IntegrationSettingsController';
import './aiConnection.css';
import './integrationSettings.css';

type Props = IntegrationAccountCallbacks & {
  t: Translate;
  toolkits: IntegrationToolkitStatus[];
  rows: Record<string, IntegrationAccountRowState>;
  actions: IntegrationSettingsActions;
  /** The service whose detail view shows first, for rendering it from fixtures. */
  initialToolkit?: string;
};

const NEEDS_RECONNECTION = new Set(['not_connected', 'reauthorization_required']);

/**
 * Presentational: what it shows comes from props. Two levels in place: the connected services,
 * and one service's accounts. Connecting is offered only when `onConnect` is supplied.
 */
export function IntegrationSettingsSection({
  t,
  toolkits,
  rows,
  actions,
  initialToolkit,
  ...callbacks
}: Props) {
  const rootRef = useRef<HTMLDivElement>(null);
  const addButtonRef = useRef<HTMLButtonElement>(null);
  const [openToolkit, setOpenToolkit] = useState<string | null>(initialToolkit ?? null);
  const [isAdding, setIsAdding] = useState(false);
  // Where focus goes after the user moves between the two levels; null on first render.
  const focusAfterMove = useRef<string | null>(null);

  useEffect(() => {
    const target = focusAfterMove.current;
    if (target === null) return;
    focusAfterMove.current = null;
    rootRef.current?.querySelector<HTMLElement>(`[data-integration-focus="${target}"]`)?.focus();
  }, [openToolkit]);

  const detail = toolkits.find((toolkit) => toolkit.toolkit === openToolkit);
  const connected = toolkits.filter((toolkit) => toolkit.accounts.length > 0);
  const { onConnect } = callbacks;

  return (
    <div ref={rootRef} className="dashboard-section integration-settings">
      {detail ? (
        <IntegrationServiceDetail
          t={t}
          toolkit={detail}
          rows={rows}
          actions={actions}
          onBack={() => {
            focusAfterMove.current = `row-${detail.toolkit}`;
            setOpenToolkit(null);
          }}
          {...callbacks}
        />
      ) : (
        <>
          <div className="integration-head">
            <div>
              <h3 className="dashboard-section-title">{t('settings.integrations.title')}</h3>
              <p className="dashboard-section-description">
                {t('settings.integrations.description')}
              </p>
            </div>
            {onConnect ? (
              <button
                ref={addButtonRef}
                type="button"
                className="settings-action-button"
                onClick={() => setIsAdding(true)}
              >
                {t('settings.integrations.addService.open')}
              </button>
            ) : null}
          </div>
          {connected.length > 0 ? (
            <ul className="integration-list">
              {connected.map((toolkit) => (
                <li key={toolkit.toolkit}>
                  <button
                    type="button"
                    className="integration-row"
                    data-integration-focus={`row-${toolkit.toolkit}`}
                    onClick={() => {
                      focusAfterMove.current = 'back';
                      setOpenToolkit(toolkit.toolkit);
                    }}
                  >
                    <IntegrationLogo toolkit={toolkit} />
                    <span className="integration-row-copy">
                      <span className="integration-name">{toolkit.display_name}</span>
                      <span className="integration-sub">
                        {toolkit.accounts.length === 1
                          ? toolkit.accounts[0].label
                          : t('settings.integrations.accountCount', {
                              count: toolkit.accounts.length,
                            })}
                      </span>
                    </span>
                    {toolkit.accounts.some((account) => NEEDS_RECONNECTION.has(account.health)) ? (
                      <span className="integration-badge">
                        {t('settings.integrations.needsReconnection')}
                      </span>
                    ) : null}
                    <span className="integration-chevron" aria-hidden="true">
                      ›
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          ) : null}
          {onConnect && isAdding ? (
            <IntegrationAddServiceDialog
              t={t}
              toolkits={toolkits.filter((toolkit) => toolkit.accounts.length === 0)}
              onConnect={onConnect}
              onClose={() => {
                setIsAdding(false);
                addButtonRef.current?.focus();
              }}
            />
          ) : null}
        </>
      )}
    </div>
  );
}
