import { Check, Minus } from 'lucide-react';
import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react';

import { BrandWordmark } from '@/components/BrandWordmark';
import { LocalOwnerBoundary } from '@/components/LocalOwnerBoundary';
import { useI18n } from '@/context/useI18n';
import { useAuth } from '@/hooks/useAuth';
import {
  connectedToolkits,
  useIntegrationSettingsController,
} from '../pages/settings/components/IntegrationSettingsController';
import { ConnectionLoadFailure } from '../pages/settings/components/ConnectionLoadFailure';
import {
  useAiConnectionController,
  type ConnectionState,
} from '../pages/settings/useAiConnectionController';
import { AiStep } from './AiStep';
import { AppsStep } from './AppsStep';
import { DoneStep } from './DoneStep';
import { RecordingStep } from './RecordingStep';
import type { OnboardingProgress, OnboardingStep } from './useOnboardingProgress';
import { useRecordingStep } from './useRecordingStep';
import { WebSearchStep } from './WebSearchStep';
import '../components/Layout.css';
import '../components/recordingIntro.css';
import '../pages/settings/settingsPage.css';
import './onboarding.css';

const STEPS: readonly { id: OnboardingStep; required: boolean }[] = [
  { id: 'ai', required: true },
  { id: 'recording', required: true },
  { id: 'web_search', required: false },
  { id: 'apps', required: false },
  { id: 'done', required: false },
];

type Outcome = 'pending' | 'done' | 'skipped';

/** First-run setup in a full window. Only the owner's steps wait for an owner: the AI
 *  connection is the device's, and it is what repairs a runtime that cannot start one. */
export function OnboardingScreen({
  step,
  progress,
}: {
  step: OnboardingStep;
  progress: OnboardingProgress;
}) {
  const { t } = useI18n();
  // The steps change the connection through this controller.
  const connection = useAiConnectionController();
  const { state } = connection;
  // The services are the owner's; without an owner none are read.
  const { runtimeState } = useAuth();
  const integrations = useIntegrationSettingsController(
    runtimeState.status === 'ready' ? (runtimeState.owner?.id ?? null) : null
  );
  const keySaved = state?.settings.hasSavedIntegrationKey ?? false;
  const services = connectedToolkits((keySaved ? integrations.toolkits : null) ?? []);
  const [servicesRead, setServicesRead] = useState<'reading' | 'read'>('reading');
  const listReads = useRef(0);
  const { reload: reloadServices } = integrations;
  const recording = useRecordingStep();
  const titleRef = useRef<HTMLHeadingElement>(null);
  const shownStep = useRef(step);

  const readServices = useCallback(() => {
    const read = ++listReads.current;
    void reloadServices().then(() => {
      if (read === listReads.current) setServicesRead('read');
    });
  }, [reloadServices]);
  useEffect(readServices, [readServices]);
  // Apps and Done show the services as they are now: a connect can finish after the apps step
  // was left, which ended its polling.
  const goTo = (target: OnboardingStep) => {
    if (target === 'apps' || target === 'done') {
      setServicesRead('reading');
      readServices();
    }
    progress.goTo(target);
  };
  // Until that read lands, no service is known to be missing.
  const servicesKnown = !keySaved ? 'read' : integrations.loadFailed ? 'failed' : servicesRead;

  // A step change moves focus to the new step's heading, as a page change would.
  useEffect(() => {
    if (shownStep.current === step) return;
    shownStep.current = step;
    titleRef.current?.focus();
  }, [step]);

  const configured: Record<OnboardingStep, boolean> = {
    ai: state !== null && state.runtime.ok && state.runtime.status.llmRoute !== 'unconfigured',
    recording: recording.isRecording === true,
    web_search: state?.settings.hasSavedWebSearchKey ?? false,
    apps: services.length > 0,
    done: false,
  };
  const index = STEPS.findIndex((s) => s.id === step);
  const current = STEPS[index];
  // An optional step the user went past without setting it up was skipped; nothing stores it.
  const outcome = (id: OnboardingStep, i: number): Outcome =>
    configured[id]
      ? 'done'
      : i < index && id !== 'done' && (id !== 'apps' || servicesKnown === 'read')
        ? 'skipped'
        : 'pending';
  const requiredDoneBefore = (i: number) =>
    STEPS.slice(0, i).every((s) => !s.required || configured[s.id]);
  const next = () => goTo(STEPS[index + 1].id);
  // The connection steps need the connection read first.
  const withConnection = (render: (read: ConnectionState) => ReactNode) =>
    connection.loadError ? (
      <ConnectionLoadFailure t={t} connection={connection} error={connection.loadError} />
    ) : state === null ? (
      <p role="status">{t('settings.loadingStatus')}</p>
    ) : (
      render(state)
    );

  return (
    <div className="app-shell onb-shell">
      <div className="app-titlebar" aria-hidden="true" />
      <main className="app-main">
        <div className="app-surface app-surface--split">
          <div className="app-split">
            <nav
              className="app-split-master settings-page-nav onb-nav"
              aria-label={t('onboarding.title')}
            >
              <BrandWordmark className="onb-wordmark" />
              <h2 className="app-split-title settings-page-nav-title onb-nav-title">
                {t('onboarding.title')}
              </h2>
              <p className="onb-progress">
                {index + 1} / {STEPS.length}
              </p>
              <ol className="onb-steps">
                {STEPS.map((s, i) => {
                  const state = outcome(s.id, i);
                  const isCurrent = s.id === step;
                  const meta =
                    s.id === 'done'
                      ? null
                      : state === 'done'
                        ? t('onboarding.stepDone')
                        : state === 'skipped'
                          ? t('onboarding.stepSkipped')
                          : t(s.required ? 'onboarding.required' : 'onboarding.optional');
                  return (
                    <li key={s.id}>
                      <button
                        type="button"
                        className={`settings-page-nav-item onb-step-item${isCurrent ? ' settings-page-nav-item--active' : ''}`}
                        aria-current={isCurrent ? 'step' : undefined}
                        disabled={!requiredDoneBefore(i) || progress.finishing === 'saving'}
                        onClick={() => goTo(s.id)}
                      >
                        <span
                          className={`onb-marker onb-marker--${state}${isCurrent ? ' onb-marker--current' : ''}`}
                          aria-hidden="true"
                        >
                          {state === 'done' ? (
                            <Check size={12} strokeWidth={2.6} />
                          ) : state === 'skipped' ? (
                            <Minus size={12} strokeWidth={2.6} />
                          ) : (
                            i + 1
                          )}
                        </span>
                        <span className="onb-step-copy">
                          <span>{t(`onboarding.step.${s.id}`)}</span>
                          {meta ? <span className="onb-step-meta">{meta}</span> : null}
                        </span>
                      </button>
                    </li>
                  );
                })}
              </ol>
            </nav>

            <div className="app-split-detail onb-detail">
              <div className="onb-content">
                <h2 className="onb-title" ref={titleRef} tabIndex={-1}>
                  {step === 'done' ? t('onboarding.done.title') : t(`onboarding.step.${step}`)}
                  {step !== 'done' ? (
                    <span className="onb-tag">
                      {t(current.required ? 'onboarding.required' : 'onboarding.optional')}
                    </span>
                  ) : null}
                </h2>

                {step === 'ai'
                  ? withConnection((read) => (
                      <AiStep
                        t={t}
                        connection={connection}
                        state={read}
                        configured={configured.ai}
                      />
                    ))
                  : null}
                {step === 'recording' ? (
                  <LocalOwnerBoundary>
                    <RecordingStep t={t} recording={recording} />
                  </LocalOwnerBoundary>
                ) : null}
                {step === 'web_search'
                  ? withConnection((read) => (
                      <WebSearchStep t={t} connection={connection} state={read} />
                    ))
                  : null}
                {step === 'apps'
                  ? withConnection((read) => (
                      <LocalOwnerBoundary>
                        <AppsStep
                          t={t}
                          connection={connection}
                          state={read}
                          integrations={integrations}
                        />
                      </LocalOwnerBoundary>
                    ))
                  : null}
                {step === 'done' && state !== null ? (
                  <DoneStep
                    t={t}
                    state={state}
                    aiConnected={configured.ai}
                    isRecording={configured.recording}
                    webSearchDone={configured.web_search}
                    services={services}
                    servicesRead={servicesKnown}
                    onReloadServices={() => void reloadServices()}
                  />
                ) : null}
              </div>

              <div className="onb-footer">
                {index > 0 && step !== 'done' ? (
                  <button
                    type="button"
                    className="settings-action-button"
                    onClick={() => goTo(STEPS[index - 1].id)}
                  >
                    {t('onboarding.back')}
                  </button>
                ) : (
                  <span />
                )}
                <div className="onb-footer-end">
                  {progress.finishing === 'failed' ? (
                    <span className="settings-section-error" role="alert">
                      {t('onboarding.finishFailed')}
                    </span>
                  ) : null}
                  {!current.required && step !== 'done' && !configured[step] ? (
                    <button type="button" className="settings-action-button" onClick={next}>
                      {t('onboarding.skip')}
                    </button>
                  ) : null}
                  {step === 'done' ? (
                    <button
                      type="button"
                      className="recording-intro-primary"
                      disabled={progress.finishing === 'saving'}
                      onClick={() => void progress.finish()}
                    >
                      {t('onboarding.start')}
                    </button>
                  ) : (
                    <button
                      type="button"
                      className="recording-intro-primary"
                      disabled={!configured[step]}
                      onClick={next}
                    >
                      {t('onboarding.next')}
                    </button>
                  )}
                </div>
              </div>
            </div>
          </div>
        </div>
      </main>
    </div>
  );
}
