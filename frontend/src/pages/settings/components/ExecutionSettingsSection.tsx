import { useEffect, useState } from 'react';
import { Settings2 } from 'lucide-react';
import { useNavigate } from 'react-router-dom';

import { CommandNetworkSettingsSection } from './CommandNetworkSettingsSection';

import type { MessageKey } from '@/i18n/types';
import { clearWorkspaceSettingsCache } from './workspaceSettingsCache';

type ReadAccessScope = 'workspace' | 'full_access';

type ExecutionSettingsSectionProps = {
  t: (key: MessageKey, vars?: Record<string, string | number>) => string;
};

export function ExecutionSettingsSection({ t }: ExecutionSettingsSectionProps) {
  const navigate = useNavigate();
  const [readAccessScope, setReadAccessScope] = useState<ReadAccessScope | null>(null);
  const [isSavingReadScope, setIsSavingReadScope] = useState(false);
  const [readScopeErrorMessage, setReadScopeErrorMessage] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    const loadReadAccessScope = async () => {
      try {
        const workspaceSettingsApi = window.electron?.workspaceSettings;
        if (!workspaceSettingsApi) {
          throw new Error('Workspace settings bridge is unavailable.');
        }
        const response = await workspaceSettingsApi.getReadAccessScope();
        if (cancelled) return;
        setReadAccessScope(response.read_access_scope);
        setReadScopeErrorMessage(null);
      } catch {
        if (cancelled) return;
        setReadScopeErrorMessage(t('settings.readAccessScope.loadFailed'));
        setReadAccessScope(null);
      }
    };

    void loadReadAccessScope();
    return () => {
      cancelled = true;
    };
  }, [t]);

  const updateReadAccessScope = async (nextScope: ReadAccessScope) => {
    if (readAccessScope === nextScope || isSavingReadScope) return;
    const previousScope = readAccessScope;
    setReadAccessScope(nextScope);
    setIsSavingReadScope(true);
    setReadScopeErrorMessage(null);
    try {
      const workspaceSettingsApi = window.electron?.workspaceSettings;
      if (!workspaceSettingsApi) {
        throw new Error('Workspace settings bridge is unavailable.');
      }
      const response = await workspaceSettingsApi.updateReadAccessScope(nextScope);
      clearWorkspaceSettingsCache();
      setReadAccessScope(response.read_access_scope);
    } catch {
      setReadAccessScope(previousScope);
      setReadScopeErrorMessage(t('settings.readAccessScope.saveFailed'));
    } finally {
      setIsSavingReadScope(false);
    }
  };

  const isFullAccess = readAccessScope === 'full_access';
  const readAccessLabel =
    readAccessScope === 'full_access'
      ? t('settings.readAccessScope.fullAccess')
      : readAccessScope === 'workspace'
        ? t('settings.readAccessScope.workspace')
        : t('settings.readAccessScope.unavailable');

  return (
    <div className="dashboard-section execution-settings-section">
      <h3 className="dashboard-section-title">{t('settings.execution.title')}</h3>

      <div className="settings-control-row">
        <div className="settings-control-copy">
          <div className="settings-control-title">{t('settings.readAccessScope.title')}</div>
          <div className="settings-section-status">{readAccessLabel}</div>
          <p className="dashboard-section-description settings-control-description">
            {t(
              isFullAccess
                ? 'settings.readAccessScope.fullAccessDescription'
                : 'settings.readAccessScope.workspaceDescription'
            )}
          </p>
          <div className="settings-control-footnote">
            <span>{t('settings.readAccessScope.workspaceBoundary')}</span>
            <button
              type="button"
              className="settings-inline-link"
              onClick={() => navigate('/history')}
            >
              <Settings2 size={13} aria-hidden="true" />
              <span>{t('settings.readAccessScope.openWorkspace')}</span>
            </button>
          </div>
        </div>
        <button
          type="button"
          role="switch"
          aria-checked={isFullAccess}
          aria-label={t('settings.readAccessScope.title')}
          className={[
            'settings-toggle',
            'settings-toggle--fixed',
            isFullAccess ? 'settings-toggle--on' : null,
          ]
            .filter(Boolean)
            .join(' ')}
          disabled={isSavingReadScope || readAccessScope === null}
          onClick={() => void updateReadAccessScope(isFullAccess ? 'workspace' : 'full_access')}
        >
          <span className="settings-toggle-thumb" />
          <span className="settings-toggle-label">
            {isFullAccess
              ? t('settings.readAccessScope.toggle.fullAccess')
              : t('settings.readAccessScope.toggle.workspace')}
          </span>
        </button>
      </div>
      {readScopeErrorMessage ? (
        <p className="dashboard-section-description settings-section-error">
          {readScopeErrorMessage}
        </p>
      ) : null}
      <div className="settings-section-divider" />
      <CommandNetworkSettingsSection t={t} />
    </div>
  );
}
