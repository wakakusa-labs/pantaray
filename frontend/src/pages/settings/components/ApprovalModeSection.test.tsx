import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { MemoryRouter, useLocation } from 'react-router-dom';

import { ApprovalModeSection } from './ApprovalModeSection';
import type { MessageKey } from '@/i18n/types';
import {
  clearWorkspaceSettingsCache,
  getCachedWorkspaceSettings,
  setCachedWorkspaceSettings,
} from './workspaceSettingsCache';

const translate = ((key: string) => key) as (
  key: MessageKey,
  vars?: Record<string, string | number>
) => string;

const workspaceSettings = {
  read_access_scope: 'workspace' as const,
  organizations: [],
  projects: [],
  folders: [],
};

function LocationProbe() {
  const location = useLocation();
  return <output aria-label="location">{`${location.pathname}${location.search}`}</output>;
}

function renderApprovalModeSection() {
  return render(
    <MemoryRouter initialEntries={['/settings?section=execution']}>
      <ApprovalModeSection t={translate} />
      <LocationProbe />
    </MemoryRouter>
  );
}

describe('ApprovalModeSection', () => {
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
    clearWorkspaceSettingsCache();
    delete window.electron;
  });

  it('updates approval preference independently from read access scope', async () => {
    const approval = {
      getWorkspaceEditCommandPreference: vi.fn(async () => ({
        scope_type: 'global' as const,
        scope_ref: null,
        approval_mode: 'prompt_each_time' as const,
        applies_to: ['workspace_edit_and_command'] as ['workspace_edit_and_command'],
      })),
      setWorkspaceEditCommandPreference: vi.fn(async () => ({
        scope_type: 'global' as const,
        scope_ref: null,
        approval_mode: 'always_allow' as const,
        applies_to: ['workspace_edit_and_command'] as ['workspace_edit_and_command'],
      })),
    };
    const workspaceSettingsApi = {
      getCommandNetwork: vi.fn(async () => ({ command_network_enabled: true })),
      updateCommandNetwork: vi.fn(),
      getReadAccessScope: vi.fn(async () => ({
        read_access_scope: workspaceSettings.read_access_scope,
      })),
      updateReadAccessScope: vi.fn(),
    };
    window.electron = {
      approval,
      workspaceSettings: workspaceSettingsApi,
    } as unknown as Window['electron'];

    renderApprovalModeSection();

    const toggle = screen.getByRole('switch', { name: 'settings.approvalMode.controlTitle' });
    await waitFor(() => expect(toggle).toBeEnabled());
    fireEvent.click(toggle);

    await waitFor(() => {
      expect(approval.setWorkspaceEditCommandPreference).toHaveBeenCalledWith('always_allow');
    });
    expect(workspaceSettingsApi.updateReadAccessScope).not.toHaveBeenCalled();
  });

  it('updates read access scope through the execution settings toggle', async () => {
    const approval = {
      getWorkspaceEditCommandPreference: vi.fn(async () => ({
        scope_type: 'global' as const,
        scope_ref: null,
        approval_mode: 'prompt_each_time' as const,
        applies_to: ['workspace_edit_and_command'] as ['workspace_edit_and_command'],
      })),
      setWorkspaceEditCommandPreference: vi.fn(),
    };
    const workspaceSettingsApi = {
      getCommandNetwork: vi.fn(async () => ({ command_network_enabled: true })),
      updateCommandNetwork: vi.fn(),
      getReadAccessScope: vi.fn(async () => ({
        read_access_scope: workspaceSettings.read_access_scope,
      })),
      updateReadAccessScope: vi.fn(async () => ({ read_access_scope: 'full_access' as const })),
    };
    setCachedWorkspaceSettings('user-1', workspaceSettings);
    window.electron = {
      approval,
      workspaceSettings: workspaceSettingsApi,
    } as unknown as Window['electron'];

    renderApprovalModeSection();

    const toggle = screen.getByRole('switch', { name: 'settings.readAccessScope.title' });
    await waitFor(() => expect(toggle).toBeEnabled());
    expect(screen.getByText('settings.readAccessScope.workspaceBoundary')).toBeInTheDocument();
    fireEvent.click(toggle);

    await waitFor(() => {
      expect(workspaceSettingsApi.updateReadAccessScope).toHaveBeenCalledWith('full_access');
    });
    expect(getCachedWorkspaceSettings('user-1')).toBeNull();
    expect(approval.setWorkspaceEditCommandPreference).not.toHaveBeenCalled();

    // The workspace it reads from is managed in Settings' own Workspace section.
    fireEvent.click(screen.getByRole('button', { name: 'settings.readAccessScope.openWorkspace' }));
    expect(screen.getByRole('status', { name: 'location' })).toHaveTextContent(
      '/settings?section=workspace'
    );
  });
});
