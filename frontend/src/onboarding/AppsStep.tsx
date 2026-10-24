import { IntegrationSettings } from '../pages/settings/components/IntegrationSettings';
import type { Translate } from '../pages/settings/types';
import { ComposioKeyGuide } from './ComposioKeyGuide';
import '../pages/settings/settingsPage.css';
import './onboardingSteps.css';

/**
 * Setup's apps step: the Settings integrations section with setup's own lead, and the key
 * guide above the key field. The section shows the guide only until a key is
 * saved, then the services. `hasSavedKey` is the parent's read of the same connection
 * state, which it also uses to tell whether the step is done.
 */
export function AppsStep({ t, hasSavedKey }: { t: Translate; hasSavedKey: boolean }) {
  return (
    <div className="onb-apps">
      <IntegrationSettings
        t={t}
        onboarding={{
          lead: (
            <p className="onb-lead">
              {t(hasSavedKey ? 'onboarding.apps.leadWithKey' : 'onboarding.apps.lead')}
            </p>
          ),
          keyGuide: <ComposioKeyGuide t={t} />,
        }}
      />
    </div>
  );
}
