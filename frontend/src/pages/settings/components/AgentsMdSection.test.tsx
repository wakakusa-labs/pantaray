import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { t as translate } from '@/i18n/translate';
import type { MessageKey } from '@/i18n/types';
import { AGENTS_MD_MAX_BYTES } from '../../../../electron/src/ipc/schemas/agentsMd';
import { AgentsMdSection } from './AgentsMdSection';

const ja = (key: MessageKey, vars?: Record<string, string | number>) => translate('ja', key, vars);

function installBridge(read: () => Promise<string>, write = vi.fn(async (_content: string) => {})) {
  const agentsMd = { read: vi.fn(read), write };
  window.electron = { agentsMd } as unknown as Window['electron'];
  return agentsMd;
}

describe('AgentsMdSection', () => {
  afterEach(() => {
    cleanup();
    delete window.electron;
  });

  it('shows the saved file and writes the edited text', async () => {
    const bridge = installBridge(async () => '結論から答える。');
    render(<AgentsMdSection t={ja} />);

    const field = await screen.findByRole('textbox', { name: ja('settings.agentsMd.title') });
    expect(field).toHaveValue('結論から答える。');
    fireEvent.change(field, { target: { value: '結論から短く答える。' } });
    fireEvent.click(screen.getByRole('button', { name: ja('settings.agentsMd.save') }));

    expect(await screen.findByText(ja('settings.agentsMd.saved'))).toBeInTheDocument();
    expect(bridge.write).toHaveBeenCalledWith('結論から短く答える。');
  });

  it('saves an emptied field, so the instructions are removed', async () => {
    const bridge = installBridge(async () => 'old');
    render(<AgentsMdSection t={ja} />);

    fireEvent.change(await screen.findByRole('textbox'), { target: { value: '' } });
    fireEvent.click(screen.getByRole('button', { name: ja('settings.agentsMd.save') }));

    expect(await screen.findByText(ja('settings.agentsMd.saved'))).toBeInTheDocument();
    expect(bridge.write).toHaveBeenCalledWith('');
  });

  it('offers no field to save over when the file could not be read', async () => {
    const bridge = installBridge(async () => {
      throw new Error('EACCES');
    });
    render(<AgentsMdSection t={ja} />);

    expect(await screen.findByRole('alert')).toHaveTextContent(ja('settings.agentsMd.loadFailed'));
    expect(screen.queryByRole('textbox')).not.toBeInTheDocument();
    expect(bridge.write).not.toHaveBeenCalled();
  });

  it('keeps the text and says so when the save fails', async () => {
    installBridge(
      async () => '',
      vi.fn(async () => {
        throw new Error('ENOSPC');
      })
    );
    render(<AgentsMdSection t={ja} />);

    const field = await screen.findByRole('textbox');
    fireEvent.change(field, { target: { value: 'draft' } });
    fireEvent.click(screen.getByRole('button', { name: ja('settings.agentsMd.save') }));

    expect(await screen.findByText(ja('settings.agentsMd.saveFailed'))).toBeInTheDocument();
    expect(field).toHaveValue('draft');
  });

  it('counts the limit in UTF-8 bytes and does not send text over it', async () => {
    const bridge = installBridge(async () => '');
    render(<AgentsMdSection t={ja} />);

    // Fewer characters than the byte limit, but three bytes each.
    fireEvent.change(await screen.findByRole('textbox'), {
      target: { value: 'あ'.repeat(AGENTS_MD_MAX_BYTES / 2) },
    });
    const save = screen.getByRole('button', { name: ja('settings.agentsMd.save') });

    expect(screen.getByText(ja('settings.agentsMd.tooLong'))).toBeInTheDocument();
    expect(save).toBeDisabled();
    fireEvent.click(save);
    expect(bridge.write).not.toHaveBeenCalled();
  });
});
