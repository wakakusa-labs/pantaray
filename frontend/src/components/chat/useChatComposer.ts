import { useEffect, useRef, useState } from 'react';

import {
  ChatMessageRequestSchema,
  type ChatItem,
  type ChatMessageRejectedField,
  type ChatMessageRequest,
} from '../../../electron/src/chat/chatContracts';
import {
  addStagedAttachments,
  attachmentKey,
  attachmentPayload,
  canAttachMore,
  discardDocuments,
  stageFiles,
  type AttachmentFailure,
  type ComposerAttachment,
} from '@/components/agent-overlay/attachmentStaging';

import type { ChatMessageItem } from './chatTimeline';

/** Why the last send did not go out: a field the backend or the request schema refused. */
export type ChatSendProblem = ChatMessageRejectedField | 'transport';

type ChatComposerState = {
  draft: string;
  quote: ChatMessageItem | null;
  attachments: readonly ComposerAttachment[];
  /** Attach round trips still running; sending waits for them. */
  attachmentsInFlight: number;
  attachmentFailure: AttachmentFailure | null;
  /**
   * The message on its way. A failed one keeps its request, so a retry resends the same
   * `message_id` and the backend appends it once even if the first response was lost.
   */
  pending: { request: ChatMessageRequest; state: 'sending' | 'failed' } | null;
  problem: ChatSendProblem | null;
};

const EMPTY: ChatComposerState = {
  draft: '',
  quote: null,
  attachments: [],
  attachmentsInFlight: 0,
  attachmentFailure: null,
  pending: null,
  problem: null,
};

export type ChatComposerControl = ReturnType<typeof useChatComposer>;

/**
 * The chat's composer: one message with an optional quote and attachments, sent once. It lives
 * as long as the History page, so switching to the list keeps the draft and a failed request.
 */
export function useChatComposer({ onSent }: { onSent: (item: ChatItem) => void }) {
  const [state, setState] = useState<ChatComposerState>(EMPTY);
  const actions = window.electron?.actions;
  // Documents staged here and never sent are discarded when the page closes, including those
  // whose write finishes after it closed.
  const unsentRef = useRef<readonly ComposerAttachment[]>([]);
  const closedRef = useRef(false);
  useEffect(() => {
    unsentRef.current = state.pending === null ? state.attachments : [];
  });
  useEffect(() => {
    closedRef.current = false;
    return () => {
      closedRef.current = true;
      discardDocuments(actions, unsentRef.current);
    };
  }, [actions]);

  const deliver = (request: ChatMessageRequest) => {
    const chat = window.electron?.chat;
    if (!chat) return;
    setState((current) => ({ ...current, pending: { request, state: 'sending' }, problem: null }));
    const settle = (next: (current: ChatComposerState) => ChatComposerState) =>
      setState((current) =>
        current.pending?.request.message_id === request.message_id ? next(current) : current
      );
    chat.sendMessage(request).then(
      (result) => {
        if (result.kind === 'sent') {
          onSent(result.item);
          settle(() => EMPTY);
          return;
        }
        // Refused before anything was appended: the user fixes the named field and sends anew.
        settle((current) => ({ ...current, pending: null, problem: result.field }));
      },
      () =>
        settle((current) => ({
          ...current,
          pending: { request, state: 'failed' },
          problem: 'transport',
        }))
    );
  };

  const send = () => {
    if (state.pending !== null || state.attachmentsInFlight > 0) return;
    const parsed = ChatMessageRequestSchema.safeParse({
      message_id: crypto.randomUUID(),
      text: state.draft,
      quote_item_id: state.quote?.item_id ?? null,
      ...attachmentPayload(state.attachments),
    });
    if (!parsed.success) {
      setState((current) => ({ ...current, problem: 'text' }));
      return;
    }
    deliver(parsed.data);
  };

  const retry = () => {
    if (state.pending?.state === 'failed') deliver(state.pending.request);
  };

  const attachFiles = async (picked: readonly File[]) => {
    if (!actions || state.pending !== null || picked.length === 0) return;
    setState((current) => ({ ...current, attachmentsInFlight: current.attachmentsInFlight + 1 }));
    const { accepted, failure } = await stageFiles(actions, picked, state.attachments);
    if (closedRef.current) {
      discardDocuments(actions, accepted);
      return;
    }
    setState((current) => {
      const added = addStagedAttachments(actions, current.attachments, accepted, failure);
      return {
        ...current,
        attachments: added.attachments,
        attachmentsInFlight: current.attachmentsInFlight - 1,
        attachmentFailure: added.failure,
      };
    });
  };

  const removeAttachment = (removed: ComposerAttachment) => {
    discardDocuments(actions, [removed]);
    setState((current) => ({
      ...current,
      attachments: current.attachments.filter(
        (attachment) => attachmentKey(attachment) !== attachmentKey(removed)
      ),
      attachmentFailure: null,
      problem: current.problem === 'images' || current.problem === 'files' ? null : current.problem,
    }));
  };

  return {
    state,
    canAttach: canAttachMore(state.attachments) && state.pending === null,
    canSend: state.pending === null && state.attachmentsInFlight === 0 && state.draft.trim() !== '',
    setDraft: (draft: string) =>
      setState((current) => ({
        ...current,
        draft,
        problem: current.problem === 'text' || current.problem === 'body' ? null : current.problem,
      })),
    setQuote: (quote: ChatMessageItem | null) =>
      setState((current) => ({
        ...current,
        quote,
        problem: current.problem === 'quote_item_id' ? null : current.problem,
      })),
    send,
    retry,
    attachFiles,
    removeAttachment,
  };
}
