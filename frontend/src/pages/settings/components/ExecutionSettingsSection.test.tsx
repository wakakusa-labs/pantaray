import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { MemoryRouter, useLocation } from 'react-router-dom';

import { ExecutionSettingsSection } from './ExecutionSettingsSection';
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

function renderExecutionSettingsSection() {
  return render(
    <MemoryRouter initialEntries={['/settings?section=execution']}>
      <ExecutionSettingsSection t={translate} />
      <LocationProbe />
    </MemoryRouter>
  );
}

describe('ExecutionSettingsSection', () => {
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
    clearWorkspaceSettingsCache();
    delete window.electron;
  });

  it('updates read access scope through the execution settings toggle', async () => {
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
      workspaceSettings: workspaceSettingsApi,
    } as unknown as Window['electron'];

    renderExecutionSettingsSection();

    const toggle = screen.getByRole('switch', { name: 'settings.readAccessScope.title' });
    await waitFor(() => expect(toggle).toBeEnabled());
    expect(screen.getByText('settings.readAccessScope.workspaceBoundary')).toBeInTheDocument();
    fireEvent.click(toggle);

    await waitFor(() => {
      expect(workspaceSettingsApi.updateReadAccessScope).toHaveBeenCalledWith('full_access');
    });
    expect(getCachedWorkspaceSettings('user-1')).toBeNull();

    // The workspace it reads from is the projects in the History page's sidebar.
    fireEvent.click(screen.getByRole('button', { name: 'settings.readAccessScope.openWorkspace' }));
    expect(screen.getByRole('status', { name: 'location' })).toHaveTextContent('/history');
  });
});
