import type { Translate } from '../pages/settings/types';
import type { ConnectionState } from '../pages/settings/useAiConnectionController';

/** How Pantaray reaches its model, in the words the user chose it by. */
export function aiConnectionLabel(t: Translate, settings: ConnectionState['settings']): string {
  const { method, provider } = settings.preferences;
  return method === 'chatgpt'
    ? t('settings.aiConnection.method.chatgpt.title')
    : t('onboarding.aiMethod.apiKey', {
        provider: t(`settings.aiConnection.provider.${provider}`),
      });
}
