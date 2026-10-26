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

/** Composio's steps to a project API key, folded until asked for. */
export function ComposioKeyGuide({ t }: { t: Translate }) {
  return (
    <details className="onb-guide">
      <summary>{t('onboarding.apps.guide.summary')}</summary>
      <ol>
        <li>
          {t('onboarding.apps.guide.signUp')}{' '}
          <ExternalLink href={COMPOSIO_SIGN_UP_URL} text={t('onboarding.apps.guide.signUpLink')} />
        </li>
        <li>
          {t('onboarding.apps.guide.project')}{' '}
          <ExternalLink
            href={COMPOSIO_DASHBOARD_URL}
            text={t('onboarding.apps.guide.dashboardLink')}
          />
        </li>
        <li>
          {t('onboarding.apps.guide.createKey')}
          <dl className="onb-scopes">
            {KEY_SCOPES.map(([scope, access]) => (
              <Fragment key={scope}>
                <dt>{scope}</dt>
                <dd>{access}</dd>
              </Fragment>
            ))}
          </dl>
        </li>
      </ol>
    </details>
  );
}

function ExternalLink({ href, text }: { href: string; text: string }) {
  return (
    <a className="settings-inline-link" href={href} target="_blank" rel="noopener noreferrer">
      {text}
      <span aria-hidden="true"> ↗</span>
    </a>
  );
}
