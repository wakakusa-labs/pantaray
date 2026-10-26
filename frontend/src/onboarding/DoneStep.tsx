import { Check, Minus } from 'lucide-react';
import { useEffect, useState, type ReactNode } from 'react';

import { LocalOwnerBoundary } from '@/components/LocalOwnerBoundary';
import type { ConnectionStateResult } from '../../electron/src/ipc/schemas/aiConnection';
import type { IntegrationToolkitStatus } from '../../electron/src/settings/integrationSettingsFetch';
import { AiConnectionRow as Row } from '../pages/settings/components/AiConnectionRow';
import { IntegrationLogo } from '../pages/settings/components/IntegrationLogo';
import type { Translate } from '../pages/settings/types';
import { aiConnectionLabel } from './aiConnectionLabel';

type ConnectionState = Extract<ConnectionStateResult, { ok: true }>;

function aiLabel(t: Translate, { settings, runtime }: ConnectionState): string {
  if (runtime.ok && runtime.status.llmRoute === 'cloud') {
    return t('settings.aiConnection.method.cloud.title');
  }
  return aiConnectionLabel(t, settings);
}

/** The owner's connected services; until (or unless) the read finds one, the saved key. */
function ConnectedServices({ t }: { t: Translate }) {
  const [services, setServices] = useState<IntegrationToolkitStatus[]>([]);
  useEffect(() => {
    let active = true;
    (async () => window.electron!.integrations!.getStatus())()
      .then((status) => {
        if (active && status.state === 'configured') {
          setServices(status.toolkits.filter((toolkit) => toolkit.accounts.length > 0));
        }
      })
      .catch((error: unknown) => console.error('Failed to read the connected services:', error));
    return () => {
      active = false;
    };
  }, []);
  if (services.length === 0) return <Done>{t('onboarding.done.keySaved')}</Done>;
  return (
    <Done>
      {services.map((toolkit) => (
        <span key={toolkit.toolkit} className="onb-summary-app">
          <IntegrationLogo toolkit={toolkit} small />
          {toolkit.display_name}
        </span>
      ))}
    </Done>
  );
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
          {hasSavedIntegrationKey ? (
            <LocalOwnerBoundary fallback={<Done>{t('onboarding.done.keySaved')}</Done>}>
              <ConnectedServices t={t} />
            </LocalOwnerBoundary>
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
