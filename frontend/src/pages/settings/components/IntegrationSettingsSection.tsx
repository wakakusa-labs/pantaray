import { useEffect, useRef, useState } from 'react';

import type { IntegrationToolkitStatus } from '../../../../electron/src/settings/integrationSettingsFetch';
import type { Translate } from '../types';
import { IntegrationAddServiceDialog } from './IntegrationAddServiceDialog';
import type { IntegrationFlow, IntegrationFlowActions } from './IntegrationConnectFlow';
import { IntegrationFlowStatus } from './IntegrationFlowStatus';
import { IntegrationLogo } from './IntegrationLogo';
import { IntegrationServiceDetail } from './IntegrationServiceDetail';
import {
  isAccountKey,
  type IntegrationAccountRowState,
  type IntegrationSettingsActions,
} from './IntegrationSettingsController';
import { SecretRow } from './SecretRow';
import './aiConnection.css';
import './integrationSettings.css';

const COMPOSIO_DASHBOARD_URL = 'https://dashboard.composio.dev';

/** The connection method's key. Only whether one is saved reaches the renderer. */
type IntegrationKeyState = {
  hasSavedKey: boolean;
  canStoreSecrets: boolean;
  /** Another connection setting is being applied. */
  disabled: boolean;
  onSave: (apiKey: string) => Promise<void>;
  onClear: () => Promise<void>;
};

type Props = {
  t: Translate;
  integrationKey: IntegrationKeyState;
  /** Null until the backend reports a configured connection method. */
  toolkits: IntegrationToolkitStatus[] | null;
  /** The saved key is refused: changing it is the fix, not loading again. */
  keyRejected: boolean;
  loadFailed: boolean;
  onReload: () => void;
  rows: Record<string, IntegrationAccountRowState>;
  actions: IntegrationSettingsActions;
  flow: IntegrationFlow | null;
  flowActions: IntegrationFlowActions;
  /** The service whose detail view shows first, for rendering it from fixtures. */
  initialToolkit?: string;
};

const NEEDS_RECONNECTION = new Set(['not_connected', 'reauthorization_required']);

/**
 * Presentational: what it shows comes from props. Without a key, only the key and how to get
 * one. With a key, two levels in place: the connected services, and one service's accounts.
 */
export function IntegrationSettingsSection({
  t,
  integrationKey,
  toolkits,
  keyRejected,
  loadFailed,
  onReload,
  rows,
  actions,
  flow,
  flowActions,
  initialToolkit,
}: Props) {
  const rootRef = useRef<HTMLDivElement>(null);
  const addButtonRef = useRef<HTMLButtonElement>(null);
  const [openToolkit, setOpenToolkit] = useState<string | null>(initialToolkit ?? null);
  const [isAdding, setIsAdding] = useState(false);
  // Where focus goes after the user moves between the two levels; null on first render.
  const focusAfterMove = useRef<string | null>(null);

  const shown = integrationKey.hasSavedKey ? toolkits : null;
  const detail = shown?.find(
    (toolkit) => toolkit.toolkit === openToolkit && toolkit.accounts.length > 0
  );

  // A service's view also closes when its last account is disconnected; focus then goes to
  // the add button instead of being lost with the removed view.
  const view = detail?.toolkit ?? null;
  const shownView = useRef(view);
  useEffect(() => {
    const previous = shownView.current;
    shownView.current = view;
    const target = focusAfterMove.current ?? (previous !== null && view === null ? 'add' : null);
    if (target === null) return;
    focusAfterMove.current = null;
    rootRef.current?.querySelector<HTMLElement>(`[data-integration-focus="${target}"]`)?.focus();
  }, [view]);

  // A finished connect opens its service. The account shows the access the backend reports:
  // it decides the default for a new account and carries a reconnected one's over.
  useEffect(() => {
    if (flow?.phase !== 'active') return;
    setOpenToolkit((open) => {
      if (open !== flow.toolkit) focusAfterMove.current = 'back';
      return flow.toolkit;
    });
  }, [flow]);

  if (detail) {
    return (
      <div ref={rootRef} className="dashboard-section integration-settings">
        <IntegrationServiceDetail
          t={t}
          toolkit={detail}
          rows={rows}
          actions={actions}
          flow={flow?.toolkit === detail.toolkit ? flow : null}
          flowActions={flowActions}
          onBack={() => {
            focusAfterMove.current = `row-${detail.toolkit}`;
            setOpenToolkit(null);
          }}
        />
      </div>
    );
  }

  const connected = shown?.filter((toolkit) => toolkit.accounts.length > 0) ?? [];
  const flowService =
    shown?.find((toolkit) => toolkit.toolkit === flow?.toolkit)?.display_name ?? flow?.toolkit;

  return (
    <div ref={rootRef} className="dashboard-section integration-settings">
      <h3 className="dashboard-section-title">{t('settings.integrations.title')}</h3>
      <p className="dashboard-section-description">{t('settings.integrations.description')}</p>
      {integrationKey.canStoreSecrets ? null : (
        <p className="settings-section-error">{t('settings.aiConnection.secretsUnavailable')}</p>
      )}
      <div className="integration-key">
        <SecretRow
          label={t('settings.integrations.key.label')}
          inputId="integration-key"
          disabled={!integrationKey.canStoreSecrets || integrationKey.disabled}
          hasSavedKey={integrationKey.hasSavedKey}
          placeholder={t('settings.integrations.key.placeholder')}
          onSave={integrationKey.onSave}
          onClear={integrationKey.onClear}
          problem={keyRejected ? t('settings.integrations.key.rejected') : undefined}
          draftProblem={(draft) =>
            isAccountKey(draft) ? t('settings.integrations.key.accountKey') : undefined
          }
          hint={
            integrationKey.hasSavedKey ? undefined : (
              <>
                <a
                  className="settings-inline-link"
                  href={COMPOSIO_DASHBOARD_URL}
                  target="_blank"
                  rel="noopener noreferrer"
                >
                  {t('settings.integrations.setup.open')}
                </a>
                <p className="ai-note ai-note--control">
                  {t('settings.integrations.setup.createKey')}
                </p>
                <p className="ai-note ai-note--control">
                  {t('settings.integrations.setup.permissions')}
                </p>
              </>
            )
          }
          t={t}
        />
      </div>
      {integrationKey.hasSavedKey && loadFailed ? (
        <p role="alert" className="dashboard-section-description settings-section-error">
          {t('settings.integrations.loadFailed')}{' '}
          <button type="button" className="integration-text-button" onClick={onReload}>
            {t('settings.integrations.reload')}
          </button>
        </p>
      ) : null}
      {flow && flowService ? (
        <IntegrationFlowStatus
          t={t}
          flow={flow}
          service={flowService}
          showService
          onCancel={flowActions.cancel}
        />
      ) : null}
      {shown ? (
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
          <li>
            <button
              ref={addButtonRef}
              type="button"
              className="integration-row"
              data-integration-focus="add"
              onClick={() => setIsAdding(true)}
            >
              <span className="integration-add-icon" aria-hidden="true">
                +
              </span>
              <span className="integration-name">{t('settings.integrations.addService.open')}</span>
              <span className="integration-chevron" aria-hidden="true">
                ›
              </span>
            </button>
          </li>
        </ul>
      ) : null}
      {shown && isAdding ? (
        <IntegrationAddServiceDialog
          t={t}
          toolkits={shown.filter((toolkit) => toolkit.accounts.length === 0)}
          onConnect={flowActions.connect}
          onClose={() => {
            setIsAdding(false);
            addButtonRef.current?.focus();
          }}
        />
      ) : null}
    </div>
  );
}
