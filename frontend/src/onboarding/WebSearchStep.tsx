import { CheckItem } from '../components/Checklist';
import type { Translate } from '../pages/settings/types';
import type {
  AiConnectionController,
  ConnectionState,
} from '../pages/settings/useAiConnectionController';
import { KeyField } from './KeyField';
import './onboardingSteps.css';

const TAVILY_KEYS_URL = 'https://app.tavily.com';

/** Setup's web search step: the Tavily key. */
export function WebSearchStep({
  t,
  connection,
  state,
}: {
  t: Translate;
  connection: AiConnectionController;
  state: ConnectionState;
}) {
  const { outcome } = connection;
  const saved = state.settings.hasSavedWebSearchKey;
  const title = t('onboarding.webSearch.keyTitle');

  return (
    <>
      <p className="onb-lead">{t('onboarding.webSearch.lead')}</p>
      <ul className="checklist">
        <CheckItem
          title={title}
          status={saved ? 'done' : 'pending'}
          problem={
            outcome?.operation === 'save_web_search_key' && !outcome.result.ok
              ? t(`settings.aiConnection.error.${outcome.result.error}`)
              : null
          }
          note={
            saved ? null : (
              <a
                className="settings-inline-link"
                href={TAVILY_KEYS_URL}
                target="_blank"
                rel="noopener noreferrer"
              >
                {t('onboarding.webSearch.getKey')}
                <span aria-hidden="true"> ↗</span>
              </a>
            )
          }
          action={() => (
            <KeyField
              label={title}
              saved={saved}
              placeholder={t('settings.aiConnection.webSearch.keyPlaceholder')}
              disabled={connection.pendingOperation !== null}
              onSave={async (apiKey) => {
                if (!(await connection.run({ operation: 'save_web_search_key', apiKey }))) {
                  throw new Error('WEB_SEARCH_KEY_SAVE_FAILED');
                }
              }}
              t={t}
            />
          )}
        />
      </ul>
    </>
  );
}
