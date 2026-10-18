import { randomUUID } from 'crypto';

import type { IpcMainInvokeEvent } from 'electron';

import type { MainContext } from '../context';
import type { IpcRegistrar } from '../registrar';
import { IpcSenderRejectedError, type IpcSenderIdentity } from '../senderTrust';
import type {
  ActionMessageResponse,
  ActionMessageSubmitResult,
  ActionWithdrawMessageResult,
} from '../../actions/actionContracts';
import { LocalBackendRequestError } from '../../localBackend/client';
import { answeredSendOutcome, sendWork, type TaskSendOutcome } from '../../actions/taskDraftStore';
import {
  ActionConversationOverlayRequestSchema,
  ActionConversationPageRequestSchema,
  ActionMessageRequestSchema,
  ActionResumeRequestSchema,
  ActionStopRequestSchema,
  ActionToolOutputRequestSchema,
  ActionWithdrawMessageRequestSchema,
} from '../schemas/actions';
import { parseInput } from '../schemas/error';

type TurnSender = { kind: 'main' } | { kind: 'panel'; overlayId: string };

/** The Action's page could not be read, so its unfinished run was not resumed either. */
export class ActionConversationOpenError extends Error {
  constructor() {
    super('The Action conversation could not be read.');
    this.name = 'ActionConversationOpenError';
  }
}

/**
 * The window a turn is opened from. Sender trust admitted the window's role; this pins which
 * window it is. An Overlay panel is a target of its own (events reach it by the Action↔panel
 * association), while the main window receives every Action's updates already.
 */
function resolveTurnSender(ctx: MainContext, sender: IpcSenderIdentity): TurnSender | null {
  const mainWindow = ctx.windows.getMainWindow();
  if (mainWindow !== null && !mainWindow.isDestroyed() && mainWindow.webContents === sender) {
    return { kind: 'main' };
  }
  const overlayId = ctx.actions.resolveOverlayIdForSender(sender);
  return overlayId === null ? null : { kind: 'panel', overlayId };
}

type Turn = { sender: TurnSender; subjectId: string };

/** The window and owner a turn is opened for, pinned when the window asks for it. */
function resolveTurn(ctx: MainContext, event: IpcMainInvokeEvent): Turn {
  const sender = resolveTurnSender(ctx, event.sender);
  if (sender === null) throw new IpcSenderRejectedError('overlay_window_not_registered');
  const subjectId = ctx.actions.getCurrentSubjectId();
  if (subjectId === null) throw new Error('Missing authenticated user id.');
  return { sender, subjectId };
}

/**
 * ひとつの USER ターンを開く要求を、その結果の引き回しごと実行する。
 *
 * 送信と「再開」は同じターンの開き方なので、409 の読み替えも、オーバーレイとの
 * 対応付けも、新しい process への live relay の張り直しも同じでなければならない。
 * 送信中に owner が替わった、または panel が別の Overlay になったなら、結果は何も書かない。
 * 提案への返信は、開いた（または既にある）会話をその提案を映すすべての窓に知らせる。
 */
async function openUserTurn(
  ctx: MainContext,
  event: IpcMainInvokeEvent,
  { sender: turnSender, subjectId }: Turn,
  open: () => Promise<ActionMessageResponse>,
  replyToSuggestionId: string | null
): Promise<ActionMessageSubmitResult> {
  let response: ActionMessageResponse;
  try {
    response = await open();
  } catch (error) {
    if (error instanceof LocalBackendRequestError && error.status === 409) {
      if (error.errorCode === 'ExpectedProcessConflict') {
        return { kind: 'expected_process_conflict' } satisfies ActionMessageSubmitResult;
      }
      if (error.errorCode === 'MessageWithdrawn') {
        return { kind: 'withdrawn' } satisfies ActionMessageSubmitResult;
      }
      if (error.errorCode === 'ActionConflict') {
        // A reply refused because the suggestion already has its conversation opens that one.
        const existing =
          replyToSuggestionId === null
            ? null
            : (await ctx.overlay.resolveOverlayBootstrap(replyToSuggestionId))?.snapshot.actionId;
        return existing &&
          deliverTurn(ctx, event, turnSender, subjectId, existing, replyToSuggestionId)
          ? ({ kind: 'reply_exists', actionId: existing } satisfies ActionMessageSubmitResult)
          : ({ kind: 'action_conflict' } satisfies ActionMessageSubmitResult);
      }
    }
    throw error;
  }
  if (!deliverTurn(ctx, event, turnSender, subjectId, response.action_id, replyToSuggestionId)) {
    return { kind: 'submitted', response } satisfies ActionMessageSubmitResult;
  }
  if (response.disposition === 'started') {
    // A standalone (or follow-up) HTTP submit starts a new backend process
    // with no other live-relay attach path, so request the resume here or
    // no window receives lifecycle, pause, or step events.
    ctx.overlay.resumeLiveProcess({
      kind: 'action',
      processId: response.process_id,
      actionId: response.action_id,
      fromStart: true,
    });
  }
  return { kind: 'submitted', response } satisfies ActionMessageSubmitResult;
}

/**
 * Points the windows at the turn's Action: the suggestion replied to, the sending panel, and a
 * canonical refresh. False, with nothing written, when the owner or the panel changed meanwhile.
 */
function deliverTurn(
  ctx: MainContext,
  event: IpcMainInvokeEvent,
  turnSender: TurnSender,
  subjectId: string,
  actionId: string,
  replyToSuggestionId: string | null
): boolean {
  if (ctx.actions.getCurrentSubjectId() !== subjectId) return false;
  if (
    turnSender.kind === 'panel' &&
    ctx.actions.resolveOverlayIdForSender(event.sender) !== turnSender.overlayId
  ) {
    return false;
  }
  if (replyToSuggestionId !== null)
    ctx.overlay.recordSuggestionReply(replyToSuggestionId, actionId);
  if (turnSender.kind === 'panel')
    ctx.actions.registerActionAssociation(actionId, turnSender.overlayId);
  ctx.actions.refreshActionConversation(actionId);
  return true;
}

export function registerActionHandlers(ctx: MainContext, registrar: IpcRegistrar): void {
  registrar.handle('action:submitMessage', async (event, request) => {
    const parsed = parseInput(ActionMessageRequestSchema, 'action:submitMessage', request);
    const replyTo =
      parsed.target.kind === 'new' ? (parsed.target.reply_to_suggestion_id ?? null) : null;
    // The task's draft is cleared as its words go out and settled by how the send ends. The new
    // task's draft is the main window's: the small New task window keeps its own.
    const owner = ctx.actions.getCurrentSubjectId();
    const mainWindow = ctx.windows.getMainWindow();
    const fromMainWindow =
      mainWindow !== null && !mainWindow.isDestroyed() && mainWindow.webContents === event.sender;
    const work = sendWork(parsed);
    const drafted = owner !== null && (work !== 'new' || fromMainWindow);
    if (drafted) ctx.taskDrafts.clearForSend(owner, work);
    const settle = (outcome: TaskSendOutcome) => {
      if (drafted) ctx.taskDrafts.settleSend(owner, work, parsed, outcome);
    };
    try {
      const result = await openUserTurn(
        ctx,
        event,
        resolveTurn(ctx, event),
        () => ctx.actions.submitMessage(parsed),
        replyTo
      );
      settle(answeredSendOutcome(result));
      return result;
    } catch (error) {
      // The backend answered with a refusal, or the outcome is unknown and only a retry of
      // this exact request, which it dedupes, may follow.
      settle(
        error instanceof LocalBackendRequestError && error.status !== null && error.status < 500
          ? 'refused'
          : 'unknown'
      );
      throw error;
    }
  });
  registrar.handle('action:resume', async (event, request) =>
    openUserTurn(
      ctx,
      event,
      resolveTurn(ctx, event),
      () =>
        ctx.actions.resumeAction(parseInput(ActionResumeRequestSchema, 'action:resume', request)),
      null
    )
  );
  // Either answer changed the conversation, so every window showing it reads it again.
  registrar.handle('action:withdrawMessage', async (_event, request) => {
    const parsed = parseInput(
      ActionWithdrawMessageRequestSchema,
      'action:withdrawMessage',
      request
    );
    let result: ActionWithdrawMessageResult = { kind: 'withdrawn' };
    try {
      await ctx.actions.withdrawMessage(parsed);
    } catch (error) {
      const adopted =
        error instanceof LocalBackendRequestError &&
        error.status === 409 &&
        error.errorCode === 'MessageAdopted';
      if (!adopted) throw error;
      result = { kind: 'adopted' };
    }
    ctx.actions.refreshActionConversation(parsed.actionId);
    return result;
  });
  // Stop, from either window. The run ends once its subagents settle, so the page is read again and
  // the live run kept attached: the terminal then reaches the window however long that takes. A
  // refused Stop reads it too, since the run it named may already have ended.
  registrar.handle('action:stop', async (_event, request) => {
    const parsed = parseInput(ActionStopRequestSchema, 'action:stop', request);
    try {
      await ctx.actions.stopAction(parsed);
    } finally {
      await ctx.actions.refreshAndResumeActionConversation(parsed.actionId);
    }
  });
  // Send now: stop the run the waiting messages are queued for and, once it has ended, resume,
  // which runs them first. Main carries it through, so a window closed meanwhile does not drop it.
  // A run that ended on its own has already handed them to a successor run, so the Stop naming
  // it is refused, or, once that run has ended too, the resume is; either way nothing changes.
  registrar.handle('action:sendNow', async (event, request) => {
    const parsed = parseInput(ActionStopRequestSchema, 'action:sendNow', request);
    const turn = resolveTurn(ctx, event);
    try {
      await ctx.actions.stopAction(parsed);
    } finally {
      await ctx.actions.refreshAndResumeActionConversation(parsed.actionId);
    }
    await ctx.actions.whenRunEnds(parsed);
    if (ctx.actions.getCurrentSubjectId() !== turn.subjectId) return;
    await openUserTurn(
      ctx,
      event,
      turn,
      () => ctx.actions.resumeAction({ actionId: parsed.actionId, messageId: randomUUID() }),
      null
    );
  });
  // The main window shows an Action in place: its page, and its live run if one is unfinished.
  // Without the page no run is resumed, so the window must hear of the failure to retry.
  registrar.handle('action:openConversation', async (_event, request) => {
    const { actionId } = parseInput(
      ActionConversationOverlayRequestSchema,
      'action:openConversation',
      request
    );
    if ((await ctx.actions.refreshAndResumeActionConversation(actionId)) === 'failed') {
      throw new ActionConversationOpenError();
    }
  });
  registrar.handle('action:readConversationPage', async (_event, request) => {
    const parsed = parseInput(
      ActionConversationPageRequestSchema,
      'action:readConversationPage',
      request
    );
    try {
      return await ctx.actions.readConversationPage(parsed);
    } catch (error) {
      if (
        parsed.cursor !== null &&
        error instanceof LocalBackendRequestError &&
        error.status === 409
      ) {
        return { kind: 'stale_cursor' } as const;
      }
      throw error;
    }
  });
  registrar.handle('action:readToolOutputPage', async (_event, request) =>
    ctx.actions.readToolOutputPage(
      parseInput(ActionToolOutputRequestSchema, 'action:readToolOutputPage', request)
    )
  );
}
