import { CheckItem } from '../components/Checklist';
import type { IntegrationToolkitStatus } from '../../electron/src/settings/integrationSettingsFetch';
import type { Translate } from '../pages/settings/types';
import type { ConnectionState } from '../pages/settings/useAiConnectionController';
import { aiConnectionLabel } from './aiConnectionLabel';
import { ServiceList } from './ServiceList';

/** What setup left set up, read only; an optional step not set up was skipped. */
export function DoneStep({
  t,
  state,
  aiConnected,
  isRecording,
  webSearchDone,
  services,
  servicesRead,
  onReloadServices,
}: {
  t: Translate;
  state: ConnectionState;
  aiConnected: boolean;
  isRecording: boolean;
  webSearchDone: boolean;
  /** The connected services; none means the apps step was skipped. */
  services: IntegrationToolkitStatus[];
  /** Whether `services` is the current list; until it is, apps are neither done nor skipped. */
  servicesRead: 'reading' | 'read' | 'failed';
  onReloadServices: () => void;
}) {
  const appsDone = services.length > 0;
  const appsSkipped = !appsDone && servicesRead === 'read';
  const skipped = t('onboarding.stepSkipped');
  const shows = (text: string) => <span className="check-item-state">{text}</span>;

  return (
    <>
      <p className="onb-lead">{t('onboarding.done.lead')}</p>
      <ul className="checklist">
        <CheckItem
          title={t('onboarding.done.ai')}
          status={aiConnected ? 'done' : 'pending'}
          note={aiConnectionLabel(t, state.settings)}
          action={() =>
            shows(
              t(
                aiConnected
                  ? 'settings.aiConnection.chatgpt.connected'
                  : 'onboarding.done.notConnected'
              )
            )
          }
        />
        <CheckItem
          title={t('onboarding.done.recording')}
          status={isRecording ? 'done' : 'pending'}
          action={() => shows(isRecording ? t('onboarding.recording.recording') : t('common.off'))}
        />
        <CheckItem
          title={t('onboarding.step.web_search')}
          status={webSearchDone ? 'done' : 'skipped'}
          note={webSearchDone ? 'Tavily' : null}
          action={() => shows(webSearchDone ? t('onboarding.done.available') : skipped)}
        />
        <CheckItem
          title={t('onboarding.done.apps')}
          status={appsDone ? 'done' : appsSkipped ? 'skipped' : 'pending'}
          problem={
            !appsDone && servicesRead === 'failed' ? t('settings.integrations.loadFailed') : null
          }
          note={appsDone ? <ServiceList toolkits={services} /> : null}
          action={() =>
            appsDone ? (
              shows(t('settings.aiConnection.chatgpt.connected'))
            ) : appsSkipped ? (
              shows(skipped)
            ) : servicesRead === 'failed' ? (
              <button type="button" className="settings-text-button" onClick={onReloadServices}>
                {t('settings.integrations.reload')}
              </button>
            ) : (
              shows(t('settings.loadingStatus'))
            )
          }
        />
      </ul>
      {!webSearchDone || appsSkipped ? (
        <p className="onb-after">{t('onboarding.done.skippedNote')}</p>
      ) : null}
    </>
  );
}
