import { useId, useState } from 'react';

import type {
  ApiDenial,
  IntegrationAccountStatus,
  IntegrationToolkitStatus,
} from '../../../../electron/src/settings/integrationSettingsFetch';
import type { Translate } from '../types';
import {
  integrationRow,
  type IntegrationAccountRowState,
  type IntegrationSettingsActions,
  type IntegrationToolsState,
} from './IntegrationSettingsController';
import './aiConnection.css';
import './integrationSettings.css';

type Props = {
  t: Translate;
  toolkits: IntegrationToolkitStatus[];
  rows: Record<string, IntegrationAccountRowState>;
  actions: IntegrationSettingsActions;
};

/** Presentational: everything it shows comes from props, so it renders from fixtures alone. */
export function IntegrationSettingsSection({ t, toolkits, rows, actions }: Props) {
  return (
    <div className="dashboard-section integration-settings">
      <h3 className="dashboard-section-title">{t('settings.integrations.title')}</h3>
      <p className="dashboard-section-description">{t('settings.integrations.description')}</p>
      {toolkits.map((toolkit) => (
        <section key={toolkit.toolkit}>
          <h4 className="ai-group">{toolkit.display_name}</h4>
          <ul className="integration-accounts">
            {toolkit.accounts.map((account) => (
              <li key={account.account} className="integration-account">
                <AccountRow
                  t={t}
                  toolkit={toolkit}
                  account={account}
                  row={integrationRow(rows, account.account)}
                  actions={actions}
                />
              </li>
            ))}
          </ul>
        </section>
      ))}
    </div>
  );
}

function denialText(t: Translate, reason: ApiDenial, service: string): string {
  if (reason.source === 'account_grant') {
    return reason.detail
      ? t('settings.integrations.denial.grantDetail', { service, detail: reason.detail })
      : t('settings.integrations.denial.grant', { service });
  }
  return t(`settings.integrations.denial.${reason.capability}`);
}

function AccountRow({
  t,
  toolkit: { toolkit, display_name: service },
  account,
  row,
  actions,
}: {
  t: Translate;
  toolkit: IntegrationToolkitStatus;
  account: IntegrationAccountStatus;
  row: IntegrationAccountRowState;
  actions: IntegrationSettingsActions;
}) {
  const id = useId();
  const reasonId = `${id}-reason`;
  // The refused option the user last pressed: its reason stays beside the control.
  const [pressed, setPressed] = useState<ApiDenial | null>(null);
  const chosen = row.pending ?? account.access;

  return (
    <>
      <div className="integration-account-head">
        <span className="ai-value">{account.label}</span>
        {account.health === 'ok' ? null : (
          <span className="ai-check ai-check--failed">
            {t(`settings.integrations.health.${account.health}`)}
          </span>
        )}
      </div>
      <div
        className="ai-segmented"
        role="radiogroup"
        aria-label={t('settings.integrations.accessLabel', { account: account.label })}
        aria-describedby={`${id}-write`}
      >
        {account.options.map((option) => (
          // aria-disabled, not disabled: a disabled radio swallows the press that shows why.
          <label
            key={option.value}
            className={`ai-segment${chosen === option.value ? ' ai-segment--selected' : ''}`}
          >
            <input
              className="ai-segment-input"
              type="radio"
              name={`${id}-access`}
              value={option.value}
              checked={chosen === option.value}
              aria-disabled={option.allowed ? undefined : true}
              aria-describedby={option.allowed ? undefined : reasonId}
              onChange={() => {
                setPressed(option.reason);
                if (option.reason === null)
                  actions.setAccess(toolkit, account.account, option.value);
              }}
            />
            {t(`settings.integrations.access.${option.value}`)}
          </label>
        ))}
      </div>
      <p id={`${id}-write`} className="ai-note">
        {t('settings.integrations.writeNote')}
      </p>
      <p id={reasonId} className="ai-note" aria-live="polite">
        {pressed ? denialText(t, pressed, service) : null}
      </p>
      {row.notice ? (
        <p role="alert" className="dashboard-section-description settings-section-error">
          {row.notice.kind === 'not_allowed'
            ? denialText(t, row.notice.reason, service)
            : t('settings.integrations.saveFailed')}
        </p>
      ) : null}
      <details
        className="integration-tools"
        onToggle={(event) => {
          if (event.currentTarget.open) actions.openTools(toolkit, account.account);
        }}
      >
        <summary className="integration-tools-summary">
          {t('settings.integrations.tools.summary')}
        </summary>
        <ToolList
          t={t}
          service={service}
          tools={row.tools}
          onBlockedChange={(slug, blocked) =>
            actions.setToolBlocked(toolkit, account.account, slug, blocked)
          }
        />
      </details>
    </>
  );
}

function ToolList({
  t,
  service,
  tools,
  onBlockedChange,
}: {
  t: Translate;
  service: string;
  tools: IntegrationToolsState | null;
  onBlockedChange: (slug: string, blocked: boolean) => void;
}) {
  const id = useId();
  const [pressedSlug, setPressedSlug] = useState<string | null>(null);

  if (tools === null || tools.state === 'loading') {
    return <p className="ai-note">{t('common.loading')}</p>;
  }
  if (tools.state === 'failed') {
    return (
      <p role="alert" className="dashboard-section-description settings-section-error">
        {t('settings.integrations.tools.loadFailed')}
      </p>
    );
  }
  return (
    <>
      <ul className="integration-tool-list">
        {tools.tools.map((tool) => {
          const denied = tool.api_denial !== null;
          const blocked = tools.savingSlug === tool.slug ? !tool.blocked : tool.blocked;
          const on = !blocked && !denied;
          const reasonId = `${id}-${tool.slug}`;
          return (
            <li key={tool.slug} className="integration-tool">
              <span className="integration-tool-name">{tool.name}</span>
              <span className="ai-note">
                {t(`settings.integrations.tools.class.${tool.tool_class}`)}
              </span>
              <button
                type="button"
                role="switch"
                aria-checked={on}
                aria-label={t('settings.integrations.tools.use', { tool: tool.name })}
                aria-disabled={denied ? true : undefined}
                aria-describedby={denied ? reasonId : undefined}
                className={`settings-toggle settings-toggle--fixed${on ? ' settings-toggle--on' : ''}`}
                onClick={() => {
                  if (denied) {
                    setPressedSlug(tool.slug);
                    return;
                  }
                  onBlockedChange(tool.slug, !tool.blocked);
                }}
              >
                <span className="settings-toggle-thumb" />
                <span className="settings-toggle-label">{t(on ? 'common.on' : 'common.off')}</span>
              </button>
              {tool.api_denial ? (
                <p id={reasonId} className="ai-note integration-tool-reason" aria-live="polite">
                  {pressedSlug === tool.slug ? denialText(t, tool.api_denial, service) : null}
                </p>
              ) : null}
            </li>
          );
        })}
      </ul>
      {tools.saveFailed ? (
        <p role="alert" className="dashboard-section-description settings-section-error">
          {t('settings.integrations.saveFailed')}
        </p>
      ) : null}
    </>
  );
}
