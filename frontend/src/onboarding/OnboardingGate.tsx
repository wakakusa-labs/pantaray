import type { ReactNode } from 'react';

import { LocalOwnerBoundary } from '@/components/LocalOwnerBoundary';
import { useI18n } from '@/context/useI18n';
import { OnboardingScreen } from './OnboardingScreen';
import { useOnboardingProgress } from './useOnboardingProgress';

/**
 * Shows first-run setup instead of the app until it is completed. Nothing is drawn until the
 * state is read, so a user who finished setup never sees it flash.
 *
 * Only setup sits inside the owner boundary: recording and connected apps belong to the
 * owner, while Settings stays reachable without one, as it is today.
 */
export function OnboardingGate({ children }: { children: ReactNode }) {
  const { t } = useI18n();
  const { state, ...progress } = useOnboardingProgress();

  if (state === null) return <div className="app-boot-loading">{t('common.loading')}</div>;
  if (state.status === 'completed') return <>{children}</>;
  return (
    <LocalOwnerBoundary>
      <OnboardingScreen step={state.step} progress={progress} />
    </LocalOwnerBoundary>
  );
}
