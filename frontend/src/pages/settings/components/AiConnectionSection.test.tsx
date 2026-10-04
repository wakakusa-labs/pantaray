import { useState } from 'react';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';

import { AiConnectionSection, type AiConnectionActions } from './AiConnectionSection';
import type { AiConnectionState } from '../aiConnectionModel';
import type { Translate } from '../types';

vi.mock('../../../../electron/src/auth/accountLoginFeature', () => ({
  PANTARAY_ACCOUNT_LOGIN_ENABLED: true,
}));

// The identity translator keeps assertions on message keys, not on copy.
const translate = ((key: string, vars?: Record<string, string | number>) =>
  vars ? `${key}:${Object.values(vars).join(',')}` : key) as Translate;

function runtime(
  status: Partial<Extract<AiConnectionState['runtime'], { ok: true }>['status']> = {}
): AiConnectionState['runtime'] {
  return {
    ok: true,
    status: {
      helperInstanceId: 'helper',
      activeOwnerId: 'guest',
      configured: true,
      llmRoute: 'unconfigured',
      webSearchRoute: 'unconfigured',
      cloudSessionState: 'absent',
      ...status,
    },
  };
}

const UNCONFIGURED: AiConnectionState = {
  runtime: runtime(),
  method: 'api_key',
  model: '',
  chatgpt: null,
  apiKey: { provider: 'openai', hasSavedKey: false },
  webSearchHasSavedKey: false,
  canStoreSecrets: true,
};

function renderSection(state: Partial<AiConnectionState> = {}): AiConnectionActions {
  const actions: AiConnectionActions = {
    selectMethod: vi.fn(),
    selectProvider: vi.fn(),
    saveModel: vi.fn(),
    saveApiKey: vi.fn(),
    clearApiKey: vi.fn(),
    cancelChatgptSignIn: vi.fn(),
    signInToChatgpt: vi.fn(),
    disconnectChatgpt: vi.fn(),
    saveWebSearchKey: vi.fn(),
    clearWebSearchKey: vi.fn(),
  };
  render(
    <AiConnectionSection
      pendingOperation={null}
      feedback={null}
      state={{ ...UNCONFIGURED, ...state }}
      actions={actions}
      t={translate}
    />
  );
  return actions;
}

afterEach(cleanup);

it('offers Pantaray Cloud only while a cloud session exists', () => {
  renderSection({ runtime: runtime({ cloudSessionState: 'absent' }) });
  expect(screen.getByRole('radio', { name: /method.cloud/ })).toBeDisabled();

  cleanup();
  renderSection({ runtime: runtime({ cloudSessionState: 'present' }) });
  expect(screen.getByRole('radio', { name: /method.cloud/ })).toBeEnabled();
});

it('says an expired session needs a new sign-in instead of a direct fallback', () => {
  renderSection({
    runtime: runtime({ llmRoute: 'cloud', cloudSessionState: 'expired' }),
    method: 'cloud',
  });
  expect(screen.getAllByText('settings.aiConnection.status.expiredDetail').length).toBeGreaterThan(
    0
  );
});

it('never renders a stored key, and replaces it only on request', () => {
  const secret = 'sk-secret-value';
  const actions = renderSection({
    apiKey: { ...UNCONFIGURED.apiKey, hasSavedKey: true },
  });
  expect(document.body.textContent).not.toContain(secret);
  expect(screen.queryByPlaceholderText('settings.aiConnection.key.placeholder')).toBeNull();

  fireEvent.click(screen.getByText('settings.aiConnection.key.replace'));
  const input = screen.getByPlaceholderText('settings.aiConnection.key.placeholder');
  fireEvent.change(input, { target: { value: secret } });
  fireEvent.click(screen.getAllByText('settings.aiConnection.key.save')[0]);

  expect(actions.saveApiKey).toHaveBeenCalledWith(secret);
});

it('shows a connected ChatGPT account without asking for a new sign-in', () => {
  const actions = renderSection({
    method: 'chatgpt',
    chatgpt: { status: 'connected' },
  });
  expect(screen.getByText('settings.aiConnection.chatgpt.connected')).toBeInTheDocument();
  expect(screen.queryByText('settings.aiConnection.chatgpt.reauthenticate')).toBeNull();

  fireEvent.click(screen.getByText('settings.aiConnection.chatgpt.disconnect'));
  expect(actions.disconnectChatgpt).toHaveBeenCalled();
});

it('offers only selectable Codex models for ChatGPT and keeps API key custom input', () => {
  const actions = renderSection({ method: 'chatgpt', model: 'gpt-6-luna' });
  const picker = screen.getByRole('combobox', { name: 'settings.aiConnection.modelLabel' });
  expect([...picker.querySelectorAll('option')].map((option) => option.value)).toEqual([
    '',
    'gpt-6-luna',
    'gpt-6-astra',
    'gpt-6.1-sol',
    'gpt-6-sol',
    'gpt-5.6-sol',
    'gpt-5.6-terra',
    'gpt-5.6-luna',
    'gpt-5.5',
  ]);
  fireEvent.change(picker, { target: { value: 'gpt-6-sol' } });
  fireEvent.click(screen.getByText('settings.aiConnection.model.save'));
  expect(actions.saveModel).toHaveBeenCalledWith('gpt-6-sol');

  cleanup();
  renderSection({ method: 'api_key' });
  expect(screen.getByLabelText('settings.aiConnection.modelLabel')).toHaveAttribute('type', 'text');
  expect(
    [...document.querySelectorAll('#ai-model-candidates option')].map((option) =>
      option.getAttribute('value')
    )
  ).toEqual(['gpt-5.6', 'gpt-5.6-sol', 'gpt-5.6-terra', 'gpt-6-luna']);
});

it('requires a new choice when the stored ChatGPT model is no longer offered', () => {
  const actions = renderSection({ method: 'chatgpt', model: 'retired-model' });
  const picker = screen.getByRole('combobox', { name: 'settings.aiConnection.modelLabel' });
  expect(picker).toHaveValue('');
  expect(screen.getByText('settings.aiConnection.model.save')).toBeDisabled();
  fireEvent.change(picker, { target: { value: 'gpt-6-luna' } });
  fireEvent.click(screen.getByText('settings.aiConnection.model.save'));
  expect(actions.saveModel).toHaveBeenCalledWith('gpt-6-luna');
});

it('keeps the chosen Codex model available for retry after apply fails', async () => {
  const actions = renderSection({ method: 'chatgpt', model: 'gpt-6-luna' });
  const save = actions.saveModel as ReturnType<typeof vi.fn>;
  save.mockRejectedValueOnce(new Error('runtime unavailable'));
  const picker = screen.getByRole('combobox', { name: 'settings.aiConnection.modelLabel' });
  fireEvent.change(picker, { target: { value: 'gpt-6-sol' } });
  fireEvent.click(screen.getByText('settings.aiConnection.model.save'));
  expect(await screen.findByText('settings.aiConnection.model.saveFailed')).toBeInTheDocument();
  expect(picker).toHaveValue('gpt-6-sol');
  expect(picker).toHaveFocus();
  expect(screen.getByText('settings.aiConnection.model.save')).toBeEnabled();
});

it('explains that a saved search key applies while signed out of the cloud', () => {
  renderSection({ runtime: runtime({ webSearchRoute: 'cloud' }) });
  expect(screen.getByText('settings.aiConnection.webSearch.cloudDescription')).toBeInTheDocument();
});

it('does not carry a typed key across a provider change', () => {
  const actions: AiConnectionActions = {
    selectMethod: vi.fn(),
    selectProvider: vi.fn(),
    saveModel: vi.fn(),
    saveApiKey: vi.fn(),
    clearApiKey: vi.fn(),
    cancelChatgptSignIn: vi.fn(),
    signInToChatgpt: vi.fn(),
    disconnectChatgpt: vi.fn(),
    saveWebSearchKey: vi.fn(),
    clearWebSearchKey: vi.fn(),
  };
  const { rerender } = render(
    <AiConnectionSection
      pendingOperation={null}
      feedback={null}
      state={UNCONFIGURED}
      actions={actions}
      t={translate}
    />
  );
  fireEvent.change(screen.getByLabelText('settings.aiConnection.key.label'), {
    target: { value: 'sk-openai' },
  });

  rerender(
    <AiConnectionSection
      pendingOperation={null}
      feedback={null}
      state={{ ...UNCONFIGURED, apiKey: { ...UNCONFIGURED.apiKey, provider: 'anthropic' } }}
      actions={actions}
      t={translate}
    />
  );

  expect(screen.getByLabelText('settings.aiConnection.key.label')).toHaveValue('');
});

it('refuses to take a key when this system cannot store secrets', () => {
  renderSection({ canStoreSecrets: false });
  expect(screen.getByLabelText('settings.aiConnection.key.label')).toBeDisabled();
  expect(screen.getAllByText('settings.aiConnection.key.save')[0].closest('button')).toBeDisabled();
});

it('keeps the typed key and reports the failure when saving rejects', async () => {
  const actions = renderSection({});
  (actions.saveApiKey as ReturnType<typeof vi.fn>).mockRejectedValue(new Error('no storage'));
  const input = screen.getByLabelText('settings.aiConnection.key.label');
  fireEvent.change(input, { target: { value: 'sk-value' } });
  fireEvent.click(screen.getAllByText('settings.aiConnection.key.save')[0]);

  expect(await screen.findByText('settings.aiConnection.key.saveFailed')).toBeInTheDocument();
  expect(input).toHaveValue('sk-value');
});

it('cannot start a ChatGPT sign-in when this system cannot store secrets', () => {
  renderSection({ method: 'chatgpt', canStoreSecrets: false });
  expect(screen.getByText('settings.aiConnection.chatgpt.signIn').closest('button')).toBeDisabled();
});

it('locks the key field while a save is in flight', async () => {
  let release: () => void = () => {};
  const actions = renderSection({});
  (actions.saveApiKey as ReturnType<typeof vi.fn>).mockReturnValue(
    new Promise<void>((resolve) => {
      release = resolve;
    })
  );
  const input = screen.getByLabelText('settings.aiConnection.key.label');
  fireEvent.change(input, { target: { value: 'sk-value' } });
  fireEvent.click(screen.getAllByText('settings.aiConnection.key.save')[0]);

  expect(input).toBeDisabled();
  release();
  await waitFor(() => expect(input).not.toBeDisabled());
});

it('says web search is unusable while the session is expired', () => {
  renderSection({
    runtime: runtime({ webSearchRoute: 'cloud', cloudSessionState: 'expired', llmRoute: 'cloud' }),
  });
  expect(screen.getAllByText('settings.aiConnection.status.expiredDetail')).toHaveLength(2);
});

it('states why Pantaray Cloud is unavailable instead of hiding it in a tooltip', () => {
  renderSection({ runtime: runtime({ cloudSessionState: 'absent' }) });
  const reason = screen.getByText('settings.aiConnection.method.cloud.requiresLogin');
  expect(reason).toBeInTheDocument();
  expect(screen.getByRole('radiogroup')).toHaveAttribute('aria-describedby', 'ai-method-login');
});

it('lets a replacement be abandoned without losing the saved key', () => {
  renderSection({ apiKey: { ...UNCONFIGURED.apiKey, hasSavedKey: true } });
  fireEvent.click(screen.getByText('settings.aiConnection.key.replace'));
  fireEvent.change(screen.getByLabelText('settings.aiConnection.key.label'), {
    target: { value: 'sk-typed' },
  });

  fireEvent.click(screen.getByText('settings.aiConnection.key.cancel'));

  expect(screen.getByText('settings.aiConnection.key.saved')).toBeInTheDocument();
  expect(screen.getByText('settings.aiConnection.key.remove')).toBeInTheDocument();
  expect(screen.getByText('settings.aiConnection.key.replace')).toHaveFocus();
});

it('says a ChatGPT session needs a new sign-in instead of showing it as healthy', () => {
  renderSection({
    runtime: runtime({ llmRoute: 'direct' }),
    method: 'chatgpt',
    model: 'gpt-5.6',
    chatgpt: { status: 'reauthentication_required' },
  });
  expect(
    screen.getAllByText('settings.aiConnection.chatgpt.reauthenticationRequired').length
  ).toBeGreaterThan(0);
});

it('names the credential each secret action belongs to', () => {
  renderSection({
    apiKey: { ...UNCONFIGURED.apiKey, hasSavedKey: true },
    webSearchHasSavedKey: true,
  });

  expect(
    screen.getByRole('button', {
      name: 'settings.aiConnection.key.label: settings.aiConnection.key.remove',
    })
  ).toBeInTheDocument();
  expect(
    screen.getByRole('button', {
      name: 'settings.aiConnection.webSearch.keyLabel: settings.aiConnection.key.remove',
    })
  ).toBeInTheDocument();
});

for (const credential of ['api', 'search'] as const) {
  it(`preserves the ${credential} key on failed removal, then focuses its input after retry`, async () => {
    let rejectRemoval: (error: Error) => void = () => {};
    const remove = vi.fn().mockImplementationOnce(
      () =>
        new Promise<void>((_resolve, reject) => {
          rejectRemoval = reject;
        })
    );
    function Fixture() {
      const [saved, setSaved] = useState(true);
      const clear = async () => {
        await remove();
        setSaved(false);
      };
      const actions: AiConnectionActions = {
        selectMethod: vi.fn(),
        selectProvider: vi.fn(),
        saveModel: vi.fn(),
        saveApiKey: vi.fn(),
        clearApiKey: clear,
        cancelChatgptSignIn: vi.fn(),
        signInToChatgpt: vi.fn(),
        disconnectChatgpt: vi.fn(),
        saveWebSearchKey: vi.fn(),
        clearWebSearchKey: clear,
      };
      return (
        <AiConnectionSection
          pendingOperation={null}
          feedback={null}
          state={{
            ...UNCONFIGURED,
            apiKey: { provider: 'openai', hasSavedKey: credential === 'api' && saved },
            webSearchHasSavedKey: credential === 'search' && saved,
          }}
          actions={actions}
          t={translate}
        />
      );
    }
    render(<Fixture />);
    const label =
      credential === 'api'
        ? 'settings.aiConnection.key.label'
        : 'settings.aiConnection.webSearch.keyLabel';
    const removeButton = screen.getByRole('button', {
      name: `${label}: settings.aiConnection.key.remove`,
    });
    removeButton.focus();
    fireEvent.click(removeButton);
    expect(removeButton).toBeDisabled();
    expect(screen.getByText('settings.aiConnection.key.removing')).toBeInTheDocument();
    rejectRemoval(new Error('private path must not be rendered'));
    expect(await screen.findByText('settings.aiConnection.key.removeFailed')).toBeInTheDocument();
    expect(removeButton).toHaveFocus();
    expect(document.body.textContent).not.toContain('private path');
    fireEvent.click(removeButton);
    expect(await screen.findByText('settings.aiConnection.key.removed')).toBeInTheDocument();
    expect(screen.getByLabelText(label)).toHaveFocus();
  });
}

it('focuses Replace after a newly saved key hides the input', async () => {
  let finishSave: () => void = () => {};
  function Fixture() {
    const [saved, setSaved] = useState(false);
    const actions: AiConnectionActions = {
      selectMethod: vi.fn(),
      selectProvider: vi.fn(),
      saveModel: vi.fn(),
      saveApiKey: () => {
        setSaved(true);
        return new Promise<void>((resolve) => {
          finishSave = resolve;
        });
      },
      clearApiKey: vi.fn(),
      cancelChatgptSignIn: vi.fn(),
      signInToChatgpt: vi.fn(),
      disconnectChatgpt: vi.fn(),
      saveWebSearchKey: vi.fn(),
      clearWebSearchKey: vi.fn(),
    };
    return (
      <AiConnectionSection
        pendingOperation={null}
        feedback={null}
        state={{ ...UNCONFIGURED, apiKey: { provider: 'openai', hasSavedKey: saved } }}
        actions={actions}
        t={translate}
      />
    );
  }
  render(<Fixture />);
  fireEvent.change(screen.getByLabelText('settings.aiConnection.key.label'), {
    target: { value: 'secret' },
  });
  fireEvent.click(
    screen.getByRole('button', {
      name: 'settings.aiConnection.key.label: settings.aiConnection.key.save',
    })
  );
  expect(screen.getByLabelText('settings.aiConnection.key.label')).toBeDisabled();
  finishSave();
  expect(await screen.findByText('settings.aiConnection.key.saveSuccess')).toBeInTheDocument();
  expect(screen.getByText('settings.aiConnection.key.replace')).toHaveFocus();
  expect(screen.queryByDisplayValue('secret')).toBeNull();
});

it('edits a model as a draft and preserves it when applying fails', async () => {
  const actions = renderSection({ model: 'saved-model', runtime: runtime({ llmRoute: 'direct' }) });
  const save = actions.saveModel as ReturnType<typeof vi.fn>;
  save.mockRejectedValueOnce(new Error('provider details'));
  const input = screen.getByLabelText('settings.aiConnection.modelLabel');
  fireEvent.change(input, { target: { value: ' new-model ' } });
  expect(save).not.toHaveBeenCalled();
  expect(screen.getByText(/saved-model/)).toBeInTheDocument();
  fireEvent.click(screen.getByText('settings.aiConnection.model.save'));
  expect(save).toHaveBeenCalledWith('new-model');
  expect(await screen.findByText('settings.aiConnection.model.saveFailed')).toBeInTheDocument();
  expect(input).toHaveValue(' new-model ');
  expect(input).toHaveFocus();
});

it('shows unavailable runtime status without hiding credential editing', () => {
  renderSection({ runtime: { ok: false, error: 'runtime_unavailable' } });
  expect(screen.getAllByText('settings.aiConnection.route.unavailable')).toHaveLength(2);
  expect(screen.queryByText('settings.aiConnection.route.unconfigured')).toBeNull();
  expect(screen.getByLabelText('settings.aiConnection.key.label')).toBeEnabled();
  expect(screen.getByRole('radio', { name: /method.cloud/ })).toBeDisabled();
});

it('locks settings while authorizing but keeps cancellation available', () => {
  const actions = renderSection();
  cleanup();
  const { rerender } = render(
    <AiConnectionSection
      state={{ ...UNCONFIGURED, method: 'chatgpt' }}
      actions={actions}
      t={translate}
      pendingOperation="sign_in_chatgpt"
      feedback={null}
    />
  );
  expect(screen.getByText('settings.aiConnection.chatgpt.waiting')).toBeInTheDocument();
  expect(screen.getByLabelText('settings.aiConnection.modelLabel')).toBeDisabled();
  expect(screen.getByText('settings.aiConnection.chatgpt.signIn')).toBeDisabled();
  const cancel = screen.getByText('settings.aiConnection.chatgpt.cancel');
  expect(cancel).toBeEnabled();
  expect(cancel).toHaveFocus();
  fireEvent.click(cancel);
  expect(actions.cancelChatgptSignIn).toHaveBeenCalled();
  rerender(
    <AiConnectionSection
      state={{ ...UNCONFIGURED, method: 'chatgpt' }}
      actions={actions}
      t={translate}
      pendingOperation="cancel_chatgpt_sign_in"
      feedback={null}
    />
  );
  expect(screen.getByText('settings.aiConnection.chatgpt.signIn')).toBeDisabled();
  rerender(
    <AiConnectionSection
      state={{ ...UNCONFIGURED, method: 'chatgpt' }}
      actions={actions}
      t={translate}
      pendingOperation={null}
      feedback={{ message: 'Authorization timed out', isError: true }}
    />
  );
  expect(screen.getByText('Authorization timed out')).toBeInTheDocument();
  expect(screen.getByText('settings.aiConnection.chatgpt.signIn')).toBeEnabled();
  expect(screen.getByText('settings.aiConnection.chatgpt.signIn')).toHaveFocus();
});

it('can reapply the same model after persistence succeeds but runtime synchronization fails', async () => {
  const baseActions = renderSection();
  cleanup();
  const appliedModels: string[] = [];
  function Fixture() {
    const [model, setModel] = useState('old-model');
    const saveModel = async (next: string) => {
      setModel(next);
      appliedModels.push(next);
      if (appliedModels.length === 1) throw new Error('runtime unavailable after persistence');
    };
    return (
      <AiConnectionSection
        state={{ ...UNCONFIGURED, model }}
        actions={{ ...baseActions, saveModel }}
        t={translate}
        pendingOperation={null}
        feedback={null}
      />
    );
  }
  render(<Fixture />);
  const input = screen.getByLabelText('settings.aiConnection.modelLabel');
  fireEvent.change(input, { target: { value: 'new-model' } });
  fireEvent.click(screen.getByText('settings.aiConnection.model.save'));
  expect(await screen.findByText('settings.aiConnection.model.saveFailed')).toBeInTheDocument();
  expect(input).toHaveValue('new-model');
  fireEvent.change(input, { target: { value: 'new-model ' } });
  const saveButton = screen.getByText('settings.aiConnection.model.save');
  expect(saveButton).toBeEnabled();
  fireEvent.click(saveButton);
  await waitFor(() => expect(appliedModels).toEqual(['new-model', 'new-model']));
  await waitFor(() => expect(saveButton).toBeDisabled());
  expect(screen.queryByText('settings.aiConnection.model.saveFailed')).toBeNull();
});

for (const credential of ['api', 'search'] as const) {
  it(`retains the ${credential} key draft when persistence succeeds but applying fails`, async () => {
    const actions = renderSection();
    cleanup();
    function Fixture() {
      const [saved, setSaved] = useState(false);
      const save = async () => {
        setSaved(true);
        throw new Error('runtime apply failed');
      };
      const clear = async () => {
        setSaved(false);
        throw new Error('runtime apply failed');
      };
      return (
        <AiConnectionSection
          pendingOperation={null}
          feedback={null}
          t={translate}
          state={{
            ...UNCONFIGURED,
            apiKey: { provider: 'openai', hasSavedKey: credential === 'api' && saved },
            webSearchHasSavedKey: credential === 'search' && saved,
          }}
          actions={{
            ...actions,
            saveApiKey: save,
            saveWebSearchKey: save,
            clearApiKey: clear,
            clearWebSearchKey: clear,
          }}
        />
      );
    }
    render(<Fixture />);
    const label =
      credential === 'api'
        ? 'settings.aiConnection.key.label'
        : 'settings.aiConnection.webSearch.keyLabel';
    const input = screen.getByLabelText(label);
    fireEvent.change(input, { target: { value: 'key-to-retry' } });
    fireEvent.click(
      screen.getByRole('button', { name: `${label}: settings.aiConnection.key.save` })
    );
    expect(await screen.findByText('settings.aiConnection.key.saveFailed')).toBeInTheDocument();
    expect(input).toHaveValue('key-to-retry');
    expect(input).toHaveFocus();
    fireEvent.click(
      screen.getByRole('button', { name: `${label}: settings.aiConnection.key.cancel` })
    );
    fireEvent.click(
      screen.getByRole('button', { name: `${label}: settings.aiConnection.key.remove` })
    );
    expect(await screen.findByText('settings.aiConnection.key.removeFailed')).toBeInTheDocument();
    expect(screen.getByLabelText(label)).toHaveFocus();
  });
}

it('restores account focus only after a disconnect operation finishes', () => {
  const actions = renderSection();
  cleanup();
  const state: AiConnectionState = { ...UNCONFIGURED, method: 'chatgpt' };
  const { rerender } = render(
    <AiConnectionSection
      state={state}
      actions={actions}
      t={translate}
      pendingOperation="disconnect_chatgpt"
      feedback={null}
    />
  );
  expect(screen.getByText('settings.aiConnection.chatgpt.signIn')).toBeDisabled();
  rerender(
    <AiConnectionSection
      state={state}
      actions={actions}
      t={translate}
      pendingOperation={null}
      feedback={null}
    />
  );
  expect(screen.getByText('settings.aiConnection.chatgpt.signIn')).toHaveFocus();
});
