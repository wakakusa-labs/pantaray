import { useEffect, useId, useState } from 'react';

import { MenuButton, type MenuButtonItem } from '@/components/ui/MenuButton';
import type {
  IntegrationAccountStatus,
  IntegrationToolkitStatus,
  IntegrationToolSetting,
} from '../../../../electron/src/settings/integrationSettingsFetch';
import type { Translate } from '../types';
import {
  denialText,
  integrationRow,
  type IntegrationAccountRowState,
  type IntegrationSettingsActions,
  type IntegrationToolsState,
} from './IntegrationSettingsController';
import type { IntegrationFlow, IntegrationFlowActions } from './IntegrationConnectFlow';
import { IntegrationDisconnectDialog, IntegrationFlowStatus } from './IntegrationFlowStatus';
import { IntegrationLogo } from './IntegrationLogo';

type DetailProps = {
  t: Translate;
  toolkit: IntegrationToolkitStatus;
  rows: Record<string, IntegrationAccountRowState>;
  actions: IntegrationSettingsActions;
  /** The running or last flow, when it is about this service. */
  flow: IntegrationFlow | null;
  flowActions: IntegrationFlowActions;
  onBack: () => void;
};

/** One service: each connected account with its access choice and its tools. */
export function IntegrationServiceDetail({
  t,
  toolkit,
  rows,
  actions,
  flow,
  flowActions,
  onBack,
}: DetailProps) {
  const [confirming, setConfirming] = useState<IntegrationAccountStatus | null>(null);
  // Loaded up front, so each row can say how many tools are turned off. Only accounts never
  // loaded: a failed load is retried when the user opens that account's tools.
  useEffect(() => {
    for (const account of toolkit.accounts) {
      if (integrationRow(rows, account.account).tools === null) {
        actions.loadTools(toolkit.toolkit, account.account);
      }
    }
  }, [actions, rows, toolkit]);

  return (
    <>
      <button
        type="button"
        className="integration-back"
        aria-label={t('settings.integrations.backLabel')}
        data-integration-focus="back"
        onClick={onBack}
      >
        <span aria-hidden="true">‹ </span>
        {t('settings.integrations.title')}
      </button>
      <div className="integration-detail-head">
        <IntegrationLogo toolkit={toolkit} />
        <h3 className="dashboard-section-title integration-detail-title">{toolkit.display_name}</h3>
      </div>
      <ul className="integration-list">
        {toolkit.accounts.map((account) => (
          <AccountRow
            key={account.account}
            t={t}
            toolkit={toolkit}
            account={account}
            row={integrationRow(rows, account.account)}
            actions={actions}
            flow={flow?.account === account.account ? flow : null}
            onReconnect={() => flowActions.reconnect(toolkit.toolkit, account.account)}
            onDisconnect={() => setConfirming(account)}
            onCancelFlow={flowActions.cancel}
          />
        ))}
      </ul>
      {flow?.account === null ? (
        <IntegrationFlowStatus
          t={t}
          flow={flow}
          service={toolkit.display_name}
          showService={false}
          onCancel={flowActions.cancel}
        />
      ) : null}
      <button
        type="button"
        className="integration-text-button integration-add-account"
        onClick={() => flowActions.connect(toolkit.toolkit)}
      >
        {t('settings.integrations.addAccount')}
      </button>
      {confirming ? (
        <IntegrationDisconnectDialog
          t={t}
          service={toolkit.display_name}
          account={confirming.label}
          onConfirm={() => flowActions.disconnect(toolkit.toolkit, confirming.account)}
          onClose={() => setConfirming(null)}
        />
      ) : null}
    </>
  );
}

function AccountRow({
  t,
  toolkit,
  account,
  row,
  actions,
  flow,
  onReconnect,
  onDisconnect,
  onCancelFlow,
}: {
  t: Translate;
  toolkit: IntegrationToolkitStatus;
  account: IntegrationAccountStatus;
  row: IntegrationAccountRowState;
  actions: IntegrationSettingsActions;
  flow: IntegrationFlow | null;
  onReconnect: () => void;
  onDisconnect: () => void;
  onCancelFlow: () => void;
}) {
  const toolsId = useId();
  const [expanded, setExpanded] = useState(false);
  const service = toolkit.display_name;
  // An unconfirmed save leaves no value to show: the control says so instead of guessing.
  const chosen = row.pending ?? (row.unconfirmed ? null : account.access);
  const chosenTitle = t(`settings.integrations.access.${chosen ?? 'unconfirmed'}`);
  const blockedCount =
    row.tools?.state === 'loaded' ? row.tools.tools.filter((tool) => tool.blocked).length : 0;

  const accessItems: MenuButtonItem[] = account.options.map((option) => ({
    key: option.value,
    title: t(`settings.integrations.access.${option.value}`),
    description: t(`settings.integrations.accessDescription.${option.value}`),
    unavailableReason: option.reason ? denialText(t, option.reason, service) : null,
    checked: option.value === chosen,
    onSelect: () => actions.setAccess(toolkit.toolkit, account.account, option.value),
  }));
  const moreItems: MenuButtonItem[] = [
    { key: 'reconnect', title: t('settings.integrations.reconnect'), onSelect: onReconnect },
    { key: 'disconnect', title: t('settings.integrations.disconnect'), onSelect: onDisconnect },
  ];

  return (
    <li className="integration-account">
      <div className="integration-account-row">
        <div className="integration-account-copy">
          <div className="integration-name">{account.label}</div>
          {account.health === 'ok' ? null : (
            <div className="integration-health">
              {t(`settings.integrations.health.${account.health}`)}
            </div>
          )}
          <div className="integration-sub">
            <button
              type="button"
              className="integration-text-button"
              aria-expanded={expanded}
              aria-controls={toolsId}
              onClick={() => {
                if (!expanded) actions.loadTools(toolkit.toolkit, account.account);
                setExpanded(!expanded);
              }}
            >
              {t('settings.integrations.tools.summary')}
            </button>
            {blockedCount > 0 ? (
              <span>
                {' · '}
                {t('settings.integrations.tools.blockedCount', { count: blockedCount })}
              </span>
            ) : null}
            <span role="status">{row.busy ? ` · ${t('settings.integrations.saving')}` : null}</span>
          </div>
        </div>
        <MenuButton
          label={t('settings.integrations.accessButton', {
            account: account.label,
            access: chosenTitle,
          })}
          busy={row.busy}
          items={accessItems}
        >
          <span>{chosenTitle}</span>
          <span className="integration-caret" aria-hidden="true">
            ▾
          </span>
        </MenuButton>
        <MenuButton
          variant="icon"
          label={t('settings.integrations.moreActions', { account: account.label })}
          items={moreItems}
        >
          <span aria-hidden="true">•••</span>
        </MenuButton>
      </div>
      {flow ? (
        <IntegrationFlowStatus
          t={t}
          flow={flow}
          service={service}
          showService={false}
          onCancel={onCancelFlow}
        />
      ) : null}
      {row.notice ? (
        <p role="alert" className="dashboard-section-description settings-section-error">
          {row.notice.kind === 'not_allowed'
            ? denialText(t, row.notice.reason, service)
            : t('settings.integrations.saveFailed')}
        </p>
      ) : null}
      {row.unconfirmed ? (
        <p role="alert" className="dashboard-section-description settings-section-error">
          {t('settings.integrations.unconfirmed')}{' '}
          <button
            type="button"
            className="integration-text-button"
            disabled={row.busy}
            onClick={() => actions.recheckAccess(toolkit.toolkit, account.account)}
          >
            {t('settings.integrations.recheck')}
          </button>
        </p>
      ) : null}
      <div id={toolsId} hidden={!expanded}>
        <ToolList
          t={t}
          service={service}
          tools={row.tools}
          writesUnused={chosen === 'read_only'}
          onRetry={() => actions.loadTools(toolkit.toolkit, account.account)}
          onBlockedChange={(slug, blocked) =>
            actions.setToolBlocked(toolkit.toolkit, account.account, slug, blocked)
          }
        />
      </div>
    </li>
  );
}

function ToolList({
  t,
  service,
  tools,
  writesUnused,
  onRetry,
  onBlockedChange,
}: {
  t: Translate;
  service: string;
  tools: IntegrationToolsState | null;
  writesUnused: boolean;
  onRetry: () => void;
  onBlockedChange: (slug: string, blocked: boolean) => void;
}) {
  if (tools === null || tools.state === 'loading') {
    return <p className="ai-note">{t('common.loading')}</p>;
  }
  if (tools.state === 'failed') {
    return (
      <p role="alert" className="dashboard-section-description settings-section-error">
        {t(
          tools.unconfirmed
            ? 'settings.integrations.tools.unconfirmed'
            : 'settings.integrations.tools.loadFailed'
        )}{' '}
        <button type="button" className="integration-text-button" onClick={onRetry}>
          {t('settings.integrations.tools.retry')}
        </button>
      </p>
    );
  }
  const group = (toolClass: IntegrationToolSetting['tool_class'], unused: boolean) => (
    <ToolGroup
      t={t}
      service={service}
      title={t(`settings.integrations.tools.class.${toolClass}`)}
      tools={tools.tools.filter((tool) => tool.tool_class === toolClass)}
      savingSlug={tools.savingSlug}
      unusedNote={unused ? t('settings.integrations.tools.writesUnused') : null}
      onBlockedChange={onBlockedChange}
    />
  );
  return (
    <div className="integration-tools">
      {group('read', false)}
      {group('write', writesUnused)}
      <p role="status" className="ai-note">
        {tools.savingSlug === null ? null : t('settings.integrations.saving')}
      </p>
      {tools.saveFailed ? (
        <p role="alert" className="dashboard-section-description settings-section-error">
          {t('settings.integrations.saveFailed')}
        </p>
      ) : null}
    </div>
  );
}

function ToolGroup({
  t,
  service,
  title,
  tools,
  savingSlug,
  unusedNote,
  onBlockedChange,
}: {
  t: Translate;
  service: string;
  title: string;
  tools: IntegrationToolSetting[];
  savingSlug: string | null;
  /** Set when the account's access leaves this group unused: its switches are inactive. */
  unusedNote: string | null;
  onBlockedChange: (slug: string, blocked: boolean) => void;
}) {
  const id = useId();
  if (tools.length === 0) return null;
  return (
    <div className="integration-tool-group">
      <div id={`${id}-title`} className="integration-tool-group-title">
        {title}
      </div>
      {unusedNote ? <p className="ai-note">{unusedNote}</p> : null}
      <ul className="integration-tool-list" aria-labelledby={`${id}-title`}>
        {tools.map((tool) => {
          const blocked = savingSlug === tool.slug ? !tool.blocked : tool.blocked;
          // One block saves at a time; the others wait, visibly, instead of losing the press.
          const busy = savingSlug !== null;
          const on = !blocked && tool.api_denial === null && unusedNote === null;
          const reasonId = `${id}-${tool.slug}`;
          return (
            <li key={tool.slug} className="integration-tool">
              <div>
                <div className="integration-tool-name">{tool.name}</div>
                {tool.api_denial ? (
                  <div id={reasonId} className="integration-reason">
                    {denialText(t, tool.api_denial, service)}
                  </div>
                ) : null}
              </div>
              <button
                type="button"
                role="switch"
                aria-checked={on}
                aria-label={t('settings.integrations.tools.use', { tool: tool.name })}
                aria-disabled={tool.api_denial || busy ? true : undefined}
                aria-busy={busy || undefined}
                aria-describedby={tool.api_denial ? reasonId : undefined}
                disabled={unusedNote !== null}
                className={`settings-toggle settings-toggle--fixed${on ? ' settings-toggle--on' : ''}`}
                onClick={() => {
                  if (tool.api_denial === null && !busy) onBlockedChange(tool.slug, !tool.blocked);
                }}
              >
                <span className="settings-toggle-thumb" />
                <span className="settings-toggle-label">{t(on ? 'common.on' : 'common.off')}</span>
              </button>
            </li>
          );
        })}
      </ul>
    </div>
  );
}
