import { useEffect, useRef, useState } from 'react';
import {
  ActionMessageRequestSchema,
  codePointSpanInTrimmedText,
  type ActionConversationPage,
  type ActionMessageRequest,
  type ActionProjectRef,
} from '../../../electron/src/actions/actionContracts';
import type {
  ActionConversationPageChain,
  OptimisticSubmission,
} from '../../../electron/src/actions/actionConversationModel';
import { ACTION_CONVERSATION_PAGE_LIMIT } from './conversationPaging';
import { isActionSupplementWithinLimit, normalizeActionSupplement } from '@/types/websocket';
import type { ActionApprovalMode } from './useActionApprovalMode';
import type { ComposerMention } from './composerMentions';
import { useSharedTaskDraft } from './useSharedTaskDraft';

import {
  addStagedAttachments,
  attachmentKey,
  attachmentPayload,
  canAttachMore,
  discardDocuments,
  stageFiles,
  type AttachmentFailure,
  type ComposerAttachment,
} from './attachmentStaging';

type ComposerActions = NonNullable<NonNullable<typeof window.electron>['actions']>;

export type ComposerState = {
  draft: string;
  /** Workspace projects named in `draft`, in text order. */
  mentions: readonly ComposerMention[];
  /** Attachments in the order the user added them. */
  attachments: readonly ComposerAttachment[];
  /** Attach round trips still running. Sending is blocked while any is outstanding. */
  attachmentsInFlight: number;
  attachmentFailure: AttachmentFailure | null;
  submission: OptimisticSubmission | null;
  submissionStartFence: {
    messageId: string;
    sequence: number;
    processId: string | null;
  } | null;
  initialActionId: string | null;
  validationFailed: boolean;
  failureKind: 'transport' | 'expected_process_conflict' | 'action_conflict' | null;
  refreshState: 'idle' | 'loading' | 'failed';
  /**
   * 「再開」の往復。押している間は二重に開かないよう閉じ、失敗は入力欄の中で告げる。
   * 往復に失敗した再試行は同じ key で送る（応答だけが失われた場合、別の key では走り出した
   * run と衝突するだけで最初の応答には辿り着けない）。成功後も、正典の会話がまだ「再開
   * できる」と言っている間は閉じたままにし、同じ停止に二度目の再開を送らない。
   */
  resume: { messageId: string; state: 'requesting' | 'awaiting_refresh' | 'failed' } | null;
};

export function initialComposerState(initialActionId: string | null): ComposerState {
  return {
    draft: '',
    mentions: [],
    attachments: [],
    attachmentsInFlight: 0,
    attachmentFailure: null,
    submission: null,
    submissionStartFence: null,
    initialActionId,
    validationFailed: false,
    failureKind: null,
    refreshState: 'idle',
    resume: null,
  };
}

function pageContainsMessage(
  page: ActionConversationPageChain[number],
  messageId: string
): boolean {
  return (
    page.unadopted_messages.some((entry) => entry.message_id === messageId) ||
    page.runs.some((run) =>
      run.entries.some((entry) => entry.step_kind === 'user' && entry.message_id === messageId)
    )
  );
}

export function reconcileCanonicalSubmission(
  current: ComposerState,
  page: ActionConversationPageChain[number]
): ComposerState {
  const messageId = current.submission?.request.message.message_id;
  // 再開の往復が通ったあと、正典の会話が「再開できる」と言わなくなった時点で解錠する。
  const resume =
    current.resume?.state === 'awaiting_refresh' && page.action.resumable === false
      ? null
      : current.resume;
  const settled = resume === current.resume ? current : { ...current, resume };
  return messageId && pageContainsMessage(page, messageId)
    ? {
        ...settled,
        draft: '',
        mentions: [],
        attachments: [],
        submission: null,
        failureKind: null,
        refreshState: 'idle',
      }
    : settled;
}

export function useOverlayComposerController({
  actions,
  initialActionId,
  suggestionId,
  suggestionAccepted,
  language,
  onRefreshedPage,
  fallbackActionId = null,
}: {
  actions: ComposerActions | undefined;
  initialActionId: string | null;
  suggestionId: string | null;
  suggestionAccepted: boolean;
  language: 'en' | 'ja';
  onRefreshedPage: (page: ActionConversationPage) => void;
  /** The Action a suggestion's window shows once its approval started one. */
  fallbackActionId?: string | null;
}) {
  const [composer, setComposer] = useState<ComposerState>(() =>
    initialComposerState(initialActionId)
  );
  // These fences belong to the composer lifetime, not to live conversation updates.
  const composerGenerationRef = useRef(0);
  const submissionRefreshScopeRef = useRef(0);
  const restoreComposerFocusRef = useRef(false);
  const [scope, setScope] = useState({ suggestionId, suggestionAccepted, initialActionId });
  if (
    scope.suggestionId !== suggestionId ||
    scope.suggestionAccepted !== suggestionAccepted ||
    scope.initialActionId !== initialActionId
  ) {
    setScope({ suggestionId, suggestionAccepted, initialActionId });
    setComposer(initialComposerState(initialActionId));
  }
  useEffect(() => {
    // Pending image writes must not cross into a replacement composer.
    composerGenerationRef.current += 1;
  }, [suggestionId, suggestionAccepted, initialActionId]);
  // The conversation this composer writes into, and so the task whose shared draft it shows.
  const submittedActionId =
    composer.submission?.request.target.kind === 'existing'
      ? composer.submission.request.target.action_id
      : null;
  const conversationActionId = submittedActionId ?? composer.initialActionId ?? fallbackActionId;
  useSharedTaskDraft(
    actions,
    conversationActionId !== null
      ? `action:${conversationActionId}`
      : suggestionId !== null
        ? `suggestion:${suggestionId}`
        : null,
    composer,
    setComposer
  );
  const sendRequest = (request: ActionMessageRequest) => {
    if (!actions) return;
    const messageId = request.message.message_id;
    void actions
      .submitMessage(request)
      .then((result) => {
        if (result.kind === 'reply_exists') {
          // The suggestion already has its conversation: open that one, keeping the draft for it.
          setComposer((current) =>
            current.submissionStartFence?.messageId === messageId
              ? {
                  ...current,
                  submission: null,
                  submissionStartFence: null,
                  failureKind: null,
                  refreshState: 'idle',
                  initialActionId: result.actionId,
                }
              : current
          );
          return;
        }
        if (result.kind !== 'submitted') {
          setComposer((current) => {
            if (current.submissionStartFence?.messageId !== messageId) return current;
            const submission = current.submission ? { request, state: 'failed' as const } : null;
            return {
              ...current,
              submission,
              failureKind: submission ? result.kind : current.failureKind,
              refreshState: submission ? 'idle' : current.refreshState,
              submissionStartFence: null,
            };
          });
          return;
        }
        const { response } = result;
        setComposer((current) => {
          const fence = current.submissionStartFence;
          if (fence?.messageId !== messageId) return current;
          const submission = current.submission
            ? ({ request, state: 'awaiting_refresh' } as const)
            : null;
          return {
            ...current,
            submission,
            failureKind: submission ? null : current.failureKind,
            refreshState: submission ? 'idle' : current.refreshState,
            initialActionId:
              submission && request.target.kind === 'new'
                ? response.action_id
                : current.initialActionId,
            submissionStartFence:
              response.disposition === 'started'
                ? { ...fence, processId: response.process_id }
                : null,
          };
        });
      })
      .catch(() => {
        setComposer((current) => {
          if (current.submissionStartFence?.messageId !== messageId) return current;
          if (!current.submission) return { ...current, submissionStartFence: null };
          return {
            ...current,
            submission: { request, state: 'failed' },
            failureKind: 'transport',
            refreshState: 'idle',
          };
        });
      });
  };
  // 押しただけの操作なので、送る本文も、会話に出す吹き出しもない。開いた run の
  // 続きは main 側の refresh と live relay が運ぶ。
  const requestResume = (visiblePages: ActionConversationPageChain | null) => {
    const actionId = visiblePages?.[0].action.action_id;
    if (!actions || actionId === undefined) return;
    if (composer.resume !== null && composer.resume.state !== 'failed') return;
    const messageId = composer.resume?.messageId ?? crypto.randomUUID();
    const generation = composerGenerationRef.current;
    setComposer((current) => ({ ...current, resume: { messageId, state: 'requesting' } }));
    const settle = (state: 'awaiting_refresh' | 'failed') =>
      setComposer((current) => {
        // 会話がリセットされて入力欄が入れ替わったあとに届いた応答は、その入力欄のものではない。
        if (generation !== composerGenerationRef.current) return current;
        if (current.resume?.messageId !== messageId) return current;
        return { ...current, resume: { messageId, state } };
      });
    void actions
      .resumeAction({ actionId, messageId })
      .then((result) => settle(result.kind === 'submitted' ? 'awaiting_refresh' : 'failed'))
      .catch(() => settle('failed'));
  };
  const { images, files } = attachmentPayload(composer.attachments);
  // The same draft is either the message content or the approval supplement; both are trimmed
  // before sending, so one conversion serves both.
  const projectRefs: ActionProjectRef[] = composer.mentions.map((mention) => ({
    project_id: mention.projectId,
    display_name: mention.displayName,
    paths: [...mention.paths],
    ...codePointSpanInTrimmedText(composer.draft, mention.start, mention.end),
  }));
  const canAttach = canAttachMore(composer.attachments);
  const supplement = normalizeActionSupplement(composer.draft);
  const supplementInvalid = supplement !== null && !isActionSupplementWithinLimit(supplement);
  const submitDraft = (
    visiblePages: ActionConversationPageChain | null,
    canStartConversation: boolean,
    activeProcessId: string | null,
    sequence: number,
    initialApprovalMode: ActionApprovalMode | null,
    replyToSuggestionId: string | null
  ) => {
    // 送信ボタンと Enter は canSend で塞いであるが、form の submit はそれを通らない。
    // 書き込み中の画像を残したまま送ると、その画像は次のメッセージに付いてしまう。
    if (composer.submission || composer.attachmentsInFlight > 0) return;
    const target = visiblePages
      ? ({
          kind: 'existing',
          action_id: visiblePages[0].action.action_id,
          expected_process_id: activeProcessId,
        } as const)
      : canStartConversation && initialApprovalMode !== null
        ? ({
            kind: 'new',
            approval_mode: initialApprovalMode,
            ...(replyToSuggestionId === null
              ? {}
              : { reply_to_suggestion_id: replyToSuggestionId }),
          } as const)
        : null;
    if (target === null) return;
    const parsed = ActionMessageRequestSchema.safeParse({
      target,
      message: {
        version: 1,
        message_id: crypto.randomUUID(),
        content: composer.draft,
        images,
        language,
        project_refs: projectRefs,
        files,
      },
    });
    if (!parsed.success) {
      setComposer((current) => ({ ...current, validationFailed: true }));
      return;
    }
    const request = parsed.data;
    setComposer((current) => ({
      ...current,
      attachmentFailure: null,
      validationFailed: false,
      submission: { request, state: 'submitting' },
      submissionStartFence: {
        messageId: request.message.message_id,
        sequence,
        processId: null,
      },
      failureKind: null,
      refreshState: 'idle',
    }));
    sendRequest(request);
  };
  const attachFiles = async (picked: readonly File[]) => {
    if (!actions || composer.submission || picked.length === 0) return;
    // The write outlives the composer it started in. A conversation reset — an account switch,
    // among others — replaces that composer, and the storage path this produces belongs to the
    // signed-in user at write time, so it must never surface in the composer that replaced it.
    const generation = composerGenerationRef.current;
    // Sending is blocked until this settles. Writing an 8 MB image takes a sha256 and a full
    // decode, and a send that lands first would clear the attachments, leaving the image to be
    // merged into the *next* message: the user's image silently missing from the one they meant.
    setComposer((current) => ({
      ...current,
      attachmentsInFlight: current.attachmentsInFlight + 1,
    }));
    const { accepted, failure } = await stageFiles(actions, picked, composer.attachments);
    if (generation !== composerGenerationRef.current) {
      // A replaced composer already starts at zero attachments in flight, so the result is
      // dropped rather than decrementing a counter it never incremented. Its staged documents
      // will never be sent.
      discardDocuments(actions, accepted);
      return;
    }
    setComposer((current) => {
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
    setComposer((current) => ({
      ...current,
      attachments: current.attachments.filter(
        (attachment) => attachmentKey(attachment) !== attachmentKey(removed)
      ),
      attachmentFailure: null,
    }));
  };
  const refreshSubmission = async (focusOwner: HTMLButtonElement) => {
    const submission = composer.submission;
    const actionId =
      submission?.request.target.kind === 'existing'
        ? submission.request.target.action_id
        : composer.initialActionId;
    if (!actions || !submission || actionId === null || composer.refreshState === 'loading') return;
    if (focusOwner.contains(document.activeElement)) restoreComposerFocusRef.current = true;
    const messageId = submission.request.message.message_id;
    const refreshScope = submissionRefreshScopeRef.current;
    setComposer((current) =>
      current.submission?.request.message.message_id === messageId
        ? { ...current, refreshState: 'loading' }
        : current
    );
    try {
      const result = await actions.readConversationPage({
        actionId,
        cursor: null,
        limit: ACTION_CONVERSATION_PAGE_LIMIT,
      });
      if (refreshScope !== submissionRefreshScopeRef.current) return;
      if ('kind' in result) {
        setComposer((current) =>
          current.submission?.request.message.message_id === messageId
            ? { ...current, refreshState: 'failed' }
            : current
        );
        return;
      }
      onRefreshedPage(result);
      setComposer((current) => {
        if (current.submission?.request.message.message_id !== messageId) return current;
        if (current.failureKind === 'expected_process_conflict') {
          return {
            ...current,
            submission: null,
            failureKind: null,
            refreshState: 'idle',
          };
        }
        return reconcileCanonicalSubmission({ ...current, refreshState: 'idle' }, result);
      });
    } catch {
      if (refreshScope !== submissionRefreshScopeRef.current) return;
      setComposer((current) =>
        current.submission?.request.message.message_id === messageId
          ? { ...current, refreshState: 'failed' }
          : current
      );
    }
  };
  // Typing dismisses a failed resume's notice.
  const changeDraft = (draft: string, mentions: readonly ComposerMention[]) =>
    setComposer((current) => ({
      ...current,
      draft,
      mentions,
      validationFailed: false,
      resume: current.resume?.state === 'failed' ? null : current.resume,
    }));
  const retrySubmission = () => {
    const request = composer.submission?.request;
    if (
      !request ||
      composer.submission?.state !== 'failed' ||
      composer.failureKind !== 'transport'
    ) {
      return;
    }
    setComposer((current) =>
      current.submission?.request.message.message_id === request.message.message_id
        ? {
            ...current,
            submission: { request, state: 'submitting' },
            failureKind: null,
          }
        : current
    );
    sendRequest(request);
  };
  return {
    composer,
    conversationActionId,
    images,
    files,
    canAttach,
    projectRefs,
    supplement,
    supplementInvalid,
    setComposer,
    composerGenerationRef,
    submissionRefreshScopeRef,
    restoreComposerFocusRef,
    submitDraft,
    requestResume,
    changeDraft,
    attachFiles,
    removeAttachment,
    refreshSubmission,
    retrySubmission,
  };
}
