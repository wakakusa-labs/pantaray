import { useRef, useState } from 'react';

import { CheckItem } from '../components/Checklist';
import { useAuth } from '@/hooks/useAuth';
import { IntegrationAddServiceDialog } from '../pages/settings/components/IntegrationAddServiceDialog';
import { useIntegrationConnectFlow } from '../pages/settings/components/IntegrationConnectFlow';
import {
  denialText,
  isAccountKey,
  type IntegrationSettingsController,
} from '../pages/settings/components/IntegrationSettingsController';
import type { Translate } from '../pages/settings/types';
import type {
  AiConnectionController,
  ConnectionState,
} from '../pages/settings/useAiConnectionController';
import { ComposioKeyGuide } from './ComposioKeyGuide';
import { KeyField } from './KeyField';
import { ServiceList } from './ServiceList';
import '../pages/settings/components/integrationSettings.css';
import './onboardingSteps.css';

/**
 * Setup's apps step: the Composio key, then the services. The key row is done once the services
 * it reaches are listed, and the services row once one is connected. The parent owns the
 * services list, since whether one is connected also decides the step.
 */
export function AppsStep({
  t,
  connection,
  state,
  integrations,
}: {
  t: Translate;
  connection: AiConnectionController;
  state: ConnectionState;
  integrations: IntegrationSettingsController;
}) {
  const { runtimeState } = useAuth();
  const ownerId = runtimeState.status === 'ready' ? (runtimeState.owner?.id ?? null) : null;
  const { flow, actions: flowActions } = useIntegrationConnectFlow(ownerId, integrations.reload);
  const [accountKeyTyped, setAccountKeyTyped] = useState(false);
  const [adding, setAdding] = useState(false);
  const addRef = useRef<HTMLButtonElement>(null);

  const { outcome } = connection;
  const keySaved = state.settings.hasSavedIntegrationKey;
  const services = keySaved ? integrations.toolkits : null;
  const connected = services?.filter((toolkit) => toolkit.accounts.length > 0) ?? [];
  const keyTitle = t('onboarding.apps.keyTitle');
  const keyProblem = accountKeyTyped
    ? t('settings.integrations.key.accountKey')
    : outcome?.operation === 'save_integration_key' && !outcome.result.ok
      ? t(`settings.aiConnection.error.${outcome.result.error}`)
      : !keySaved
        ? null
        : integrations.keyRejected
          ? t('settings.integrations.key.rejected')
          : integrations.loadFailed
            ? t('settings.integrations.loadFailed')
            : null;

  const saveKey = async (apiKey: string) => {
    const saved = await connection.run({ operation: 'save_integration_key', apiKey });
    // Saving the key adds the backend's connection method, which the services come from.
    await integrations.reload();
    if (!saved) throw new Error('INTEGRATION_KEY_SAVE_FAILED');
  };

  const following = flow?.phase === 'pending' || flow?.phase === 'retrying';
  const running = following || flow?.phase === 'starting';
  const flowService =
    services?.find((toolkit) => toolkit.toolkit === flow?.toolkit)?.display_name ?? flow?.toolkit;
  const flowText =
    flow && flowService && flow.phase !== 'active'
      ? t('settings.integrations.flow.service', {
          service: flowService,
          status:
            flow.phase === 'not_allowed'
              ? denialText(t, flow.reason, flowService)
              : t(`settings.integrations.flow.${flow.phase}`),
        })
      : null;

  return (
    <>
      <p className="onb-lead">{t('onboarding.apps.lead')}</p>
      <ul className="checklist">
        <CheckItem
          title={keyTitle}
          status={services !== null ? 'done' : 'pending'}
          problem={keyProblem}
          note={services !== null ? null : <ComposioKeyGuide t={t} />}
          action={() => (
            <>
              {keySaved && integrations.loadFailed ? (
                <button
                  type="button"
                  className="settings-text-button"
                  onClick={() => void integrations.reload()}
                >
                  {t('settings.integrations.reload')}
                </button>
              ) : null}
              <KeyField
                label={keyTitle}
                saved={keySaved}
                placeholder={t('settings.integrations.key.placeholder')}
                disabled={connection.pendingOperation !== null}
                onDraftChange={(draft) => setAccountKeyTyped(isAccountKey(draft))}
                blocked={accountKeyTyped}
                onSave={saveKey}
                t={t}
              />
            </>
          )}
        />
        <CheckItem
          title={t('onboarding.apps.servicesTitle')}
          status={connected.length > 0 ? 'done' : 'pending'}
          disabled={services === null}
          problem={running ? null : flowText}
          note={
            running ? flowText : connected.length > 0 ? <ServiceList toolkits={connected} /> : null
          }
          action={() =>
            following ? (
              <button type="button" className="settings-text-button" onClick={flowActions.cancel}>
                {t('settings.integrations.flow.cancel')}
              </button>
            ) : (
              <button
                ref={addRef}
                type="button"
                className="settings-action-button"
                disabled={services === null || running}
                onClick={() => setAdding(true)}
              >
                {t('onboarding.apps.add')}
              </button>
            )
          }
        />
      </ul>
      {services && adding ? (
        <IntegrationAddServiceDialog
          t={t}
          toolkits={services.filter((toolkit) => toolkit.accounts.length === 0)}
          onConnect={flowActions.connect}
          onClose={() => {
            setAdding(false);
            addRef.current?.focus();
          }}
        />
      ) : null}
    </>
  );
}
