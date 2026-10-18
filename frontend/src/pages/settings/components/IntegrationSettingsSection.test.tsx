import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from 'vitest';

import { AuthContext, type AuthContextType } from '@/context/AuthContextDef';
import { UiLanguageProvider } from '@/context/UiLanguageContext';
import SettingsPage from '../../SettingsPage';
import type {
  IntegrationAccessUpdateResult,
  IntegrationAccountStatus,
  IntegrationsStatus,
  IntegrationToolSetting,
} from '../../../../electron/src/settings/integrationSettingsFetch';

type IntegrationsApi = NonNullable<NonNullable<Window['electron']>['integrations']>;

vi.mock('@/components/AiConnectionNotice', () => ({ AiConnectionNotice: () => null }));
vi.mock('@/components/LocalOwnerBoundary', () => ({ LocalOwnerBoundary: () => null }));

const GRANT_DENIAL = { source: 'account_grant', capability: 'tool', detail: 'gmail.send' } as const;

function account(
  id: string,
  access: IntegrationAccountStatus['access'],
  writable: boolean
): IntegrationAccountStatus {
  return {
    account: `acct-${id}`,
    label: `${id}@example.com`,
    health: writable ? 'reauthorization_required' : 'ok',
    access,
    options: [
      { value: 'off', allowed: true, reason: null },
      { value: 'read_only', allowed: true, reason: null },
      writable
        ? { value: 'read_write', allowed: true, reason: null }
        : { value: 'read_write', allowed: false, reason: GRANT_DENIAL },
    ],
  };
}

const HOME = account('home', 'read_write', true);
const CONFIGURED: IntegrationsStatus = {
  state: 'configured',
  toolkits: [
    {
      toolkit: 'gmail',
      display_name: 'Gmail',
      accounts: [account('work', 'read_only', false), HOME],
    },
  ],
};

const TOOLS: IntegrationToolSetting[] = [
  { slug: 'FETCH', name: 'Fetch emails', tool_class: 'read', blocked: false, api_denial: null },
  { slug: 'SEND', name: 'Send', tool_class: 'write', blocked: false, api_denial: GRANT_DENIAL },
];

const auth = {
  runtimeState: { status: 'ready', message: null, owner: { kind: 'guest', id: 'guest' } },
} as AuthContextType;

let api: { [Key in keyof IntegrationsApi]: Mock<IntegrationsApi[Key]> };

beforeEach(() => {
  api = {
    getStatus: vi.fn(async () => CONFIGURED),
    setAccountAccess: vi.fn<IntegrationsApi['setAccountAccess']>(),
    listAccountTools: vi.fn(async () => TOOLS),
    setToolBlocked: vi.fn(async () => undefined),
  };
  Object.defineProperty(window, 'electron', { configurable: true, value: { integrations: api } });
});

afterEach(() => {
  cleanup();
  delete window.electron;
});

function renderSettings() {
  return render(
    <AuthContext.Provider value={auth}>
      <UiLanguageProvider initialLanguage="en">
        <MemoryRouter initialEntries={['/settings?section=integrations']}>
          <SettingsPage />
        </MemoryRouter>
      </UiLanguageProvider>
    </AuthContext.Provider>
  );
}

async function accessGroup(label: string) {
  return await screen.findByRole('radiogroup', { name: `What Pantaray can do with ${label}` });
}

describe('integration settings', () => {
  it.each([
    ['not configured', async () => ({ state: 'not_configured' }) as IntegrationsStatus],
    ['unreachable', async () => Promise.reject(new Error('Not Found'))],
  ])('adds nothing to the page when integrations are %s', async (_case, getStatus) => {
    api.getStatus.mockImplementation(getStatus);
    renderSettings();

    await waitFor(() => expect(api.getStatus).toHaveBeenCalled());
    const nav = screen.getByRole('complementary', { name: 'Settings' });
    expect(within(nav).queryByRole('button', { name: 'Integrations' })).not.toBeInTheDocument();
    expect(screen.queryByRole('radiogroup')).not.toBeInTheDocument();
  });

  it('saves each account of one service on its own', async () => {
    api.setAccountAccess.mockImplementation(async (update) => ({
      kind: 'saved',
      account: { ...HOME, access: update.access },
    }));
    renderSettings();

    const home = await accessGroup('home@example.com');
    const work = await accessGroup('work@example.com');
    expect(within(work).getByRole('radio', { name: 'Read only' })).toBeChecked();
    expect(within(home).getByRole('radio', { name: 'Read and write' })).toBeChecked();
    expect(screen.getByText('Needs to be reconnected')).toBeInTheDocument();

    fireEvent.click(within(home).getByRole('radio', { name: 'Off' }));

    const homeOff = { toolkit: 'gmail', account: 'acct-home', access: 'off' };
    expect(api.setAccountAccess).toHaveBeenCalledWith(homeOff);
    await waitFor(() => expect(within(home).getByRole('radio', { name: 'Off' })).toBeChecked());
    expect(within(work).getByRole('radio', { name: 'Read only' })).toBeChecked();
  });

  it('keeps a refused option unselected and says why when it is pressed', async () => {
    renderSettings();
    const work = await accessGroup('work@example.com');
    const readWrite = within(work).getByRole('radio', { name: 'Read and write' });
    expect(readWrite).toHaveAttribute('aria-disabled', 'true');

    fireEvent.click(readWrite);

    expect(api.setAccountAccess).not.toHaveBeenCalled();
    expect(within(work).getByRole('radio', { name: 'Read only' })).toBeChecked();
    expect(readWrite).toHaveAccessibleDescription(
      'Your Gmail connection does not allow gmail.send.'
    );
  });

  it.each([
    [
      'the backend refuses the option',
      async () =>
        ({
          kind: 'not_allowed',
          reason: { source: 'connection_method', capability: 'execute', detail: null },
        }) as IntegrationAccessUpdateResult,
      'The API does not allow running tools.',
    ],
    [
      'the save fails',
      async () => Promise.reject(new Error('Local backend request failed.')),
      "Couldn't save the change. The previous setting is kept.",
    ],
  ])('puts the previous choice back when %s', async (_case, setAccountAccess, message) => {
    api.setAccountAccess.mockImplementation(setAccountAccess);
    renderSettings();
    const home = await accessGroup('home@example.com');

    fireEvent.click(within(home).getByRole('radio', { name: 'Read only' }));

    expect(await screen.findByRole('alert')).toHaveTextContent(message);
    expect(within(home).getByRole('radio', { name: 'Read and write' })).toBeChecked();
  });

  it('loads the tools only when the details are opened, and blocks the chosen one', async () => {
    renderSettings();
    await accessGroup('work@example.com');
    expect(api.listAccountTools).not.toHaveBeenCalled();

    const [workDetails] = screen.getAllByText('Choose tool by tool');
    const details = workDetails.closest('details')!;
    details.open = true;
    fireEvent(details, new Event('toggle'));

    expect(api.listAccountTools).toHaveBeenCalledWith({ toolkit: 'gmail', account: 'acct-work' });
    const list = within(details);
    const send = await list.findByRole('switch', { name: 'Use Send' });
    fireEvent.click(send);
    expect(api.setToolBlocked).not.toHaveBeenCalled();
    expect(send).toHaveAccessibleDescription('Your Gmail connection does not allow gmail.send.');

    fireEvent.click(list.getByRole('switch', { name: 'Use Fetch emails' }));
    const block = { toolkit: 'gmail', account: 'acct-work', slug: 'FETCH', blocked: true };
    expect(api.setToolBlocked).toHaveBeenCalledWith(block);
    await waitFor(() =>
      expect(list.getByRole('switch', { name: 'Use Fetch emails' })).not.toBeChecked()
    );
  });
});
