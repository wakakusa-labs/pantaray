import { AiConnectionSettingsSection } from '../pages/settings/components/AiConnectionSettingsSection';
import type { Translate } from '../pages/settings/types';

/** Setup's AI step: the Settings connection controls without the web search key. */
export function AiStep({ t }: { t: Translate }) {
  return (
    <>
      <p className="onb-lead">{t('onboarding.ai.lead')}</p>
      <p className="onb-sub">{t('onboarding.ai.sub')}</p>
      <div className="onb-settings-part">
        <AiConnectionSettingsSection part="inference" />
      </div>
    </>
  );
}
