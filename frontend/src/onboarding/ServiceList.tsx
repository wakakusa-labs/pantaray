import type { IntegrationToolkitStatus } from '../../electron/src/settings/integrationSettingsFetch';
import { IntegrationLogo } from '../pages/settings/components/IntegrationLogo';
import './onboardingSteps.css';

/** Connected services in a note line: each one's logo and name. */
export function ServiceList({ toolkits }: { toolkits: IntegrationToolkitStatus[] }) {
  return (
    <span className="onb-services">
      {toolkits.map((toolkit) => (
        <span key={toolkit.toolkit} className="onb-service">
          <IntegrationLogo toolkit={toolkit} small />
          {toolkit.display_name}
        </span>
      ))}
    </span>
  );
}
