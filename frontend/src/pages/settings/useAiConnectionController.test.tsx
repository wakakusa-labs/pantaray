import {
  act,
  cleanup,
  fireEvent,
  render,
  renderHook,
  screen,
  waitFor,
} from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, expect, it, vi } from 'vitest';
import type {
  ConnectionStateResult,
  ConnectionRecoveryCommand,
  ConnectionUpdateResult,
} from '../../../electron/src/ipc/schemas/aiConnection';
import { AuthContext, type AuthContextType } from '@/context/AuthContextDef';
import { useAiConnectionController } from './useAiConnectionController';
import { AiConnectionSettingsSection } from './components/AiConnectionSettingsSection';
import SettingsPage from '../SettingsPage';

vi.mock('../../../electron/src/auth/accountLoginFeature', () => ({
  PANTARAY_ACCOUNT_LOGIN_ENABLED: true,
}));

vi.mock('@/context/useI18n', () => ({
  useI18n: () => ({ t: (key: string) => key, formatDateTime: (date: Date) => date.toISOString() }),
}));
vi.mock('./components/RecordingFilterDialog', () => ({ RecordingFilterDialog: () => null }));
vi.mock('./useSettingsPageController', () => ({
  useSettingsPageController: () => ({ t: (key: string) => key, isCapturingScreenshots: null }),
}));

type Loaded = Extract<ConnectionStateResult, { ok: true }>;
function saved(): Loaded {
  return {
    ok: true,
    settings: {
      preferences: { method: 'api_key', provider: 'openai', model: 'test-model' },
      hasSavedApiKey: false,
      hasSavedWebSearchKey: false,
      canStoreSecrets: true,
      chatgpt: { status: 'disconnected' },
    },
    runtime: {
      ok: true,
      status: {
        helperInstanceId: 'helper',
        activeOwnerId: 'guest',
        configured: true,
        llmRoute: 'unconfigured',
        webSearchRoute: 'unconfigured',
        cloudSessionState: 'absent',
      },
    },
  };
}
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}
function bridge(initial = saved()) {
  const changes = new Set<() => void>();
  const authChanges = new Set<() => void>();
  const api = {
    getState: vi.fn<() => Promise<ConnectionStateResult>>().mockResolvedValue(initial),
    update: vi
      .fn<NonNullable<NonNullable<Window['electron']>['aiConnection']>['update']>()
      .mockResolvedValue({ ok: true }),
    onChanged: (callback: () => void) => {
      changes.add(callback);
      return () => {
        changes.delete(callback);
      };
    },
  };
  vi.stubGlobal('electron', {
    aiConnection: api,
    auth: {
      onStateChanged: (callback: () => void) => {
        authChanges.add(callback);
        return () => {
          authChanges.delete(callback);
        };
      },
    },
  });
  return { ...api, changes, authChanges };
}
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

it('refreshes on auth readiness and ignores the older initial response', async () => {
  const api = bridge();
  const first = deferred<ConnectionStateResult>();
  api.getState.mockReturnValueOnce(first.promise);
  const { result, unmount } = renderHook(useAiConnectionController);
  const latest = saved();
  latest.settings.preferences.model = 'latest-model';
  api.getState.mockResolvedValue(latest);
  act(() => api.authChanges.forEach((notify) => notify()));
  await waitFor(() =>
    expect(result.current.state?.settings.preferences.model).toBe('latest-model')
  );
  await act(async () => first.resolve({ ok: false, error: 'runtime_unavailable' }));
  expect(result.current.loadError).toBeNull();
  expect(result.current.state?.settings.preferences.model).toBe('latest-model');
  unmount();
  expect(api.changes.size).toBe(0);
  expect(api.authChanges.size).toBe(0);
});

it('releases a cancelled login before its old response and preserves a newer login', async () => {
  const api = bridge();
  const old = deferred<ConnectionUpdateResult>();
  const next = deferred<ConnectionUpdateResult>();
  api.update
    .mockReturnValueOnce(old.promise)
    .mockResolvedValueOnce({ ok: true })
    .mockReturnValueOnce(next.promise);
  const { result } = renderHook(useAiConnectionController);
  let oldRun!: Promise<boolean>;
  act(() => {
    oldRun = result.current.run({ operation: 'sign_in_chatgpt' });
  });
  await act(async () => result.current.cancelSignIn());
  expect(result.current.pendingOperation).toBeNull();
  expect(result.current.outcome?.result).toEqual({ ok: false, error: 'cancelled' });
  let nextRun!: Promise<boolean>;
  act(() => {
    nextRun = result.current.run({ operation: 'sign_in_chatgpt' });
  });
  await act(async () => {
    old.resolve({ ok: false, error: 'token_exchange_failed' });
    await oldRun;
  });
  expect(result.current.pendingOperation).toBe('sign_in_chatgpt');
  expect(result.current.outcome).toBeNull();
  await act(async () => {
    next.resolve({ ok: true });
    await nextRun;
  });
  expect(result.current.outcome?.result).toEqual({ ok: true });
});

it('reports cancellation failure while keeping the login cancellable', async () => {
  const initial = saved();
  initial.settings.preferences.method = 'chatgpt';
  const api = bridge(initial);
  const login = deferred<ConnectionUpdateResult>();
  api.update
    .mockReturnValueOnce(login.promise)
    .mockResolvedValueOnce({ ok: false, error: 'runtime_unavailable' });
  render(<AiConnectionSettingsSection />);
  fireEvent.click(await screen.findByText('settings.aiConnection.chatgpt.signIn'));
  fireEvent.click(await screen.findByText('settings.aiConnection.chatgpt.cancel'));
  await screen.findByText(/settings.aiConnection.error.runtime_unavailable/);
  expect(screen.getByText('settings.aiConnection.chatgpt.cancel')).toBeEnabled();
  await act(async () => login.resolve({ ok: false, error: 'authorization_timeout' }));
  await screen.findByText('settings.aiConnection.error.authorization_timeout');
});

it('does not let an older cancellation acknowledgment block cancelling a later login', async () => {
  const api = bridge();
  const first = deferred<ConnectionUpdateResult>();
  const oldCancel = deferred<ConnectionUpdateResult>();
  const second = deferred<ConnectionUpdateResult>();
  api.update
    .mockReturnValueOnce(first.promise)
    .mockReturnValueOnce(oldCancel.promise)
    .mockReturnValueOnce(second.promise)
    .mockResolvedValueOnce({ ok: true });
  const { result } = renderHook(useAiConnectionController);
  let firstRun!: Promise<boolean>;
  let cancelRun!: Promise<void>;
  act(() => {
    firstRun = result.current.run({ operation: 'sign_in_chatgpt' });
  });
  act(() => {
    cancelRun = result.current.cancelSignIn();
  });
  await act(async () => {
    first.resolve({ ok: false, error: 'cancelled' });
    await firstRun;
  });
  act(() => {
    void result.current.run({ operation: 'sign_in_chatgpt' });
  });
  await act(async () => result.current.cancelSignIn());
  expect(result.current.pendingOperation).toBeNull();
  expect(api.update.mock.calls.map(([command]) => command.operation)).toEqual([
    'sign_in_chatgpt',
    'cancel_chatgpt_sign_in',
    'sign_in_chatgpt',
    'cancel_chatgpt_sign_in',
  ]);
  await act(async () => {
    oldCancel.resolve({ ok: false, error: 'runtime_unavailable' });
    second.resolve({ ok: false, error: 'cancelled' });
    await cancelRun;
  });
  expect(result.current.outcome?.result).toEqual({ ok: false, error: 'cancelled' });
});

it('cancels its own pending login when the settings page unmounts', async () => {
  const api = bridge();
  const login = deferred<ConnectionUpdateResult>();
  api.update.mockReturnValueOnce(login.promise);
  const { result, unmount } = renderHook(useAiConnectionController);
  act(() => {
    void result.current.run({ operation: 'sign_in_chatgpt' });
  });
  unmount();
  expect(api.update.mock.calls.map(([command]) => command.operation)).toEqual([
    'sign_in_chatgpt',
    'cancel_chatgpt_sign_in',
  ]);
  await act(async () => login.resolve({ ok: false, error: 'cancelled' }));
});

it('uses refreshed provider metadata and sends the key only to the selected provider', async () => {
  const api = bridge();
  render(<AiConnectionSettingsSection />);
  const provider = await screen.findByLabelText('settings.aiConnection.providerLabel');
  const updated = saved();
  updated.settings.preferences.provider = 'anthropic';
  // 'test-model' は Anthropic の候補に無いので、切り替えとともに既定へ戻る。
  updated.settings.preferences.model = 'claude-opus-5-5';
  api.getState.mockResolvedValue(updated);
  fireEvent.change(provider, { target: { value: 'anthropic' } });
  await waitFor(() => expect(provider).toHaveValue('anthropic'));
  await waitFor(() => expect(provider).toBeEnabled());
  expect(api.update).toHaveBeenCalledWith({
    operation: 'save_preferences',
    preferences: updated.settings.preferences,
  });
  fireEvent.change(screen.getByLabelText('settings.aiConnection.key.label'), {
    target: { value: 'test-secret' },
  });
  const keyed = saved();
  keyed.settings = { ...updated.settings, hasSavedApiKey: true };
  api.getState.mockResolvedValue(keyed);
  fireEvent.click(
    screen.getByRole('button', {
      name: 'settings.aiConnection.key.label: settings.aiConnection.key.save',
    })
  );
  await screen.findByRole('button', {
    name: 'settings.aiConnection.key.label: settings.aiConnection.key.replace',
  });
  expect(api.update).toHaveBeenLastCalledWith({
    operation: 'save_api_key',
    provider: 'anthropic',
    apiKey: 'test-secret',
  });
  expect(document.body.textContent).not.toContain('test-secret');
});

it('switches to ChatGPT on a model that backend serves instead of an unusable one', async () => {
  // 持ち越した名前のままだと保存はされるのに経路が立たず、何も送信されない。
  const api = bridge();
  render(<AiConnectionSettingsSection />);
  const chatgpt = await screen.findByRole('radio', {
    name: 'settings.aiConnection.method.chatgpt.title',
  });
  fireEvent.click(chatgpt);
  await waitFor(() =>
    expect(api.update).toHaveBeenCalledWith({
      operation: 'save_preferences',
      preferences: { method: 'chatgpt', provider: 'openai', model: 'gpt-6-luna' },
    })
  );
});

it('keeps provider-bound controls locked until the newest post-save read is committed', async () => {
  const api = bridge();
  render(<AiConnectionSettingsSection />);
  const provider = await screen.findByLabelText('settings.aiConnection.providerLabel');
  const postSave = deferred<ConnectionStateResult>();
  const notification = deferred<ConnectionStateResult>();
  const newest = deferred<ConnectionStateResult>();
  api.getState
    .mockReturnValueOnce(postSave.promise)
    .mockReturnValueOnce(notification.promise)
    .mockReturnValueOnce(newest.promise);
  fireEvent.change(provider, { target: { value: 'anthropic' } });
  await waitFor(() => expect(provider).toBeDisabled());
  await act(async () => {
    api.changes.forEach((notify) => notify());
  });
  await act(async () => postSave.resolve(saved()));
  expect(provider).toBeDisabled();
  await act(async () => {
    api.authChanges.forEach((notify) => notify());
  });
  await act(async () => notification.resolve(saved()));
  expect(provider).toBeDisabled();
  const selected = saved();
  selected.settings.preferences.provider = 'anthropic';
  await act(async () => newest.resolve(selected));
  await waitFor(() => expect(provider).toBeEnabled());
  expect(provider).toHaveValue('anthropic');
  fireEvent.change(screen.getByLabelText('settings.aiConnection.key.label'), {
    target: { value: 'private-new-provider-key' },
  });
  fireEvent.click(
    screen.getByRole('button', {
      name: 'settings.aiConnection.key.label: settings.aiConnection.key.save',
    })
  );
  await waitFor(() =>
    expect(api.update).toHaveBeenLastCalledWith({
      operation: 'save_api_key',
      provider: 'anthropic',
      apiKey: 'private-new-provider-key',
    })
  );
});

it('preserves secret drafts through partial persistence, a read failure and reload', async () => {
  const api = bridge();
  render(<AiConnectionSettingsSection />);
  const input = await screen.findByLabelText('settings.aiConnection.key.label');
  fireEvent.change(input, { target: { value: 'retry-secret' } });
  api.update.mockResolvedValueOnce({ ok: false, error: 'runtime_unavailable' });
  api.getState.mockResolvedValueOnce({ ok: false, error: 'read_failed' });
  fireEvent.click(
    screen.getByRole('button', {
      name: 'settings.aiConnection.key.label: settings.aiConnection.key.save',
    })
  );
  const reload = await screen.findByText('settings.aiConnection.retry');
  expect(screen.queryByRole('radiogroup')).toBeNull();
  expect(input).toHaveValue('retry-secret');
  expect(reload).toHaveFocus();
  const persisted = saved();
  persisted.settings.hasSavedApiKey = true;
  api.getState.mockResolvedValue(persisted);
  fireEvent.click(reload);
  await waitFor(() => expect(input).toBeVisible());
  expect(screen.getByRole('region', { name: 'settings.aiConnection.title' })).toHaveFocus();
  expect(input).toHaveValue('retry-secret');
  fireEvent.click(
    screen.getByRole('button', {
      name: 'settings.aiConnection.key.label: settings.aiConnection.key.save',
    })
  );
  await screen.findByRole('button', {
    name: 'settings.aiConnection.key.label: settings.aiConnection.key.replace',
  });
});

for (const recovery of [
  { operation: 'disconnect_chatgpt' },
  { operation: 'remove_api_key', provider: 'anthropic' },
  { operation: 'remove_web_search_key' },
  {
    operation: 'save_preferences',
    preferences: { method: 'api_key', provider: 'openai', model: '' },
  },
] satisfies ConnectionRecoveryCommand[]) {
  it(`offers only ${recovery.operation} for the unreadable store and reloads after repair`, async () => {
    const api = bridge();
    api.getState.mockResolvedValue({ ok: false, error: 'invalid_data', recovery });
    render(<AiConnectionSettingsSection />);
    const repair = await screen.findByRole('button', {
      name: `settings.aiConnection.recovery.${recovery.operation}`,
    });
    expect(screen.getAllByRole('button')).toHaveLength(2);
    expect(api.update).not.toHaveBeenCalled();
    const error = recovery.operation === 'save_preferences' ? 'write_failed' : 'delete_failed';
    api.update.mockResolvedValueOnce({ ok: false, error });
    fireEvent.click(repair);
    await screen.findByText(`settings.aiConnection.error.${error}`);
    await waitFor(() => expect(repair).toBeEnabled());
    expect(screen.getByText('settings.aiConnection.retry')).toHaveFocus();
    api.getState.mockResolvedValue(saved());
    fireEvent.click(repair);
    await screen.findByRole('radiogroup', { name: 'settings.aiConnection.methodTitle' });
    expect(api.update).toHaveBeenLastCalledWith(recovery);
    expect(screen.getByRole('region', { name: 'settings.aiConnection.title' })).toHaveFocus();
  });
}

it('does not suggest deletion for unavailable encryption or expose native errors', async () => {
  const api = bridge();
  api.getState.mockResolvedValue({ ok: false, error: 'encryption_unavailable' });
  render(<AiConnectionSettingsSection />);
  await screen.findByText('settings.aiConnection.retry');
  expect(screen.getAllByRole('button')).toHaveLength(1);
  api.getState.mockRejectedValue(new Error('native secret-value'));
  fireEvent.click(screen.getByText('settings.aiConnection.retry'));
  await screen.findByText(/settings.aiConnection.error.runtime_unavailable/);
  expect(document.body.textContent).not.toContain('secret-value');
});

it('keeps cloud routing authoritative when editing saved direct settings', async () => {
  const initial = saved();
  if (initial.runtime.ok)
    Object.assign(initial.runtime.status, {
      llmRoute: 'cloud',
      webSearchRoute: 'cloud',
      cloudSessionState: 'present',
    });
  const api = bridge(initial);
  render(<AiConnectionSettingsSection />);
  const cloud = await screen.findByRole('radio', {
    name: 'settings.aiConnection.method.cloud.title',
  });
  expect(cloud).toBeChecked();
  fireEvent.click(
    screen.getByRole('radio', { name: 'settings.aiConnection.method.api_key.title' })
  );
  await screen.findByLabelText('settings.aiConnection.providerLabel');
  await waitFor(() => expect(cloud).toBeEnabled());
  api.update.mockClear();
  fireEvent.click(cloud);
  expect(cloud).toBeChecked();
  expect(api.update).not.toHaveBeenCalled();
});

function settingsPage(runtimeState: AuthContextType['runtimeState']) {
  return (
    <AuthContext.Provider value={{ runtimeState } as AuthContextType}>
      <MemoryRouter initialEntries={['/settings?section=ai_connection']}>
        <SettingsPage />
      </MemoryRouter>
    </AuthContext.Provider>
  );
}

it('mounts AI connection settings from its URL and keeps them through an owner change', async () => {
  bridge();
  const view = render(
    settingsPage({ status: 'ready', message: null, owner: { kind: 'account', id: 'user-1' } })
  );
  const methods = await screen.findByRole('radiogroup', {
    name: 'settings.aiConnection.methodTitle',
  });
  expect(screen.getByRole('button', { name: 'settings.aiConnection.title' })).toHaveAttribute(
    'aria-current',
    'page'
  );

  // The connection belongs to the installation, and leaving this section cancels the
  // browser sign-in it started. Resynchronizing the local owner is not leaving it, so
  // these controls keep their in-flight login and their drafts.
  view.rerender(settingsPage({ status: 'syncing', message: null, owner: null }));

  expect(screen.getByRole('radiogroup', { name: 'settings.aiConnection.methodTitle' })).toBe(
    methods
  );
});
