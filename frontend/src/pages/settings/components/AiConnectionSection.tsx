import React, { useEffect, useRef } from 'react';

import { MenuButton, type MenuButtonItem } from '@/components/ui/MenuButton';
import type { Translate } from '../types';
import {
  API_KEY_PROVIDERS,
  PROVIDER_MODELS,
  modelCandidates,
  type AiConnectionMethod,
  type AiConnectionState,
  type ApiKeyProvider,
} from '../aiConnectionModel';
import type { ConnectionCommand } from '../../../../electron/src/ipc/schemas/aiConnection';
import { PANTARAY_ACCOUNT_LOGIN_ENABLED } from '../../../../electron/src/auth/accountLoginFeature';
import { AiConnectionRow as Row } from './AiConnectionRow';
import { AiConnectionModelRow } from './AiConnectionModelRow';
import { SecretRow } from './SecretRow';
import './aiConnection.css';

export type AiConnectionActions = {
  selectMethod: (method: AiConnectionMethod) => void;
  selectProvider: (provider: ApiKeyProvider) => void;
  saveModel: (model: string) => Promise<void>;
  /** 保存の完了まで待てるよう、結果を返す。失敗したら入力を捨てない。 */
  saveApiKey: (apiKey: string) => Promise<void> | void;
  clearApiKey: () => Promise<void> | void;
  signInToChatgpt: () => void;
  cancelChatgptSignIn: () => void;
  disconnectChatgpt: () => void;
  saveWebSearchKey: (apiKey: string) => Promise<void> | void;
  clearWebSearchKey: () => Promise<void> | void;
};

type AiConnectionSectionProps = {
  state: AiConnectionState;
  actions: AiConnectionActions;
  t: Translate;
  pendingOperation: ConnectionCommand['operation'] | null;
  feedback: { message: string; isError: boolean } | null;
};

const TAVILY_KEYS_URL = 'https://app.tavily.com';

const METHODS: readonly AiConnectionMethod[] = PANTARAY_ACCOUNT_LOGIN_ENABLED
  ? ['cloud', 'chatgpt', 'api_key']
  : ['chatgpt', 'api_key'];

export function AiConnectionSection({
  state,
  actions,
  t,
  pendingOperation,
  feedback,
}: AiConnectionSectionProps): React.JSX.Element {
  const loginButton = useRef<HTMLButtonElement>(null);
  const cancelButton = useRef<HTMLButtonElement>(null);
  const restoreAccountFocus = useRef(false);
  useEffect(() => {
    if (pendingOperation === 'sign_in_chatgpt') {
      cancelButton.current?.focus();
      restoreAccountFocus.current = true;
    } else if (pendingOperation === 'disconnect_chatgpt') {
      restoreAccountFocus.current = true;
    } else if (pendingOperation === null && restoreAccountFocus.current) {
      loginButton.current?.focus();
      restoreAccountFocus.current = false;
    }
  }, [pendingOperation]);

  const status = state.runtime.ok ? state.runtime.status : null;
  const webSearchRoute = status?.webSearchRoute ?? null;
  const cloudSession = status?.cloudSessionState ?? null;

  const candidates = modelCandidates(state.method, state.apiKey.provider);
  const methodNoteId =
    state.method === 'cloud' && cloudSession === 'expired' ? 'ai-method-note' : undefined;
  const methodTitle = t(`settings.aiConnection.method.${state.method}.title`);
  const methodItems: MenuButtonItem[] = METHODS.map((method) => ({
    key: method,
    title: t(`settings.aiConnection.method.${method}.title`),
    description: t(`settings.aiConnection.method.${method}.description`),
    // Pantaray Cloud needs a cloud session; without the runtime status none is known.
    unavailableReason:
      method !== 'cloud'
        ? null
        : cloudSession === 'absent'
          ? t('settings.aiConnection.method.cloud.requiresLogin')
          : cloudSession === null
            ? t('settings.aiConnection.method.cloud.statusUnknown')
            : null,
    checked: state.method === method,
    onSelect: () => {
      if (state.method !== method) actions.selectMethod(method);
    },
  }));

  return (
    <section className="dashboard-section ai-connection">
      <h3 className="dashboard-section-title">{t('settings.aiConnection.title')}</h3>

      {!state.canStoreSecrets ? (
        <p className="settings-section-error">{t('settings.aiConnection.secretsUnavailable')}</p>
      ) : null}

      <p role="status" className={feedback?.isError ? 'settings-section-error' : 'ai-note'}>
        {pendingOperation === 'sign_in_chatgpt'
          ? t('settings.aiConnection.chatgpt.waiting')
          : pendingOperation
            ? t('settings.aiConnection.operation.running')
            : ''}
        {feedback?.message}
      </p>
      {pendingOperation === 'sign_in_chatgpt' ? (
        <button
          type="button"
          className="settings-text-button"
          ref={cancelButton}
          onClick={actions.cancelChatgptSignIn}
        >
          {t('settings.aiConnection.chatgpt.cancel')}
        </button>
      ) : null}
      <fieldset
        className="ai-controls"
        aria-label={t('settings.aiConnection.title')}
        disabled={pendingOperation !== null}
      >
        <h4 className="ai-group">{t('settings.aiConnection.inferenceGroup')}</h4>

        <Row label={t('settings.aiConnection.methodTitle')}>
          <MenuButton
            label={`${t('settings.aiConnection.methodTitle')}: ${methodTitle}`}
            align="start"
            describedBy={methodNoteId}
            items={methodItems}
          >
            <span>{methodTitle}</span>
            <span className="ai-caret" aria-hidden="true">
              ▾
            </span>
          </MenuButton>
          {methodNoteId ? (
            <p className="ai-note ai-note--control" id={methodNoteId}>
              {t('settings.aiConnection.status.expiredDetail')}
            </p>
          ) : null}
        </Row>

        {state.method === 'chatgpt' ? (
          <>
            <Row label={t('settings.aiConnection.chatgpt.account')}>
              {state.chatgpt ? (
                <div className="ai-inline">
                  {state.chatgpt.status === 'connected' ? (
                    <span className="ai-value">{t('settings.aiConnection.chatgpt.connected')}</span>
                  ) : (
                    <>
                      <span className="ai-check ai-check--failed" role="status">
                        {t('settings.aiConnection.chatgpt.reauthenticationRequired')}
                      </span>
                      <button
                        type="button"
                        className="settings-text-button"
                        disabled={!state.canStoreSecrets || pendingOperation !== null}
                        ref={loginButton}
                        onClick={actions.signInToChatgpt}
                      >
                        {t('settings.aiConnection.chatgpt.reauthenticate')}
                      </button>
                    </>
                  )}
                  <button
                    type="button"
                    className="settings-text-button"
                    // 接続済みでは再ログインを出さないので、ログイン後のフォーカスはここに戻す。
                    ref={state.chatgpt.status === 'connected' ? loginButton : undefined}
                    onClick={actions.disconnectChatgpt}
                  >
                    {t('settings.aiConnection.chatgpt.disconnect')}
                  </button>
                </div>
              ) : (
                <div className="ai-inline">
                  <button
                    type="button"
                    className="settings-action-button"
                    disabled={!state.canStoreSecrets || pendingOperation !== null}
                    ref={loginButton}
                    onClick={actions.signInToChatgpt}
                  >
                    {t('settings.aiConnection.chatgpt.signIn')}
                  </button>
                  <span className="ai-note">{t('settings.aiConnection.chatgpt.signInHint')}</span>
                </div>
              )}
            </Row>
            <AiConnectionModelRow
              key={`${state.method}:${state.apiKey.provider}`}
              model={state.model}
              candidates={candidates}
              selectionOnly
              onSave={actions.saveModel}
              t={t}
            />
          </>
        ) : null}

        {state.method === 'api_key' ? (
          <>
            <Row label={t('settings.aiConnection.providerLabel')} htmlFor="ai-provider">
              <select
                id="ai-provider"
                className="history-filter-select"
                value={state.apiKey.provider}
                onChange={(event) => actions.selectProvider(event.target.value as ApiKeyProvider)}
              >
                {API_KEY_PROVIDERS.map((provider) => (
                  <option key={provider} value={provider}>
                    {t(`settings.aiConnection.provider.${provider}`)}
                  </option>
                ))}
              </select>
            </Row>

            <AiConnectionModelRow
              key={`${state.method}:${state.apiKey.provider}`}
              model={state.model}
              candidates={candidates}
              allowsOther={PROVIDER_MODELS[state.apiKey.provider].allowsOther}
              onSave={actions.saveModel}
              t={t}
            />

            <SecretRow
              // プロバイダーを変えたら入力中のキーを持ち越さない。別の宛先へ送らないため。
              key={state.apiKey.provider}
              label={t('settings.aiConnection.key.label')}
              inputId="ai-api-key"
              disabled={!state.canStoreSecrets || pendingOperation !== null}
              hasSavedKey={state.apiKey.hasSavedKey}
              placeholder={t('settings.aiConnection.key.placeholder')}
              onSave={actions.saveApiKey}
              onClear={actions.clearApiKey}
              t={t}
            />
          </>
        ) : null}

        <div className="ai-rule" />

        <h4 className="ai-group">{t('settings.aiConnection.webSearch.title')}</h4>
        <p className="ai-note ai-group-note">
          {webSearchRoute === 'cloud'
            ? t('settings.aiConnection.webSearch.cloudDescription')
            : t('settings.aiConnection.webSearch.description')}
        </p>

        <SecretRow
          label={t('settings.aiConnection.webSearch.keyLabel')}
          inputId="ai-web-search-key"
          disabled={!state.canStoreSecrets || pendingOperation !== null}
          hasSavedKey={state.webSearchHasSavedKey}
          placeholder={t('settings.aiConnection.webSearch.keyPlaceholder')}
          onSave={actions.saveWebSearchKey}
          onClear={actions.clearWebSearchKey}
          hint={
            <a
              className="settings-inline-link"
              href={TAVILY_KEYS_URL}
              target="_blank"
              rel="noopener noreferrer"
            >
              {t('settings.aiConnection.webSearch.getKey')}
            </a>
          }
          t={t}
        />
      </fieldset>
    </section>
  );
}
