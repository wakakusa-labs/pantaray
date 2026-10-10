import React, { useEffect, useRef, useState } from 'react';

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

const METHODS: readonly AiConnectionMethod[] = PANTARAY_ACCOUNT_LOGIN_ENABLED
  ? ['cloud', 'chatgpt', 'api_key']
  : ['chatgpt', 'api_key'];

function SecretRow({
  label,
  hasSavedKey,
  placeholder,
  inputId,
  disabled,
  onSave,
  onClear,
  t,
}: {
  label: string;
  hasSavedKey: boolean;
  placeholder: string;
  inputId: string;
  disabled: boolean;
  onSave: (value: string) => Promise<void> | void;
  onClear: () => Promise<void> | void;
  t: Translate;
}): React.JSX.Element {
  const [draft, setDraft] = useState('');
  const [editing, setEditing] = useState(false);
  const [saving, setSaving] = useState(false);
  const [removing, setRemoving] = useState(false);
  const [result, setResult] = useState<
    'saveFailed' | 'removeFailed' | 'saveSuccess' | 'removed' | null
  >(null);
  const replaceRef = useRef<HTMLButtonElement>(null);
  const removeRef = useRef<HTMLButtonElement>(null);
  const focusAfter = useRef<'input' | 'replace' | 'remove' | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  // 「変更」で消えたボタンからフォーカスが落ちないよう、入力欄へ移す。
  useEffect(() => {
    if (editing) inputRef.current?.focus();
  }, [editing]);

  useEffect(() => {
    if (saving || removing) return;
    const target =
      focusAfter.current === 'replace'
        ? replaceRef.current
        : focusAfter.current === 'remove'
          ? hasSavedKey
            ? removeRef.current
            : inputRef.current
          : focusAfter.current === 'input'
            ? inputRef.current
            : null;
    if (target && !target.matches(':disabled')) {
      target.focus();
      focusAfter.current = null;
    }
  }, [editing, hasSavedKey, saving, removing, disabled]);

  const save = async (): Promise<void> => {
    const value = draft.trim();
    if (!value) return;
    setSaving(true);
    setEditing(true);
    setResult(null);
    try {
      await onSave(value);
    } catch {
      // 保存後の反映で失敗する場合もあるため、再試行できるよう入力を残す。
      setResult('saveFailed');
      focusAfter.current = 'input';
      return;
    } finally {
      setSaving(false);
    }
    setDraft('');
    setEditing(false);
    setResult('saveSuccess');
    focusAfter.current = 'replace';
  };

  const clear = async (): Promise<void> => {
    setRemoving(true);
    setResult(null);
    try {
      await onClear();
      setResult('removed');
      focusAfter.current = 'input';
    } catch {
      setResult('removeFailed');
      focusAfter.current = 'remove';
    } finally {
      setRemoving(false);
    }
  };

  const feedback = (
    <span
      className={result?.endsWith('Failed') ? 'ai-check ai-check--failed' : 'ai-note'}
      role="status"
    >
      {saving
        ? t('settings.aiConnection.key.saving')
        : removing
          ? t('settings.aiConnection.key.removing')
          : result
            ? t(`settings.aiConnection.key.${result}`)
            : ''}
    </span>
  );

  if (hasSavedKey && !editing) {
    return (
      <Row label={label}>
        <div className="ai-inline">
          <span className="ai-secret-mask">••••••••••••</span>
          <span className="ai-note">{t('settings.aiConnection.key.saved')}</span>
          <button
            type="button"
            className="settings-text-button"
            aria-label={`${label}: ${t('settings.aiConnection.key.replace')}`}
            ref={replaceRef}
            disabled={disabled || saving || removing}
            onClick={() => {
              setResult(null);
              setEditing(true);
            }}
          >
            {t('settings.aiConnection.key.replace')}
          </button>
          <button
            type="button"
            className="settings-text-button"
            aria-label={`${label}: ${t('settings.aiConnection.key.remove')}`}
            ref={removeRef}
            disabled={saving || removing}
            onClick={() => void clear()}
          >
            {t('settings.aiConnection.key.remove')}
          </button>
          {feedback}
        </div>
      </Row>
    );
  }

  return (
    <Row label={label} htmlFor={inputId}>
      <div className="ai-inline">
        <input
          id={inputId}
          ref={inputRef}
          className="history-filter-input"
          type="password"
          autoComplete="off"
          spellCheck={false}
          disabled={disabled || saving || removing}
          placeholder={placeholder}
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
        />
        <button
          type="button"
          className="settings-action-button"
          aria-label={`${label}: ${t('settings.aiConnection.key.save')}`}
          disabled={disabled || saving || removing || draft.trim().length === 0}
          onClick={() => void save()}
        >
          {t('settings.aiConnection.key.save')}
        </button>
        {hasSavedKey ? (
          <button
            type="button"
            className="settings-text-button"
            aria-label={`${label}: ${t('settings.aiConnection.key.cancel')}`}
            disabled={saving}
            onClick={() => {
              setDraft('');
              setResult(null);
              setEditing(false);
              focusAfter.current = 'replace';
            }}
          >
            {t('settings.aiConnection.key.cancel')}
          </button>
        ) : null}
        {feedback}
      </div>
    </Row>
  );
}

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
          <div
            className="ai-segmented"
            role="radiogroup"
            aria-label={t('settings.aiConnection.methodTitle')}
            aria-describedby={
              methodNoteId ?? (cloudSession === 'absent' ? 'ai-method-login' : undefined)
            }
          >
            {METHODS.map((method) => {
              const selected = state.method === method;
              const disabled =
                method === 'cloud' && (cloudSession === 'absent' || cloudSession === null);
              return (
                // 素の radio にして、矢印キーでの移動と読み上げをブラウザに任せる。
                <label
                  key={method}
                  className={['ai-segment', selected ? 'ai-segment--selected' : null]
                    .filter(Boolean)
                    .join(' ')}
                >
                  <input
                    className="ai-segment-input"
                    type="radio"
                    name="ai-connection-method"
                    value={method}
                    checked={selected}
                    disabled={disabled}
                    onChange={() => actions.selectMethod(method)}
                  />
                  {t(`settings.aiConnection.method.${method}.title`)}
                </label>
              );
            })}
          </div>
          {methodNoteId ? (
            <p className="ai-note ai-note--control" id={methodNoteId}>
              {t('settings.aiConnection.status.expiredDetail')}
            </p>
          ) : null}
          {PANTARAY_ACCOUNT_LOGIN_ENABLED && cloudSession === 'absent' ? (
            <p className="ai-note ai-note--control" id="ai-method-login">
              {t('settings.aiConnection.method.cloud.requiresLogin')}
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
          t={t}
        />
      </fieldset>
    </section>
  );
}
