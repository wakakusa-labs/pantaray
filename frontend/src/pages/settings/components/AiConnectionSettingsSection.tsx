import { useEffect, useRef, useState } from 'react';
import { useI18n } from '@/context/useI18n';
import type { ConnectionCommand } from '../../../../electron/src/ipc/schemas/aiConnection';
import { modelForTarget, type AiConnectionState } from '../aiConnectionModel';
import { useAiConnectionController } from '../useAiConnectionController';
import { AiConnectionSection, type AiConnectionActions } from './AiConnectionSection';

export function AiConnectionSettingsSection() {
  const { t } = useI18n();
  const title = t('settings.aiConnection.title');
  const controller = useAiConnectionController();
  const [showCloud, setShowCloud] = useState(true);
  const retryButton = useRef<HTMLButtonElement>(null);
  const content = useRef<HTMLDivElement>(null);
  const wasUnreadable = useRef(false);
  useEffect(() => {
    if (controller.loadError) retryButton.current?.focus();
    else if (wasUnreadable.current) content.current?.focus();
    wasUnreadable.current = controller.loadError !== null;
  }, [controller.loadError, controller.pendingOperation]);

  const { state, outcome, pendingOperation, recovery } = controller;
  const feedback = outcome
    ? {
        message: outcome.result.ok
          ? t(`settings.aiConnection.completed.${outcome.operation}`)
          : t(`settings.aiConnection.error.${outcome.result.error}`),
        isError: !outcome.result.ok && outcome.result.error !== 'cancelled',
      }
    : null;

  async function save(command: ConnectionCommand): Promise<void> {
    if (!(await controller.run(command))) throw new Error('AI_CONNECTION_UPDATE_FAILED');
  }

  let displayState: AiConnectionState | null = null;
  let actions: AiConnectionActions | null = null;
  if (state) {
    const { preferences, chatgpt } = state.settings;
    const cloudActive = state.runtime.ok && state.runtime.status.llmRoute === 'cloud';
    displayState = {
      runtime: state.runtime,
      method: cloudActive && showCloud ? 'cloud' : preferences.method,
      model: preferences.model,
      apiKey: { provider: preferences.provider, hasSavedKey: state.settings.hasSavedApiKey },
      webSearchHasSavedKey: state.settings.hasSavedWebSearchKey,
      canStoreSecrets: state.settings.canStoreSecrets,
      chatgpt:
        chatgpt.status === 'connected' || chatgpt.status === 'reauthentication_required'
          ? { status: chatgpt.status }
          : null,
    };
    actions = {
      // モデルは方式とプロバイダーで共有しているので、切り替え先で使えない名前は持ち越さない。
      selectMethod: (method) => {
        setShowCloud(method === 'cloud');
        if (method !== 'cloud') {
          void controller.run({
            operation: 'save_preferences',
            preferences: {
              ...preferences,
              method,
              model: modelForTarget({ ...preferences, method }),
            },
          });
        }
      },
      selectProvider: (provider) => {
        void controller.run({
          operation: 'save_preferences',
          preferences: {
            ...preferences,
            provider,
            model: modelForTarget({ ...preferences, provider }),
          },
        });
      },
      saveModel: (model) =>
        save({ operation: 'save_preferences', preferences: { ...preferences, model } }),
      saveApiKey: (apiKey) =>
        save({ operation: 'save_api_key', provider: preferences.provider, apiKey }),
      clearApiKey: () => save({ operation: 'remove_api_key', provider: preferences.provider }),
      saveWebSearchKey: (apiKey) => save({ operation: 'save_web_search_key', apiKey }),
      clearWebSearchKey: () => save({ operation: 'remove_web_search_key' }),
      signInToChatgpt: () => void controller.run({ operation: 'sign_in_chatgpt' }),
      cancelChatgptSignIn: () => void controller.cancelSignIn(),
      disconnectChatgpt: () => void controller.run({ operation: 'disconnect_chatgpt' }),
    };
  }

  return (
    <>
      {controller.loadError ? (
        <section className="dashboard-section">
          <h3 className="dashboard-section-title">{title}</h3>
          <p role="alert" className="settings-section-error">
            {t('settings.aiConnection.loadFailed')}{' '}
            {t(`settings.aiConnection.error.${controller.loadError}`)}
          </p>
          <button
            type="button"
            className="settings-action-button"
            ref={retryButton}
            onClick={() => void controller.refresh()}
          >
            {t('settings.aiConnection.retry')}
          </button>
          {recovery ? (
            <button
              type="button"
              className="settings-text-button"
              disabled={pendingOperation !== null}
              onClick={() => void controller.run(recovery)}
            >
              {t(
                `settings.aiConnection.recovery.${recovery.operation}`,
                recovery.operation === 'remove_api_key'
                  ? { provider: t(`settings.aiConnection.provider.${recovery.provider}`) }
                  : undefined
              )}
            </button>
          ) : null}
          {pendingOperation === 'sign_in_chatgpt' ? (
            <button
              type="button"
              className="settings-text-button"
              onClick={() => void controller.cancelSignIn()}
            >
              {t('settings.aiConnection.chatgpt.cancel')}
            </button>
          ) : null}
          <p role="status" className={feedback?.isError ? 'settings-section-error' : 'ai-note'}>
            {pendingOperation ? t('settings.aiConnection.operation.running') : ''}{' '}
            {feedback?.message}
          </p>
        </section>
      ) : !state ? (
        <p role="status">{t('settings.loadingStatus')}</p>
      ) : null}
      {/* Keep input drafts across failed reads, while hiding metadata that is no longer verified. */}
      <div
        ref={content}
        role="region"
        aria-label={title}
        tabIndex={-1}
        hidden={controller.loadError !== null}
      >
        {displayState && actions ? (
          <AiConnectionSection
            state={displayState}
            actions={actions}
            t={t}
            pendingOperation={pendingOperation}
            feedback={feedback}
          />
        ) : null}
      </div>
    </>
  );
}
