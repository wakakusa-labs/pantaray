import { expect, it, vi } from 'vitest';

import type { ActionMessageRequest } from '../../../electron/src/actions/actionContracts';
import {
  initialComposerState,
  type ComposerState,
} from '../agent-overlay/useOverlayComposerController';
import { createTaskComposerDrafts, keptComposerState } from './taskComposerDrafts';

const document = {
  kind: 'file',
  attachmentId: '00000000-0000-4000-8000-000000000001',
  name: 'quote.pdf',
  byteSize: 9,
} as const;
const request = { message: { message_id: 'm-1' } } as unknown as ActionMessageRequest;
const state = (patch: Partial<ComposerState>): ComposerState => ({
  ...initialComposerState('A1'),
  ...patch,
});

it('keeps nothing for an empty composer, and a send still in flight as failed for its retry', () => {
  expect(keptComposerState(state({}))).toBeNull();
  expect(
    keptComposerState(
      state({
        submission: { request, state: 'submitting' },
        submissionStartFence: { messageId: 'm-1', sequence: 0, processId: null },
        attachmentsInFlight: 1,
      })
    )
  ).toMatchObject({
    submission: { request, state: 'failed' },
    failureKind: 'transport',
    submissionStartFence: null,
    attachmentsInFlight: 0,
  });
});

it('discards the unsent documents it holds when the owner’s pages close, and later keeps too', () => {
  const discardAttachment = vi.fn(async () => undefined);
  const drafts = createTaskComposerDrafts({
    discardAttachment,
    attachFile: vi.fn(),
    attachImage: vi.fn(),
  });
  drafts.keep('action:A1', state({ draft: 'totals', attachments: [document] }));
  // A document that went out with a send is the backend's.
  drafts.keep(
    'action:A2',
    state({
      attachments: [{ ...document, attachmentId: 'sent' }],
      submission: { request, state: 'failed' },
    })
  );
  drafts.keep('action:A3', state({ draft: 'gone' }));
  drafts.keep('action:A3', state({}));
  expect(drafts.read('action:A1')?.draft).toBe('totals');
  expect(drafts.read('action:A3')).toBeUndefined();
  expect(discardAttachment).not.toHaveBeenCalled();

  drafts.close();
  expect(discardAttachment).toHaveBeenCalledExactlyOnceWith({
    attachmentId: document.attachmentId,
  });
  expect(drafts.read('action:A1')).toBeUndefined();

  // A pane that leaves after its owner's pages closed has nowhere to come back to.
  drafts.keep('suggestion:S1', state({ attachments: [{ ...document, attachmentId: 'late' }] }));
  expect(discardAttachment).toHaveBeenLastCalledWith({ attachmentId: 'late' });
  expect(drafts.read('suggestion:S1')).toBeUndefined();
});
