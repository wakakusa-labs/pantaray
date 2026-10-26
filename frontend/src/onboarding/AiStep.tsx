import { useState } from 'react';

import { CheckItem } from '../components/Checklist';
import type { ConnectionCommand } from '../../electron/src/ipc/schemas/aiConnection';
import {
  API_KEY_PROVIDERS,
  modelCandidates,
  modelForTarget,
  type ApiKeyProvider,
} from '../pages/settings/aiConnectionModel';
import type { Translate } from '../pages/settings/types';
import type {
  AiConnectionController,
  ConnectionState,
} from '../pages/settings/useAiConnectionController';
import { aiConnectionLabel } from './aiConnectionLabel';
import { KeyField } from './KeyField';
import './onboardingSteps.css';

type Method = 'chatgpt' | 'api_key';
type Row = 'connect' | 'model';

/** Operations only the connect row starts; a saved preference can come from either row. */
const CONNECT_OPERATIONS: readonly ConnectionCommand['operation'][] = [
  'sign_in_chatgpt',
  'cancel_chatgpt_sign_in',
  'save_api_key',
];

/**
 * Setup's AI step: a sign-in or an API key, then the model. `configured` is the parent's read
 * of whether the runtime has a route, which is what lets setup go on.
 */
export function AiStep({
  t,
  connection,
  state,
  configured,
}: {
  t: Translate;
  connection: AiConnectionController;
  state: ConnectionState;
  configured: boolean;
}) {
  const { settings } = state;
  const preferences = settings.preferences;
  const linked =
    preferences.method === 'chatgpt'
      ? settings.chatgpt.status === 'connected'
      : settings.hasSavedApiKey;
  const [method, setMethod] = useState<Method>('chatgpt');
  const [provider, setProvider] = useState<ApiKeyProvider>(preferences.provider);
  // Changing keeps the current connection until another one is made.
  const [changing, setChanging] = useState(false);
  // Which row saved last, since a saved preference can come from either.
  const [lastRow, setLastRow] = useState<Row | null>(null);
  const { pendingOperation, outcome } = connection;
  const busy = pendingOperation !== null;
  const signingIn =
    pendingOperation === 'sign_in_chatgpt' || pendingOperation === 'cancel_chatgpt_sign_in';
  const choosing = !linked || changing;

  const failure =
    outcome && !outcome.result.ok && outcome.result.error !== 'cancelled'
      ? t(`settings.aiConnection.error.${outcome.result.error}`)
      : null;
  const failedRow: Row | null =
    failure === null || outcome === null
      ? null
      : outcome.operation === 'save_preferences'
        ? lastRow
        : CONNECT_OPERATIONS.includes(outcome.operation)
          ? 'connect'
          : null;

  // The model is shared by every method and provider, so one that cannot carry over is reset.
  const savePreferences = (nextMethod: Method, nextProvider: ApiKeyProvider) =>
    connection.run({
      operation: 'save_preferences',
      preferences: {
        method: nextMethod,
        provider: nextProvider,
        model: modelForTarget({
          method: nextMethod,
          provider: nextProvider,
          model: preferences.model,
        }),
      },
    });

  // Signing in first keeps a working API key connection until the new one exists.
  const signIn = async () => {
    setLastRow('connect');
    if (settings.chatgpt.status !== 'connected') {
      if (!(await connection.run({ operation: 'sign_in_chatgpt' }))) return;
    }
    if (await savePreferences('chatgpt', preferences.provider)) setChanging(false);
  };

  const saveApiKey = async (apiKey: string) => {
    setLastRow('connect');
    const saved =
      (await connection.run({ operation: 'save_api_key', provider, apiKey })) &&
      (await savePreferences('api_key', provider));
    if (!saved) throw new Error('AI_CONNECTION_UPDATE_FAILED');
    setChanging(false);
  };

  const saveModel = (model: string) => {
    setLastRow('model');
    void connection.run({ operation: 'save_preferences', preferences: { ...preferences, model } });
  };

  const connectTitle = t('onboarding.step.ai');
  const cancelChange = changing ? (
    <button
      type="button"
      className="settings-text-button"
      disabled={busy}
      onClick={() => setChanging(false)}
    >
      {t('common.cancel')}
    </button>
  ) : null;
  const switchMethod = (next: Method) => (
    <button
      type="button"
      className="settings-text-button"
      disabled={busy}
      onClick={() => setMethod(next)}
    >
      {t(next === 'api_key' ? 'onboarding.ai.useApiKey' : 'onboarding.ai.useChatgpt')}
    </button>
  );

  const connectRow = !choosing ? (
    <CheckItem
      title={connectTitle}
      status="done"
      note={aiConnectionLabel(t, settings)}
      action={() => (
        <>
          <span className="check-item-state">{t('settings.aiConnection.chatgpt.connected')}</span>
          <button
            type="button"
            className="settings-text-button"
            aria-label={`${connectTitle}: ${t('settings.aiConnection.key.replace')}`}
            disabled={busy}
            onClick={() => {
              setMethod(preferences.method);
              setProvider(preferences.provider);
              setChanging(true);
            }}
          >
            {t('settings.aiConnection.key.replace')}
          </button>
        </>
      )}
    />
  ) : method === 'chatgpt' ? (
    <CheckItem
      title={connectTitle}
      problem={failedRow === 'connect' ? failure : null}
      note={signingIn ? t('onboarding.ai.signingIn') : switchMethod('api_key')}
      action={() =>
        signingIn ? (
          <button
            type="button"
            className="settings-text-button"
            disabled={pendingOperation === 'cancel_chatgpt_sign_in'}
            // The sign-in button it replaces had focus.
            autoFocus
            onClick={() => void connection.cancelSignIn()}
          >
            {t('common.cancel')}
          </button>
        ) : (
          <>
            <button
              type="button"
              className="recording-intro-primary"
              disabled={busy}
              onClick={() => void signIn()}
            >
              {t('settings.aiConnection.chatgpt.signIn')}
            </button>
            {cancelChange}
          </>
        )
      }
    />
  ) : (
    <CheckItem
      title={connectTitle}
      problem={failedRow === 'connect' ? failure : null}
      note={switchMethod('chatgpt')}
      action={() => (
        <>
          <KeyField
            // A key typed for one provider is not sent to another.
            key={provider}
            label={t('onboarding.aiMethod.apiKey', {
              provider: t(`settings.aiConnection.provider.${provider}`),
            })}
            saved={false}
            placeholder={t('settings.aiConnection.key.label')}
            disabled={busy}
            before={
              <select
                className="history-filter-select onb-provider-select"
                aria-label={t('settings.aiConnection.providerLabel')}
                value={provider}
                disabled={busy}
                onChange={(event) => setProvider(event.target.value as ApiKeyProvider)}
              >
                {API_KEY_PROVIDERS.map((option) => (
                  <option key={option} value={option}>
                    {t(`settings.aiConnection.provider.${option}`)}
                  </option>
                ))}
              </select>
            }
            onSave={saveApiKey}
            t={t}
          />
          {cancelChange}
        </>
      )}
    />
  );

  // Until a connection exists, the models shown are those of the method being set up.
  const shownMethod = choosing ? method : preferences.method;
  const shownProvider = choosing ? provider : preferences.provider;
  const candidates = modelCandidates(shownMethod, shownProvider);
  const modelLabel = t('settings.aiConnection.modelLabel');

  return (
    <>
      <p className="onb-lead">{t('onboarding.ai.lead')}</p>
      <ul className="checklist">
        {connectRow}
        <CheckItem
          title={modelLabel}
          status={configured ? 'done' : 'pending'}
          disabled={!linked}
          problem={failedRow === 'model' ? failure : null}
          action={() =>
            candidates.length > 0 ? (
              <select
                className="history-filter-select onb-model-select"
                aria-label={modelLabel}
                value={modelForTarget({
                  method: shownMethod,
                  provider: shownProvider,
                  model: preferences.model,
                })}
                disabled={!linked || changing || busy}
                onChange={(event) => saveModel(event.target.value)}
              >
                {candidates.map((candidate) => (
                  <option key={candidate} value={candidate}>
                    {candidate}
                  </option>
                ))}
              </select>
            ) : (
              <ModelInput
                key={shownProvider}
                label={modelLabel}
                saved={preferences.model}
                placeholder={t('settings.aiConnection.modelPlaceholder')}
                disabled={!linked || changing || busy}
                onSave={saveModel}
              />
            )
          }
        />
      </ul>
    </>
  );
}

/** A provider without a model list takes a typed name, saved on Enter or on leaving the field. */
function ModelInput({
  label,
  saved,
  placeholder,
  disabled,
  onSave,
}: {
  label: string;
  saved: string;
  placeholder: string;
  disabled: boolean;
  onSave: (model: string) => void;
}) {
  const [draft, setDraft] = useState(saved);
  // A blank name is never saved: it would leave the connection without a model.
  const commit = () => {
    const next = draft.trim();
    if (next && next !== saved) onSave(next);
  };
  return (
    <form
      onSubmit={(event) => {
        event.preventDefault();
        commit();
      }}
    >
      <input
        className="history-filter-input onb-model-select"
        type="text"
        spellCheck={false}
        aria-label={label}
        placeholder={placeholder}
        value={draft}
        disabled={disabled}
        onChange={(event) => setDraft(event.target.value)}
        onBlur={commit}
      />
    </form>
  );
}
