import { Check, Minus } from 'lucide-react';
import { useEffect, useState, type ReactNode } from 'react';

import type { ConnectionStateResult } from '../../electron/src/ipc/schemas/aiConnection';
import type { IntegrationToolkitStatus } from '../../electron/src/settings/integrationSettingsFetch';
import { AiConnectionRow as Row } from '../pages/settings/components/AiConnectionRow';
import { IntegrationLogo } from '../pages/settings/components/IntegrationLogo';
import type { Translate } from '../pages/settings/types';

type ConnectionState = Extract<ConnectionStateResult, { ok: true }>;

function aiLabel(t: Translate, { settings, runtime }: ConnectionState): string {
  if (runtime.ok && runtime.status.llmRoute === 'cloud') {
    return t('settings.aiConnection.method.cloud.title');
  }
  const { method, provider } = settings.preferences;
  return method === 'chatgpt'
    ? t('settings.aiConnection.method.chatgpt.title')
    : t('onboarding.done.apiKey', { provider: t(`settings.aiConnection.provider.${provider}`) });
}

/** The services with an account connected, read once; none when the read fails. */
function useConnectedServices(hasSavedKey: boolean): IntegrationToolkitStatus[] {
  const [services, setServices] = useState<IntegrationToolkitStatus[]>([]);
  useEffect(() => {
    const integrations = window.electron?.integrations;
    if (!hasSavedKey || !integrations) return;
    let active = true;
    integrations
      .getStatus()
      .then((status) => {
        if (active && status.state === 'configured') {
          setServices(status.toolkits.filter((toolkit) => toolkit.accounts.length > 0));
        }
      })
      // The summary still says the key is saved, which is true.
      .catch((error: unknown) => console.error('Failed to read the connected services:', error));
    return () => {
      active = false;
    };
  }, [hasSavedKey]);
  return services;
}

/** What setup left configured; anything skipped can be done later in Settings. */
export function DoneStep({
  t,
  connection,
  isRecording,
}: {
  t: Translate;
  connection: ConnectionState;
  isRecording: boolean;
}) {
  const { hasSavedWebSearchKey, hasSavedIntegrationKey } = connection.settings;
  const services = useConnectedServices(hasSavedIntegrationKey);

  return (
    <>
      <p className="onb-lead">{t('onboarding.done.lead')}</p>
      <section className="dashboard-section ai-connection onb-section">
        <Row label={t('onboarding.done.ai')}>
          <Done>{aiLabel(t, connection)}</Done>
        </Row>
        <Row label={t('onboarding.done.recording')}>
          {isRecording ? <Done>{t('common.on')}</Done> : <Unset>{t('common.off')}</Unset>}
        </Row>
        <Row label={t('onboarding.step.web_search')}>
          {hasSavedWebSearchKey ? (
            <Done>Tavily</Done>
          ) : (
            <Unset>{t('onboarding.stepSkipped')}</Unset>
          )}
        </Row>
        <Row label={t('onboarding.done.apps')}>
          {services.length > 0 ? (
            <Done>
              {services.map((toolkit) => (
                <span key={toolkit.toolkit} className="onb-summary-app">
                  <IntegrationLogo toolkit={toolkit} small />
                  {toolkit.display_name}
                </span>
              ))}
            </Done>
          ) : hasSavedIntegrationKey ? (
            <Done>{t('onboarding.done.keySaved')}</Done>
          ) : (
            <Unset>{t('onboarding.stepSkipped')}</Unset>
          )}
        </Row>
        {!hasSavedWebSearchKey || !hasSavedIntegrationKey ? (
          <p className="ai-note">{t('onboarding.done.skippedNote')}</p>
        ) : null}
      </section>
    </>
  );
}

function Done({ children }: { children: ReactNode }) {
  return (
    <span className="onb-summary-value">
      <Check size={14} strokeWidth={2.2} aria-hidden="true" className="onb-summary-icon" />
      {children}
    </span>
  );
}

function Unset({ children }: { children: ReactNode }) {
  return (
    <span className="onb-summary-value onb-summary-value--unset">
      <Minus size={14} strokeWidth={2.2} aria-hidden="true" />
      {children}
    </span>
  );
}
