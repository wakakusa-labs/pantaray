import { useEffect, useState } from 'react';
import { Settings2 } from 'lucide-react';
import { useNavigate } from 'react-router-dom';

import { CommandNetworkSettingsSection } from './CommandNetworkSettingsSection';

import type { MessageKey } from '@/i18n/types';
import { clearWorkspaceSettingsCache } from './workspaceSettingsCache';

type ApprovalMode = 'prompt_each_time' | 'always_allow';
type ReadAccessScope = 'workspace' | 'full_access';

type ApprovalModeSectionProps = {
  t: (key: MessageKey, vars?: Record<string, string | number>) => string;
};

export function ApprovalModeSection({ t }: ApprovalModeSectionProps) {
  const navigate = useNavigate();
  const [approvalMode, setApprovalMode] = useState<ApprovalMode | null>(null);
  const [readAccessScope, setReadAccessScope] = useState<ReadAccessScope | null>(null);
  const [isSavingApproval, setIsSavingApproval] = useState(false);
  const [isSavingReadScope, setIsSavingReadScope] = useState(false);
  const [approvalErrorMessage, setApprovalErrorMessage] = useState<string | null>(null);
  const [readScopeErrorMessage, setReadScopeErrorMessage] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    const loadApprovalMode = async () => {
      try {
        const approvalApi = window.electron?.approval;
        if (!approvalApi) {
          throw new Error('Approval settings bridge is unavailable.');
        }
        const response = await approvalApi.getWorkspaceEditCommandPreference();
        if (cancelled) return;
        setApprovalMode(response.approval_mode);
        setApprovalErrorMessage(null);
      } catch {
        if (cancelled) return;
        setApprovalErrorMessage(t('settings.approvalMode.loadFailed'));
        setApprovalMode(null);
      }
    };

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

    void Promise.all([loadApprovalMode(), loadReadAccessScope()]);
    return () => {
      cancelled = true;
    };
  }, [t]);

  const updateApprovalMode = async (nextMode: ApprovalMode) => {
    if (approvalMode === nextMode || isSavingApproval) return;
    const previousMode = approvalMode;
    setApprovalMode(nextMode);
    setIsSavingApproval(true);
    setApprovalErrorMessage(null);
    try {
      const approvalApi = window.electron?.approval;
      if (!approvalApi) {
        throw new Error('Approval settings bridge is unavailable.');
      }
      const response = await approvalApi.setWorkspaceEditCommandPreference(nextMode);
      setApprovalMode(response.approval_mode);
    } catch {
      setApprovalMode(previousMode);
      setApprovalErrorMessage(t('settings.approvalMode.saveFailed'));
    } finally {
      setIsSavingApproval(false);
    }
  };

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

  const isAlwaysAllow = approvalMode === 'always_allow';
  const isFullAccess = readAccessScope === 'full_access';
  const modeLabel =
    approvalMode === 'always_allow'
      ? t('settings.approvalMode.mode.alwaysAllow')
      : approvalMode === 'prompt_each_time'
        ? t('settings.approvalMode.mode.promptEachTime')
        : t('settings.approvalMode.unavailable');
  const readAccessLabel =
    readAccessScope === 'full_access'
      ? t('settings.readAccessScope.fullAccess')
      : readAccessScope === 'workspace'
        ? t('settings.readAccessScope.workspace')
        : t('settings.readAccessScope.unavailable');

  return (
    <div className="dashboard-section execution-settings-section">
      <h3 className="dashboard-section-title">{t('settings.approvalMode.title')}</h3>

      <div className="settings-control-row">
        <div className="settings-control-copy">
          <div className="settings-control-title">{t('settings.approvalMode.controlTitle')}</div>
          <div className="settings-section-status">{modeLabel}</div>
          <p className="dashboard-section-description settings-control-description">
            {t('settings.approvalMode.description')}
          </p>
        </div>
        <button
          type="button"
          role="switch"
          aria-checked={isAlwaysAllow}
          aria-label={t('settings.approvalMode.controlTitle')}
          className={[
            'settings-toggle',
            'settings-toggle--fixed',
            isAlwaysAllow ? 'settings-toggle--on' : null,
          ]
            .filter(Boolean)
            .join(' ')}
          disabled={isSavingApproval || approvalMode === null}
          onClick={() =>
            void updateApprovalMode(isAlwaysAllow ? 'prompt_each_time' : 'always_allow')
          }
        >
          <span className="settings-toggle-thumb" />
          <span className="settings-toggle-label">
            {isAlwaysAllow
              ? t('settings.approvalMode.mode.alwaysAllow')
              : t('settings.approvalMode.mode.promptEachTime')}
          </span>
        </button>
      </div>
      {approvalErrorMessage ? (
        <p className="dashboard-section-description settings-section-error">
          {approvalErrorMessage}
        </p>
      ) : null}

      <div className="settings-section-divider" />

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
              onClick={() => navigate({ search: '?section=workspace' })}
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
