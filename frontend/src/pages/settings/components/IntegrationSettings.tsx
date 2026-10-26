import { useAuth } from '@/hooks/useAuth';
import type { ConnectionCommand } from '../../../../electron/src/ipc/schemas/aiConnection';
import type { Translate } from '../types';
import { useAiConnectionController } from '../useAiConnectionController';
import { ConnectionLoadFailure } from './ConnectionLoadFailure';
import { useIntegrationConnectFlow } from './IntegrationConnectFlow';
import { useIntegrationSettingsController } from './IntegrationSettingsController';
import { IntegrationSettingsSection } from './IntegrationSettingsSection';

/**
 * The key belongs to the installation, like the other connection settings; the services and
 * accounts belong to the local owner.
 */
export function IntegrationSettings({ t }: { t: Translate }) {
  const { runtimeState } = useAuth();
  const ownerId = runtimeState.status === 'ready' ? (runtimeState.owner?.id ?? null) : null;
  const integrations = useIntegrationSettingsController(ownerId);
  const connection = useAiConnectionController();
  const { flow, actions: flowActions } = useIntegrationConnectFlow(ownerId, integrations.reload);
  const { state, loadError } = connection;

  async function saveKey(command: ConnectionCommand): Promise<void> {
    const saved = await connection.run(command);
    // Saving or removing the key adds or removes the backend's connection method.
    await integrations.reload();
    if (!saved) throw new Error('INTEGRATION_KEY_UPDATE_FAILED');
  }

  if (loadError) {
    return (
      <div className="dashboard-section">
        <h3 className="dashboard-section-title">{t('settings.integrations.title')}</h3>
        <ConnectionLoadFailure t={t} connection={connection} error={loadError} />
      </div>
    );
  }
  if (!state) return <p role="status">{t('settings.loadingStatus')}</p>;

  return (
    <IntegrationSettingsSection
      t={t}
      integrationKey={{
        hasSavedKey: state.settings.hasSavedIntegrationKey,
        canStoreSecrets: state.settings.canStoreSecrets,
        disabled: connection.pendingOperation !== null,
        onSave: (apiKey) => saveKey({ operation: 'save_integration_key', apiKey }),
        onClear: () => saveKey({ operation: 'remove_integration_key' }),
      }}
      toolkits={integrations.toolkits}
      keyRejected={integrations.keyRejected}
      loadFailed={integrations.loadFailed}
      onReload={() => void integrations.reload()}
      rows={integrations.rows}
      actions={integrations.actions}
      flow={flow}
      flowActions={flowActions}
    />
  );
}
