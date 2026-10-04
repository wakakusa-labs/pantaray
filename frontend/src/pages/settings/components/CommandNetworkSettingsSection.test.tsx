import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { MESSAGES } from '@/i18n/messages';
import type { MessageKey } from '@/i18n/types';
import { CommandNetworkSettingsSection } from './CommandNetworkSettingsSection';

const translate = (key: MessageKey) => MESSAGES.ja[key];
const title = 'コマンド実行時のネット接続を許可';

function installApi() {
  const api = {
    getCommandNetwork: vi.fn(async () => ({ command_network_enabled: true })),
    updateCommandNetwork: vi.fn(async (enabled: boolean) => ({ command_network_enabled: enabled })),
  };
  window.electron = { workspaceSettings: api } as unknown as Window['electron'];
  return api;
}

afterEach(() => {
  cleanup();
  delete window.electron;
});

describe('command network settings', () => {
  it('shows the agreed explanation and saves with Space and Enter', async () => {
    const api = installApi();
    const user = userEvent.setup();
    render(<CommandNetworkSettingsSection t={translate} />);
    const toggle = await screen.findByRole('switch', { name: title });
    expect(toggle).toHaveAttribute('aria-checked', 'true');
    expect(toggle).toHaveAccessibleDescription(
      'OFFにすると、GitHubなどとのコード・ファイルの送受信や、ソフトのダウンロードを止めます。会話やWeb検索には影響しません。'
    );
    await user.tab();
    expect(toggle).toHaveFocus();
    await user.keyboard(' ');
    await waitFor(() => expect(toggle).toHaveAttribute('aria-checked', 'false'));
    expect(api.updateCommandNetwork).toHaveBeenLastCalledWith(false);
    await user.keyboard('{Enter}');
    await waitFor(() => expect(toggle).toHaveAttribute('aria-checked', 'true'));
    expect(api.updateCommandNetwork).toHaveBeenLastCalledWith(true);
  });

  it('does not display an assumed value before the setting arrives', () => {
    const api = installApi();
    api.getCommandNetwork.mockReturnValue(new Promise(() => {}));
    render(<CommandNetworkSettingsSection t={translate} />);
    expect(screen.queryByRole('switch')).not.toBeInTheDocument();
    expect(screen.getByRole('status')).toHaveTextContent('読み込み中');
    expect(api.updateCommandNetwork).not.toHaveBeenCalled();
  });

  it('recovers a failed initial read only when the user reloads', async () => {
    const api = installApi();
    api.getCommandNetwork.mockRejectedValueOnce(new Error('storage unavailable'));
    api.getCommandNetwork.mockResolvedValueOnce({ command_network_enabled: false });
    const user = userEvent.setup();
    render(<CommandNetworkSettingsSection t={translate} />);
    expect(await screen.findByRole('alert')).toHaveTextContent('設定を取得できませんでした');
    expect(screen.queryByRole('switch')).not.toBeInTheDocument();
    expect(screen.getByRole('status')).toHaveTextContent('未取得');
    await user.click(screen.getByRole('button', { name: '設定を再取得' }));
    expect(await screen.findByRole('switch')).toHaveAttribute('aria-checked', 'false');
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('keeps the confirmed value and blocks repeated writes while saving', async () => {
    const api = installApi();
    let finishSave!: (value: { command_network_enabled: boolean }) => void;
    api.updateCommandNetwork.mockReturnValue(
      new Promise((resolve) => {
        finishSave = resolve;
      })
    );
    const user = userEvent.setup();
    render(<CommandNetworkSettingsSection t={translate} />);
    const toggle = await screen.findByRole('switch');
    await user.click(toggle);
    expect(toggle).toBeDisabled();
    expect(toggle).toHaveAttribute('aria-checked', 'true');
    await user.click(toggle);
    expect(api.updateCommandNetwork).toHaveBeenCalledTimes(1);
    finishSave({ command_network_enabled: false });
    await waitFor(() => expect(toggle).toBeEnabled());
    expect(toggle).toHaveAttribute('aria-checked', 'false');
  });

  it('uses readback after a lost save response instead of rolling back a committed change', async () => {
    const api = installApi();
    api.getCommandNetwork.mockResolvedValueOnce({ command_network_enabled: true });
    api.getCommandNetwork.mockResolvedValueOnce({ command_network_enabled: false });
    api.updateCommandNetwork.mockRejectedValueOnce(new Error('response timed out'));
    const user = userEvent.setup();
    render(<CommandNetworkSettingsSection t={translate} />);
    await user.click(await screen.findByRole('switch'));
    await waitFor(() =>
      expect(screen.getByRole('switch')).toHaveAttribute('aria-checked', 'false')
    );
    expect(screen.getByRole('alert')).toHaveTextContent('保存を確認できませんでした');
    expect(api.updateCommandNetwork).toHaveBeenCalledTimes(1);
  });

  it('shows unknown if both save and readback fail', async () => {
    const api = installApi();
    api.getCommandNetwork.mockResolvedValueOnce({ command_network_enabled: true });
    api.getCommandNetwork.mockRejectedValueOnce(new Error('read timed out'));
    api.updateCommandNetwork.mockRejectedValueOnce(new Error('save timed out'));
    const user = userEvent.setup();
    render(<CommandNetworkSettingsSection t={translate} />);
    await user.click(await screen.findByRole('switch'));
    expect(await screen.findByRole('alert')).toHaveTextContent(
      '保存結果と現在の設定を確認できません'
    );
    expect(screen.queryByRole('switch')).not.toBeInTheDocument();
    expect(screen.getByRole('status')).toHaveTextContent('未取得');
  });
});
