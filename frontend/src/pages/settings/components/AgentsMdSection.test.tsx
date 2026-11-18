import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { t as translate } from '@/i18n/translate';
import type { MessageKey } from '@/i18n/types';
import {
  AGENTS_MD_MAX_BYTES,
  type AgentsMdReadResult,
  type AgentsMdWriteRequest,
  type AgentsMdWriteResult,
} from '../../../../electron/src/ipc/schemas/agentsMd';
import { AgentsMdSection } from './AgentsMdSection';

const ja = (key: MessageKey, vars?: Record<string, string | number>) => translate('ja', key, vars);

const REV_A = 'a'.repeat(64);
const REV_B = 'b'.repeat(64);
const REV_C = 'c'.repeat(64);

const loaded = (content: string, revision: string | null): AgentsMdReadResult => ({
  kind: 'loaded',
  content,
  revision,
});

function installBridge(
  read: () => Promise<AgentsMdReadResult>,
  write: (request: AgentsMdWriteRequest) => Promise<AgentsMdWriteResult> = async () => ({
    kind: 'saved',
    revision: REV_B,
  })
) {
  const agentsMd = { read: vi.fn(read), write: vi.fn(write) };
  window.electron = { agentsMd } as unknown as Window['electron'];
  return agentsMd;
}

const saveButton = () => screen.getByRole('button', { name: ja('settings.agentsMd.save') });

describe('AgentsMdSection', () => {
  afterEach(() => {
    cleanup();
    delete window.electron;
  });

  it('saves edits against the revision it read, then against the one it saved', async () => {
    const bridge = installBridge(
      async () => loaded('結論から答える。', REV_A),
      vi
        .fn<(request: AgentsMdWriteRequest) => Promise<AgentsMdWriteResult>>()
        .mockResolvedValueOnce({ kind: 'saved', revision: REV_B })
        .mockResolvedValueOnce({ kind: 'saved', revision: REV_C })
    );
    render(<AgentsMdSection t={ja} />);

    const field = await screen.findByRole('textbox', { name: ja('settings.agentsMd.title') });
    expect(field).toHaveValue('結論から答える。');
    fireEvent.change(field, { target: { value: '結論から短く答える。' } });
    fireEvent.click(saveButton());
    expect(await screen.findByText(ja('settings.agentsMd.saved'))).toBeInTheDocument();
    fireEvent.change(field, { target: { value: '' } });
    fireEvent.click(saveButton());
    expect(await screen.findByText(ja('settings.agentsMd.saved'))).toBeInTheDocument();

    expect(bridge.write.mock.calls.map(([request]) => request)).toEqual([
      { content: '結論から短く答える。', expectedRevision: REV_A },
      { content: '', expectedRevision: REV_B },
    ]);
  });

  it('keeps the edits when the file changed elsewhere, and loads the latest on request', async () => {
    const bridge = installBridge(
      vi
        .fn<() => Promise<AgentsMdReadResult>>()
        .mockResolvedValueOnce(loaded('old', null))
        .mockResolvedValueOnce(loaded('from an editor', REV_C)),
      async () => ({ kind: 'conflict' })
    );
    render(<AgentsMdSection t={ja} />);

    const field = await screen.findByRole('textbox');
    fireEvent.change(field, { target: { value: 'mine' } });
    fireEvent.click(saveButton());

    expect(await screen.findByText(ja('settings.agentsMd.conflict'))).toBeInTheDocument();
    expect(field).toHaveValue('mine');
    fireEvent.click(screen.getByRole('button', { name: ja('settings.agentsMd.reload') }));

    const reloaded = await screen.findByDisplayValue('from an editor');
    await waitFor(() => expect(reloaded).toHaveFocus());
    expect(screen.queryByText(ja('settings.agentsMd.conflict'))).not.toBeInTheDocument();
    expect(bridge.read).toHaveBeenCalledTimes(2);
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

  it('offers no field when the file is over the limit, so a cut copy is never saved', async () => {
    const bridge = installBridge(async () => ({ kind: 'too_large' }));
    render(<AgentsMdSection t={ja} />);

    expect(await screen.findByRole('alert')).toHaveTextContent(
      ja('settings.agentsMd.fileTooLarge')
    );
    expect(screen.queryByRole('textbox')).not.toBeInTheDocument();
    expect(bridge.write).not.toHaveBeenCalled();
  });

  it('keeps the text and says so when the save fails', async () => {
    installBridge(
      async () => loaded('', null),
      async () => {
        throw new Error('ENOSPC');
      }
    );
    render(<AgentsMdSection t={ja} />);

    const field = await screen.findByRole('textbox');
    fireEvent.change(field, { target: { value: 'draft' } });
    fireEvent.click(saveButton());

    expect(await screen.findByText(ja('settings.agentsMd.saveFailed'))).toBeInTheDocument();
    expect(field).toHaveValue('draft');
  });

  it('counts the limit in UTF-8 bytes and does not send text over it', async () => {
    const bridge = installBridge(async () => loaded('', null));
    render(<AgentsMdSection t={ja} />);

    // Fewer characters than the byte limit, but three bytes each.
    fireEvent.change(await screen.findByRole('textbox'), {
      target: { value: 'あ'.repeat(AGENTS_MD_MAX_BYTES / 2) },
    });

    expect(screen.getByText(ja('settings.agentsMd.tooLong'))).toBeInTheDocument();
    expect(saveButton()).toBeDisabled();
    fireEvent.click(saveButton());
    expect(bridge.write).not.toHaveBeenCalled();
  });
});
