import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from 'vitest';

import { AuthContext, type AuthContextType } from '@/context/AuthContextDef';
import { UiLanguageProvider } from '@/context/UiLanguageContext';
import SettingsPage from '../../SettingsPage';
import type {
  IntegrationAccessUpdateResult,
  IntegrationAccountStatus,
  IntegrationConnectResult,
  IntegrationsStatus,
  IntegrationToolSetting,
} from '../../../../electron/src/settings/integrationSettingsFetch';
import type {
  ConnectionCommand,
  ConnectionStateResult,
} from '../../../../electron/src/ipc/schemas/aiConnection';

type IntegrationsApi = NonNullable<NonNullable<Window['electron']>['integrations']>;
type AiConnectionApi = NonNullable<NonNullable<Window['electron']>['aiConnection']>;

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
let aiConnection: { [Key in keyof AiConnectionApi]: Mock<AiConnectionApi[Key]> };
let keySaved: boolean;

function connectionState(): ConnectionStateResult {
  return {
    ok: true,
    settings: {
      preferences: { method: 'api_key', provider: 'openai', model: '' },
      hasSavedApiKey: false,
      hasSavedWebSearchKey: false,
      hasSavedIntegrationKey: keySaved,
      canStoreSecrets: true,
      chatgpt: { status: 'disconnected' },
    },
    runtime: { ok: false, error: 'runtime_unavailable' },
  };
}

beforeEach(() => {
  keySaved = true;
  api = {
    getStatus: vi.fn(async () => (keySaved ? CONFIGURED : { state: 'not_configured' })),
    setAccountAccess: vi.fn<IntegrationsApi['setAccountAccess']>(),
    listAccountTools: vi.fn(async () => TOOLS),
    setToolBlocked: vi.fn(async () => undefined),
    connect: vi.fn<IntegrationsApi['connect']>(),
    reconnect: vi.fn<IntegrationsApi['reconnect']>(),
    disconnect: vi.fn(async () => undefined),
    getConnectionState: vi.fn<IntegrationsApi['getConnectionState']>(),
  };
  aiConnection = {
    getState: vi.fn(async () => connectionState()),
    update: vi.fn(async (command: ConnectionCommand) => {
      if (command.operation === 'save_integration_key') keySaved = true;
      if (command.operation === 'remove_integration_key') keySaved = false;
      return { ok: true as const };
    }),
    onChanged: vi.fn(() => () => undefined),
  };
  Object.defineProperty(window, 'electron', {
    configurable: true,
    value: { integrations: api, aiConnection },
  });
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.restoreAllMocks();
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

describe('the Composio key', () => {
  it('without a key, shows only the key row and how to get one', async () => {
    keySaved = false;
    renderSettings();

    expect(await screen.findByLabelText('Composio API key')).toHaveAttribute('type', 'password');
    const nav = screen.getByRole('complementary', { name: 'Settings' });
    expect(within(nav).getByRole('button', { name: 'Integrations' })).toBeInTheDocument();
    const link = screen.getByRole('link', { name: 'Open Composio' });
    expect(link).toHaveAttribute('href', 'https://dashboard.composio.dev');
    expect(link).toHaveAttribute('target', '_blank');
    expect(
      screen.getByText('Create a key under Settings → Project Settings → API Keys.')
    ).toBeInTheDocument();
    expect(screen.getByText(/Write only for Session tool execution\.$/)).toBeInTheDocument();
    expect(
      screen.queryByRole('button', { name: 'Connect an external service' })
    ).not.toBeInTheDocument();
    expect(screen.queryByRole('list')).not.toBeInTheDocument();
  });

  it('saves and removes the key through main, loading the services in between', async () => {
    keySaved = false;
    renderSettings();

    fireEvent.change(await screen.findByLabelText('Composio API key'), {
      target: { value: ' ak_test ' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Composio API key: Save' }));
    expect(await screen.findByRole('button', { name: /^Gmail/ })).toBeInTheDocument();
    expect(aiConnection.update).toHaveBeenCalledWith({
      operation: 'save_integration_key',
      apiKey: 'ak_test',
    });
    expect(screen.queryByRole('link', { name: 'Open Composio' })).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Composio API key: Remove' }));
    expect(await screen.findByRole('link', { name: 'Open Composio' })).toBeInTheDocument();
    expect(aiConnection.update).toHaveBeenLastCalledWith({ operation: 'remove_integration_key' });
    expect(screen.queryByRole('button', { name: /^Gmail/ })).not.toBeInTheDocument();
  });

  it('refuses an account key while it is typed, and takes a project key', async () => {
    keySaved = false;
    renderSettings();

    const input = await screen.findByLabelText('Composio API key');
    const save = screen.getByRole('button', { name: 'Composio API key: Save' });
    fireEvent.change(input, { target: { value: ' ck_account ' } });
    expect(screen.getByRole('alert')).toHaveTextContent(
      'This is an account key. Open a project and create an API key that starts with ak_.'
    );
    expect(save).toBeDisabled();
    fireEvent.click(save);
    expect(aiConnection.update).not.toHaveBeenCalled();

    fireEvent.change(input, { target: { value: 'ak_project' } });
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    fireEvent.click(save);
    await waitFor(() =>
      expect(aiConnection.update).toHaveBeenCalledWith({
        operation: 'save_integration_key',
        apiKey: 'ak_project',
      })
    );
  });

  it('asks for a new key when the key is refused, and offers a reload only for a failed read', async () => {
    const rejected = "This key doesn't work. Replace it with a new key.";
    api.getStatus.mockResolvedValueOnce({ state: 'key_rejected' });
    renderSettings();

    expect(await screen.findByText(rejected)).toHaveAttribute('role', 'alert');
    expect(screen.getAllByRole('alert')).toHaveLength(1);
    expect(screen.queryByRole('button', { name: 'Load again' })).not.toBeInTheDocument();
    expect(screen.queryByText('Saved on this Mac')).not.toBeInTheDocument();

    // The read after the new key fails: nothing is known about the new key yet.
    api.getStatus.mockRejectedValueOnce(new Error('Local backend request failed.'));
    fireEvent.click(screen.getByRole('button', { name: 'Composio API key: Replace' }));
    fireEvent.change(screen.getByLabelText('Composio API key'), { target: { value: 'ak_new' } });
    fireEvent.click(screen.getByRole('button', { name: 'Composio API key: Save' }));
    expect(await screen.findByText(/^Couldn't load the services\./)).toHaveAttribute(
      'role',
      'alert'
    );
    expect(screen.queryByText(rejected)).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Load again' }));
    expect(await screen.findByRole('button', { name: /^Gmail/ })).toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });
});

describe('integration settings', () => {
  it('lists connected services only, and loads their tools when one is opened', async () => {
    renderSettings();

    const gmail = await screen.findByRole('button', { name: /^Gmail/ });
    expect(gmail).toHaveTextContent('2 accounts');
    expect(gmail).toHaveTextContent('Needs reconnecting');
    expect(screen.queryByRole('button', { name: /^Slack/ })).not.toBeInTheDocument();
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

  it('keeps an unknown earlier save unconfirmed when a new save is refused', async () => {
    api.setAccountAccess.mockRejectedValueOnce(new Error('Local backend request failed.'));
    renderSettings();
    await openGmail();
    api.getStatus.mockRejectedValueOnce(new Error('Local backend request failed.'));
    fireEvent.click(accessButton('home@example.com'));
    fireEvent.click(screen.getByRole('menuitemradio', { name: 'Off' }));
    await waitFor(() => expect(accessButton('home@example.com')).toHaveTextContent('Unconfirmed'));

    api.setAccountAccess.mockResolvedValueOnce({
      kind: 'not_allowed',
      reason: { source: 'connection_method', capability: 'execute', detail: null },
    });
    fireEvent.click(accessButton('home@example.com'));
    fireEvent.click(screen.getByRole('menuitemradio', { name: 'Read only' }));

    await waitFor(() =>
      expect(screen.getByText('The API does not allow running tools.')).toBeInTheDocument()
    );
    expect(accessButton('home@example.com')).toHaveTextContent('Unconfirmed');
    expect(screen.getByRole('button', { name: 'Check again' })).toBeInTheDocument();
  });

  it('keeps a save that ends while an older reload is in flight, then reads again', async () => {
    api.setAccountAccess.mockImplementation(async (update) => ({
      kind: 'saved',
      account: { ...HOME, access: update.access },
    }));
    renderSettings();
    await openGmail();
    let answerReload: (status: IntegrationsStatus) => void = () => undefined;
    api.getStatus.mockImplementationOnce(() => new Promise((resolve) => (answerReload = resolve)));

    // Disconnecting work starts a reload, which the backend answers only after the save below.
    fireEvent.click(screen.getByRole('button', { name: 'More actions for work@example.com' }));
    fireEvent.click(screen.getByRole('menuitem', { name: 'Disconnect' }));
    const dialog = screen.getByRole('dialog', { name: 'Disconnect work@example.com?' });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Disconnect' }));
    await waitFor(() => expect(api.getStatus).toHaveBeenCalledTimes(2));

    fireEvent.click(accessButton('home@example.com'));
    fireEvent.click(screen.getByRole('menuitemradio', { name: 'Off' }));
    await waitFor(() => expect(accessButton('home@example.com')).toHaveTextContent('Off'));

    const afterSave = { ...GMAIL, accounts: [{ ...HOME, access: 'off' as const }] };
    let answerReread: (status: IntegrationsStatus) => void = () => undefined;
    api.getStatus.mockImplementationOnce(() => new Promise((resolve) => (answerReread = resolve)));
    const beforeSave = { ...GMAIL, accounts: [HOME] };
    await act(async () =>
      answerReload({ state: 'configured', toolkits: [beforeSave, ...UNCONNECTED] })
    );

    expect(accessButton('home@example.com')).toHaveTextContent('Off');
    expect(screen.queryByText('work@example.com')).not.toBeInTheDocument();
    expect(api.getStatus).toHaveBeenCalledTimes(3);
    await act(async () =>
      answerReread({ state: 'configured', toolkits: [afterSave, ...UNCONNECTED] })
    );
    expect(accessButton('home@example.com')).toHaveTextContent('Off');
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

  it('ignores a stale tool list that answers after a newer list and a saved unblock', async () => {
    let answerFirst: (tools: IntegrationToolSetting[]) => void = () => undefined;
    let workLoads = 0;
    api.listAccountTools.mockImplementation(async (ref) => {
      if (ref.account === 'acct-work' && ++workLoads === 1) {
        return new Promise((resolve) => (answerFirst = resolve));
      }
      return TOOLS;
    });
    renderSettings();
    await openGmail();
    await waitFor(() => expect(workLoads).toBe(1));

    // A reload (after disconnecting home) drops work's loading row, so its list loads again.
    api.getStatus.mockResolvedValue({
      state: 'configured',
      toolkits: [{ ...GMAIL, accounts: [GMAIL.accounts[0]] }, ...UNCONNECTED],
    });
    fireEvent.click(screen.getByRole('button', { name: 'More actions for home@example.com' }));
    fireEvent.click(screen.getByRole('menuitem', { name: 'Disconnect' }));
    const dialog = screen.getByRole('dialog', { name: 'Disconnect home@example.com?' });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Disconnect' }));
    await waitFor(() => expect(workLoads).toBe(2));

    fireEvent.click(screen.getByRole('button', { name: 'Choose tool by tool' }));
    const archive = await screen.findByRole('switch', { name: 'Use Archive' });
    expect(archive).not.toBeChecked();
    fireEvent.click(archive);
    const unblock = { toolkit: 'gmail', account: 'acct-work', slug: 'ARCHIVE', blocked: false };
    expect(api.setToolBlocked).toHaveBeenCalledWith(unblock);
    await waitFor(() => expect(screen.getByRole('switch', { name: 'Use Archive' })).toBeChecked());

    await act(async () => answerFirst(TOOLS));
    expect(screen.getByRole('switch', { name: 'Use Archive' })).toBeChecked();
  });

  it('loads the tools again when a reload lands during an access save and a tool load', async () => {
    let answerFirst: (tools: IntegrationToolSetting[]) => void = () => undefined;
    let homeLoads = 0;
    api.listAccountTools.mockImplementation(async (ref) => {
      if (ref.account === 'acct-home' && ++homeLoads === 1) {
        return new Promise((resolve) => (answerFirst = resolve));
      }
      return TOOLS;
    });
    let answerSave: (result: IntegrationAccessUpdateResult) => void = () => undefined;
    api.setAccountAccess.mockImplementation(() => new Promise((resolve) => (answerSave = resolve)));
    renderSettings();
    await openGmail();
    await waitFor(() => expect(homeLoads).toBe(1));
    fireEvent.click(accessButton('home@example.com'));
    fireEvent.click(screen.getByRole('menuitemradio', { name: 'Off' }));

    // Disconnecting work reloads the status while home's save and tool load are in flight.
    api.getStatus.mockResolvedValue(withHome(HOME));
    fireEvent.click(screen.getByRole('button', { name: 'More actions for work@example.com' }));
    fireEvent.click(screen.getByRole('menuitem', { name: 'Disconnect' }));
    const dialog = screen.getByRole('dialog', { name: 'Disconnect work@example.com?' });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Disconnect' }));
    await waitFor(() => expect(api.getStatus).toHaveBeenCalledTimes(2));
    await act(async () => answerFirst(TOOLS));
    await act(async () => answerSave({ kind: 'saved', account: { ...HOME, access: 'off' } }));

    expect(homeLoads).toBe(2);
    const [homeTools] = screen.getAllByRole('button', { name: 'Choose tool by tool' }).slice(-1);
    fireEvent.click(homeTools);
    const tools = within(document.getElementById(homeTools.getAttribute('aria-controls')!)!);
    expect(await tools.findByRole('switch', { name: 'Use Fetch emails' })).toBeInTheDocument();
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

const STARTED: IntegrationConnectResult = {
  kind: 'started',
  redirect_url: 'https://connect.composio.dev/link/abc',
  expires_at: '2026-10-18T10:10:00Z',
  connection_id: 'conn-1',
};
const NOW = Date.parse('2026-10-18T10:00:00Z');
const SLACK_ACCOUNT = { ...account('team', 'read_only', false), label: 'team@example.com' };
const WITH_SLACK: IntegrationsStatus = {
  state: 'configured',
  toolkits: [GMAIL, toolkit('slack', 'Slack', [SLACK_ACCOUNT]), UNCONNECTED[1]],
};

/** The fake clock still moves on its own, so Testing Library's own timeouts keep running. */
function useFakeClock() {
  vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'Date'], shouldAdvanceTime: true });
  vi.setSystemTime(NOW);
}

async function connectSlack() {
  renderSettings();
  fireEvent.click(await screen.findByRole('button', { name: 'Connect an external service' }));
  const dialog = screen.getByRole('dialog', { name: 'Connect an external service' });
  expect(within(dialog).queryByText('Gmail')).not.toBeInTheDocument();
  fireEvent.click(within(dialog).getByRole('button', { name: 'Connect Slack' }));
}

async function poll() {
  await act(() => vi.advanceTimersByTimeAsync(2_000));
}

describe('connecting an account', () => {
  it('opens the link, polls until active, and opens the service with the new account', async () => {
    useFakeClock();
    const open = vi.spyOn(window, 'open').mockReturnValue(null);
    api.connect.mockResolvedValue(STARTED);
    api.getConnectionState.mockResolvedValueOnce('pending').mockResolvedValueOnce('active');
    await connectSlack();

    expect(api.connect).toHaveBeenCalledWith({ toolkit: 'slack' });
    await waitFor(() => expect(open).toHaveBeenCalledWith(STARTED.redirect_url, '_blank'));
    expect(screen.getByText(/^Slack: Continue in your browser…/)).toBeInTheDocument();

    await poll();
    expect(api.getConnectionState).toHaveBeenCalledWith({ connection_id: 'conn-1' });
    api.getStatus.mockResolvedValue(WITH_SLACK);
    await poll();

    expect(await screen.findByRole('heading', { name: 'Slack' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Back to Integrations' })).toHaveFocus();
    expect(accessButton('team@example.com')).toHaveTextContent('Read only');
    expect(api.getConnectionState).toHaveBeenCalledTimes(2);
  });

  it('keeps polling the same connection after failed checks, until it is active', async () => {
    useFakeClock();
    vi.spyOn(window, 'open').mockReturnValue(null);
    api.connect.mockResolvedValue(STARTED);
    api.getConnectionState
      .mockRejectedValueOnce(new Error('Local backend request failed.'))
      .mockRejectedValueOnce(new Error('Local backend request failed.'))
      .mockResolvedValueOnce('active');
    await connectSlack();
    await screen.findByText(/Continue in your browser/);

    await poll();
    expect(screen.getByText(/^Slack: Continue in your browser…/)).toBeInTheDocument();
    await poll();
    expect(screen.getByText(/^Slack: Still checking…/)).toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    api.getStatus.mockResolvedValue(WITH_SLACK);
    await poll();

    expect(await screen.findByRole('heading', { name: 'Slack' })).toBeInTheDocument();
    expect(api.getConnectionState).toHaveBeenCalledTimes(3);
    expect(api.getConnectionState).toHaveBeenLastCalledWith({ connection_id: 'conn-1' });
    expect(api.getStatus).toHaveBeenCalledTimes(2);
  });

  it('stops polling when cancelled', async () => {
    useFakeClock();
    vi.spyOn(window, 'open').mockReturnValue(null);
    api.connect.mockResolvedValue(STARTED);
    api.getConnectionState.mockResolvedValue('pending');
    await connectSlack();

    fireEvent.click(await screen.findByRole('button', { name: 'Cancel' }));
    await poll();
    await poll();
    expect(api.getConnectionState).not.toHaveBeenCalled();
    expect(screen.queryByText(/Continue in your browser/)).not.toBeInTheDocument();
  });

  it.each([
    ['the connection fails', STARTED, 'failed' as const, "Slack: Couldn't connect."],
    [
      'the link expires',
      { ...STARTED, expires_at: '2026-10-18T10:00:01Z' },
      'pending' as const,
      'Slack: The connection link expired. Try again.',
    ],
  ])('says so when %s', async (_case, started, state, message) => {
    useFakeClock();
    vi.spyOn(window, 'open').mockReturnValue(null);
    api.connect.mockResolvedValue(started);
    api.getConnectionState.mockResolvedValue(state);
    await connectSlack();
    await screen.findByText(/Continue in your browser/);

    await poll();
    expect(await screen.findByRole('alert')).toHaveTextContent(message);
    expect(api.getStatus).toHaveBeenCalledTimes(2);
  });

  it('shows why the API refuses a new connection, and opens nothing', async () => {
    const open = vi.spyOn(window, 'open').mockReturnValue(null);
    api.connect.mockResolvedValue({
      kind: 'not_allowed',
      reason: { source: 'connection_method', capability: 'connect', detail: null },
    });
    await connectSlack();

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Slack: The API does not allow new connections.'
    );
    expect(open).not.toHaveBeenCalled();
  });

  it('reconnects the chosen account and shows its pending state on that account', async () => {
    vi.spyOn(window, 'open').mockReturnValue(null);
    api.reconnect.mockResolvedValue(STARTED);
    renderSettings();
    await openGmail();

    fireEvent.click(screen.getByRole('button', { name: 'More actions for work@example.com' }));
    fireEvent.click(screen.getByRole('menuitem', { name: 'Reconnect' }));

    expect(api.reconnect).toHaveBeenCalledWith({ toolkit: 'gmail', account: 'acct-work' });
    const [work] = screen.getAllByRole('listitem');
    expect(await within(work).findByText(/^Continue in your browser…/)).toBeInTheDocument();
  });

  it('after a reconnect, shows the new account that replaced the old one', async () => {
    useFakeClock();
    vi.spyOn(window, 'open').mockReturnValue(null);
    api.reconnect.mockResolvedValue({ ...STARTED, connection_id: 'acct-work-2' });
    api.getConnectionState.mockResolvedValue('active');
    renderSettings();
    await openGmail();

    fireEvent.click(screen.getByRole('button', { name: 'More actions for work@example.com' }));
    fireEvent.click(screen.getByRole('menuitem', { name: 'Reconnect' }));
    await screen.findByText(/^Continue in your browser…/);
    // The backend moves the old account's access to the new one and removes the old one.
    const replaced = { ...GMAIL.accounts[0], account: 'acct-work-2', access: 'off' as const };
    api.getStatus.mockResolvedValue({
      state: 'configured',
      toolkits: [{ ...GMAIL, accounts: [replaced, HOME] }, ...UNCONNECTED],
    });
    await poll();

    expect(api.getConnectionState).toHaveBeenCalledWith({ connection_id: 'acct-work-2' });
    await waitFor(() => expect(accessButton('work@example.com')).toHaveTextContent('Off'));
    expect(screen.getByRole('heading', { name: 'Gmail' })).toBeInTheDocument();
    expect(screen.queryByText(/Continue in your browser/)).not.toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('disconnects only after the question is confirmed, then reloads the list', async () => {
    renderSettings();
    await openGmail();

    fireEvent.click(screen.getByRole('button', { name: 'More actions for home@example.com' }));
    fireEvent.click(screen.getByRole('menuitem', { name: 'Disconnect' }));
    let dialog = screen.getByRole('dialog', { name: 'Disconnect home@example.com?' });
    expect(dialog).toHaveAccessibleDescription(
      'Pantaray loses access to this account. Disconnecting also revokes its access at Gmail.'
    );
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }));
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(api.disconnect).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('button', { name: 'More actions for home@example.com' }));
    fireEvent.click(screen.getByRole('menuitem', { name: 'Disconnect' }));
    dialog = screen.getByRole('dialog', { name: 'Disconnect home@example.com?' });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Disconnect' }));

    expect(api.disconnect).toHaveBeenCalledWith({ toolkit: 'gmail', account: 'acct-home' });
    await waitFor(() => expect(api.getStatus).toHaveBeenCalledTimes(2));
  });
});
