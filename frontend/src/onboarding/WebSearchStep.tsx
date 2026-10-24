import { AiConnectionSettingsSection } from '../pages/settings/components/AiConnectionSettingsSection';
import type { Translate } from '../pages/settings/types';

/** Setup's web search step: the Settings Tavily key field as it is. */
export function WebSearchStep({ t }: { t: Translate }) {
  return (
    <>
      <p className="onb-lead">{t('onboarding.webSearch.lead')}</p>
      <div className="onb-settings-part">
        <AiConnectionSettingsSection part="web_search" />
      </div>
    </>
  );
}
