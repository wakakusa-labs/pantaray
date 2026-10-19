import { useState } from 'react';

import type { IntegrationToolkitStatus } from '../../../../electron/src/settings/integrationSettingsFetch';

/** Decorative: the service's name is always written next to it. */
export function IntegrationLogo({
  toolkit,
  small = false,
}: {
  toolkit: IntegrationToolkitStatus;
  small?: boolean;
}) {
  const [failed, setFailed] = useState(false);
  const className = `integration-logo${small ? ' integration-logo--small' : ''}`;
  if (toolkit.logo_url === null || failed) {
    return (
      <span className={`${className} integration-logo--letter`} aria-hidden="true">
        {toolkit.display_name.slice(0, 1).toUpperCase()}
      </span>
    );
  }
  return (
    <img className={className} src={toolkit.logo_url} alt="" onError={() => setFailed(true)} />
  );
}
