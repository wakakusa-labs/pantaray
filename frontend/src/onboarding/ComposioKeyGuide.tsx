import { Fragment } from 'react';

import type { Translate } from '../pages/settings/types';
import './onboardingSteps.css';

const COMPOSIO_SIGN_UP_URL = 'https://composio.dev';
const COMPOSIO_DASHBOARD_URL = 'https://dashboard.composio.dev';

/** The key's permissions as Composio's API key form names them, so they are not translated. */
const KEY_SCOPES = [
  ['Auth configs', 'Read and write'],
  ['Connected accounts', 'Read and write'],
  ['Session management', 'Read and write'],
  ['Session tool execution', 'Write only'],
] as const;

/** How to get a Composio project API key, shown above the key field until one is saved. */
export function ComposioKeyGuide({ t }: { t: Translate }) {
  return (
    <ol className="onb-guide">
      <li>
        <span className="onb-guide-title">{t('onboarding.apps.guide.signUp')}</span>
        <ExternalLink href={COMPOSIO_SIGN_UP_URL} text={t('onboarding.apps.guide.signUpLink')} />
      </li>
      <li>
        <span className="onb-guide-title">{t('onboarding.apps.guide.project')}</span>
        <span className="ai-note">{t('onboarding.apps.guide.projectNote')}</span>
        <ExternalLink
          href={COMPOSIO_DASHBOARD_URL}
          text={t('onboarding.apps.guide.dashboardLink')}
        />
      </li>
      <li>
        <span className="onb-guide-title">{t('onboarding.apps.guide.createKey')}</span>
        <span className="ai-note">{t('onboarding.apps.guide.createKeyNote')}</span>
        <dl className="onb-scopes">
          {KEY_SCOPES.map(([scope, access]) => (
            <Fragment key={scope}>
              <dt>{scope}</dt>
              <dd>{access}</dd>
            </Fragment>
          ))}
        </dl>
      </li>
      <li>
        <span className="onb-guide-title">{t('onboarding.apps.guide.paste')}</span>
        <span className="ai-note">{t('onboarding.apps.guide.pasteNote')}</span>
      </li>
    </ol>
  );
}

function ExternalLink({ href, text }: { href: string; text: string }) {
  return (
    <a className="settings-inline-link" href={href} target="_blank" rel="noopener noreferrer">
      {text}
      <span aria-hidden="true">↗</span>
    </a>
  );
}
