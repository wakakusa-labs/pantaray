import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from 'vitest';

import { AuthContext, type AuthContextType } from '@/context/AuthContextDef';
import { UiLanguageProvider } from '@/context/UiLanguageContext';
import { t as translate } from '@/i18n/translate';
import SettingsPage from '../../SettingsPage';
import type { Translate } from '../types';
import { IntegrationSettingsSection } from './IntegrationSettingsSection';
import type {
  IntegrationAccessUpdateResult,
  IntegrationAccountStatus,
  IntegrationsStatus,
  IntegrationToolkitStatus,
  IntegrationToolSetting,
} from '../../../../electron/src/settings/integrationSettingsFetch';

type IntegrationsApi = NonNullable<NonNullable<Window['electron']>['integrations']>;

vi.mock('@/components/AiConnectionNotice', () => ({ AiConnectionNotice: () => null }));
vi.mock('@/components/LocalOwnerBoundary', () => ({ LocalOwnerBoundary: () => null }));

const GRANT_DENIAL = { source: 'account_grant', capability: 'tool', detail: 'gmail.send' } as const;
const GRANT_TEXT = 'Your Gmail connection does not allow gmail.send.';

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

function toolkit(slug: string, name: string, accounts: IntegrationAccountStatus[]) {
  return { toolkit: slug, display_name: name, logo_url: null, accounts };
}

const HOME = account('home', 'read_write', true);
const GMAIL = toolkit('gmail', 'Gmail', [account('work', 'read_only', false), HOME]);
const UNCONNECTED = [toolkit('slack', 'Slack', []), toolkit('github', 'GitHub', [])];
const CONFIGURED: IntegrationsStatus = { state: 'configured', toolkits: [GMAIL, ...UNCONNECTED] };

function withHome(home: IntegrationAccountStatus): IntegrationsStatus {
  const gmail = { ...GMAIL, accounts: [GMAIL.accounts[0], home] };
  return { state: 'configured', toolkits: [gmail, ...UNCONNECTED] };
}

const TOOLS: IntegrationToolSetting[] = [
  { slug: 'FETCH', name: 'Fetch emails', tool_class: 'read', blocked: false, api_denial: null },
  { slug: 'ARCHIVE', name: 'Archive', tool_class: 'read', blocked: true, api_denial: null },
  { slug: 'SEND', name: 'Send', tool_class: 'write', blocked: false, api_denial: GRANT_DENIAL },
  { slug: 'DRAFT', name: 'Draft', tool_class: 'write', blocked: false, api_denial: null },
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

async function openGmail() {
  fireEvent.click(await screen.findByRole('button', { name: /^Gmail/ }));
}

function accessButton(label: string) {
  return screen.getByRole('button', { name: new RegExp(`^What Pantaray can do with ${label}:`) });
}

describe('integration settings', () => {
  it.each([
    ['not configured', async () => ({ state: 'not_configured' }) as IntegrationsStatus],
    ['unreachable', async () => Promise.reject(new Error('Not Found'))],
    [
      'configured with nothing connected',
      async () => ({ state: 'configured', toolkits: UNCONNECTED }) as IntegrationsStatus,
    ],
  ])('adds nothing to the page when integrations are %s', async (_case, getStatus) => {
    api.getStatus.mockImplementation(getStatus);
    renderSettings();

    await waitFor(() => expect(api.getStatus).toHaveBeenCalled());
    const nav = screen.getByRole('complementary', { name: 'Settings' });
    expect(within(nav).queryByRole('button', { name: 'Integrations' })).not.toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: 'Integrations' })).not.toBeInTheDocument();
  });

  it('lists connected services only, and loads their tools when one is opened', async () => {
    renderSettings();

    const gmail = await screen.findByRole('button', { name: /^Gmail/ });
    expect(gmail).toHaveTextContent('2 accounts');
    expect(gmail).toHaveTextContent('Needs reconnecting');
    expect(screen.queryByRole('button', { name: /^Slack/ })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '+ Add a service' })).not.toBeInTheDocument();
    expect(api.listAccountTools).not.toHaveBeenCalled();

    fireEvent.click(gmail);

    expect(screen.getByRole('button', { name: 'Back to Integrations' })).toHaveFocus();
    expect(api.listAccountTools).toHaveBeenCalledWith({ toolkit: 'gmail', account: 'acct-work' });
    expect(api.listAccountTools).toHaveBeenCalledWith({ toolkit: 'gmail', account: 'acct-home' });
    expect(await screen.findAllByText(/1 turned off/)).toHaveLength(2);

    fireEvent.click(screen.getByRole('button', { name: 'Back to Integrations' }));
    expect(screen.getByRole('button', { name: /^Gmail/ })).toHaveFocus();
  });

  it('saves each account of one service on its own', async () => {
    api.setAccountAccess.mockImplementation(async (update) => ({
      kind: 'saved',
      account: { ...HOME, access: update.access },
    }));
    renderSettings();
    await openGmail();

    fireEvent.click(accessButton('home@example.com'));
    fireEvent.click(screen.getByRole('menuitemradio', { name: 'Off' }));

    const homeOff = { toolkit: 'gmail', account: 'acct-home', access: 'off' };
    expect(api.setAccountAccess).toHaveBeenCalledWith(homeOff);
    await waitFor(() => expect(accessButton('home@example.com')).toHaveTextContent('Off'));
    expect(accessButton('work@example.com')).toHaveTextContent('Read only');
  });

  it('shows why an option is not allowed and never saves it', async () => {
    renderSettings();
    await openGmail();

    fireEvent.click(accessButton('work@example.com'));
    const readWrite = screen.getByRole('menuitemradio', { name: 'Read and write' });
    expect(readWrite).toHaveAttribute('aria-disabled', 'true');
    expect(readWrite).toHaveAccessibleDescription(new RegExp(GRANT_TEXT.replace('.', '\\.')));
    fireEvent.click(readWrite);

    expect(api.setAccountAccess).not.toHaveBeenCalled();
    expect(screen.getByRole('menuitemradio', { name: 'Read only' })).toHaveAttribute(
      'aria-checked',
      'true'
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
      'the save fails and the backend still has the old value',
      async () => Promise.reject(new Error('Local backend request failed.')),
      "Couldn't save the change. The previous setting is kept.",
    ],
  ])('puts the previous choice back when %s', async (_case, setAccountAccess, message) => {
    api.setAccountAccess.mockImplementation(setAccountAccess);
    renderSettings();
    await openGmail();

    fireEvent.click(accessButton('home@example.com'));
    fireEvent.click(screen.getByRole('menuitemradio', { name: 'Read only' }));

    expect(await screen.findByRole('alert')).toHaveTextContent(message);
    expect(accessButton('home@example.com')).toHaveTextContent('Read and write');
  });

  it('shows the stored value when the save went through but its answer was lost', async () => {
    api.setAccountAccess.mockRejectedValue(new Error('Local backend request failed.'));
    renderSettings();
    await openGmail();
    api.getStatus.mockResolvedValue(withHome({ ...HOME, access: 'read_only' }));

    fireEvent.click(accessButton('home@example.com'));
    fireEvent.click(screen.getByRole('menuitemradio', { name: 'Read only' }));

    await waitFor(() => expect(api.getStatus).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(accessButton('home@example.com')).toHaveTextContent('Read only'));
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('claims no value when neither the save nor the re-read answers, and can check again', async () => {
    api.setAccountAccess.mockRejectedValue(new Error('Local backend request failed.'));
    renderSettings();
    await openGmail();
    api.getStatus.mockRejectedValueOnce(new Error('Local backend request failed.'));

    fireEvent.click(accessButton('home@example.com'));
    fireEvent.click(screen.getByRole('menuitemradio', { name: 'Off' }));

    expect(await screen.findByRole('alert')).toHaveTextContent(
      "Couldn't confirm whether the change was saved."
    );
    expect(accessButton('home@example.com')).toHaveTextContent('Unconfirmed');
    fireEvent.click(accessButton('home@example.com'));
    expect(screen.queryByRole('menuitemradio', { checked: true })).not.toBeInTheDocument();

    api.getStatus.mockResolvedValue(withHome({ ...HOME, access: 'off' }));
    fireEvent.click(screen.getByRole('button', { name: 'Check again' }));
    await waitFor(() => expect(accessButton('home@example.com')).toHaveTextContent('Off'));
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('takes no second choice while a save is pending, and says it is saving', async () => {
    let answer: (result: IntegrationAccessUpdateResult) => void = () => undefined;
    api.setAccountAccess.mockImplementation(() => new Promise((resolve) => (answer = resolve)));
    renderSettings();
    await openGmail();

    fireEvent.click(accessButton('home@example.com'));
    fireEvent.click(screen.getByRole('menuitemradio', { name: 'Off' }));
    const button = accessButton('home@example.com');
    expect(button).toHaveAttribute('aria-disabled', 'true');
    expect(button).toHaveAttribute('aria-busy', 'true');
    expect(screen.getByText(/Saving…/)).toBeInTheDocument();
    fireEvent.click(button);
    expect(screen.queryByRole('menu')).not.toBeInTheDocument();

    answer({ kind: 'saved', account: { ...HOME, access: 'off' } });
    await waitFor(() => expect(accessButton('home@example.com')).not.toHaveAttribute('aria-busy'));
    expect(api.setAccountAccess).toHaveBeenCalledTimes(1);
  });

  it('blocks one tool, and keeps writes inactive while the account is read only', async () => {
    renderSettings();
    await openGmail();
    const [workTools] = screen.getAllByRole('button', { name: 'Choose tool by tool' });
    fireEvent.click(workTools);
    const tools = within(document.getElementById(workTools.getAttribute('aria-controls')!)!);

    const send = await tools.findByRole('switch', { name: 'Use Send' });
    expect(send).toHaveAccessibleDescription(GRANT_TEXT);
    expect(tools.getByText('Not used while the account is read only')).toBeInTheDocument();
    expect(tools.getByRole('switch', { name: 'Use Draft' })).toBeDisabled();

    fireEvent.click(tools.getByRole('switch', { name: 'Use Fetch emails' }));
    const block = { toolkit: 'gmail', account: 'acct-work', slug: 'FETCH', blocked: true };
    expect(api.setToolBlocked).toHaveBeenCalledWith(block);
    await waitFor(() =>
      expect(tools.getByRole('switch', { name: 'Use Fetch emails' })).not.toBeChecked()
    );
  });

  it('holds the other tools while one block saves, and reads the result back if its answer is lost', async () => {
    let fail: (error: Error) => void = () => undefined;
    api.setToolBlocked.mockImplementation(() => new Promise((_resolve, reject) => (fail = reject)));
    renderSettings();
    await openGmail();
    const [workTools] = screen.getAllByRole('button', { name: 'Choose tool by tool' });
    fireEvent.click(workTools);
    const tools = within(document.getElementById(workTools.getAttribute('aria-controls')!)!);

    fireEvent.click(await tools.findByRole('switch', { name: 'Use Fetch emails' }));
    const archive = tools.getByRole('switch', { name: 'Use Archive' });
    expect(archive).toHaveAttribute('aria-disabled', 'true');
    expect(archive).toHaveAttribute('aria-busy', 'true');
    expect(tools.getByRole('status')).toHaveTextContent('Saving…');
    fireEvent.click(archive);
    expect(api.setToolBlocked).toHaveBeenCalledTimes(1);

    const stored = TOOLS.map((tool) => (tool.slug === 'FETCH' ? { ...tool, blocked: true } : tool));
    api.listAccountTools.mockResolvedValue(stored);
    fail(new Error('Local backend request failed.'));
    await waitFor(() => expect(archive).not.toHaveAttribute('aria-busy'));
    expect(tools.getByRole('switch', { name: 'Use Fetch emails' })).not.toBeChecked();
    expect(tools.queryByRole('alert')).not.toBeInTheDocument();
  });
});

describe('adding a service', () => {
  const t = ((key, vars) => translate('en', key, vars)) as Translate;
  const actions = {
    setAccess: vi.fn(),
    recheckAccess: vi.fn(),
    loadTools: vi.fn(),
    setToolBlocked: vi.fn(),
  };
  const toolkits: IntegrationToolkitStatus[] = [GMAIL, ...UNCONNECTED];

  it('lists the unconnected services, filters them, and connects the chosen one', () => {
    const onConnect = vi.fn();
    render(
      <IntegrationSettingsSection
        t={t}
        toolkits={toolkits}
        rows={{}}
        actions={actions}
        onConnect={onConnect}
      />
    );
    const open = screen.getByRole('button', { name: '+ Add a service' });
    fireEvent.click(open);

    const dialog = screen.getByRole('dialog', { name: 'Add a service' });
    expect(within(dialog).queryByText('Gmail')).not.toBeInTheDocument();
    fireEvent.change(within(dialog).getByRole('searchbox', { name: 'Search services' }), {
      target: { value: 'git' },
    });
    expect(within(dialog).queryByText('Slack')).not.toBeInTheDocument();
    fireEvent.click(within(dialog).getByRole('button', { name: 'Connect GitHub' }));

    expect(onConnect).toHaveBeenCalledWith('github');
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(open).toHaveFocus();
  });
});
