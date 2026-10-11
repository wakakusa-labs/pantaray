import { randomUUID } from 'node:crypto';

import type { ActionLiveRefreshOutcome } from '../actions/actionLiveCore';
import type { CaptureGateState } from '../ipc/context';
import type { LocalRuntimeState } from '../auth/localRuntimeState';

type WindowOpenResult = 'created' | 'loading' | 'focused';

export type ConversationOverlayOpenResult = WindowOpenResult | 'initializing' | 'recording_intro';

/** The request the closed gate deferred, replayed once recording starts. */
type PendingOverlayRequest = { kind: 'new' } | { kind: 'action'; actionId: string };

export class ConversationOverlayStateError extends Error {}

export function createConversationOverlayOwner(params: {
  getRuntimeState: () => LocalRuntimeState;
  openOverlay: (overlayId: string, actionId: string | null) => WindowOpenResult;
  destroyOverlay: (overlayId: string) => void;
  bindActionToOverlay: (actionId: string, overlayId: string) => void;
  resolveOverlayIdForAction: (actionId: string) => string | null;
  hasOverlayWindow: (overlayId: string) => boolean;
  refreshAndResumeConversation: (actionId: string) => Promise<ActionLiveRefreshOutcome>;
  /** Shows a window that waits for its conversation to be laid out. */
  revealOverlay: (overlayId: string) => void;
  /** Whether macOS grants this app the permissions the recorder needs. */
  holdsCaptureOsPermissions: () => boolean;
  /** Brings the main window forward, where the recording screen is shown. */
  presentRecordingIntro: () => void;
  createOverlayId?: () => string;
}) {
  const ownedOverlayIds = new Set<string>();
  let boundSubject: string | null = null;
  let pendingRequest: PendingOverlayRequest | null = null;
  // "Later" is answered once per app run, not once per window: the recording screen
  // lives in the main window, which the user may close and reopen at will, and
  // reopening it must not ask again. Nothing is stored, so a relaunch asks again.
  let introDismissed = false;

  // Both belong to the subject that answered the screen and deferred the request.
  const forgetIntroState = (): void => {
    pendingRequest = null;
    introDismissed = false;
  };

  // A conversation window is destroyed when the user closes it, so its id would
  // otherwise stay owned for the rest of the session.
  const forgetClosedOverlays = (): void => {
    for (const overlayId of ownedOverlayIds) {
      if (!params.hasOverlayWindow(overlayId)) ownedOverlayIds.delete(overlayId);
    }
  };

  const destroyOwnedOverlays = (): void => {
    forgetClosedOverlays();
    for (const overlayId of ownedOverlayIds) params.destroyOverlay(overlayId);
    ownedOverlayIds.clear();
    boundSubject = null;
    forgetIntroState();
  };

  /**
   * Without the macOS permissions the recorder needs, the app can read nothing at
   * all, so a conversation window is not opened: the request is remembered and the
   * main window is brought forward instead, where the pending request it reads back
   * is what opens the recording screen. Starting recording from there — which is
   * what obtains those permissions — then continues to the window the user asked
   * for. Recording being off is not this gate: a user who granted the permissions
   * may always start a conversation of their own.
   */
  const passesOsPermissionGate = (request: PendingOverlayRequest): boolean => {
    if (params.holdsCaptureOsPermissions()) return true;
    pendingRequest = request;
    params.presentRecordingIntro();
    return false;
  };

  const onOwnerChanged = (nextSubject: string | null): void => {
    if (boundSubject === nextSubject) return;
    // The deferred request and the answered screen outlive every window: the gate
    // opens no window at all, so destroying the owned ones would not clear them.
    forgetIntroState();
    if (ownedOverlayIds.size > 0) destroyOwnedOverlays();
  };

  // New windows and history reopens use only the helper-confirmed owner.
  const claimSubject = (): { kind: 'blocked'; result: 'initializing' } | { kind: 'ready' } => {
    const runtime = params.getRuntimeState();
    if (runtime.status === 'degraded') {
      throw new ConversationOverlayStateError('Local runtime is unavailable.');
    }
    if (runtime.status !== 'ready' || !runtime.owner) {
      return { kind: 'blocked', result: 'initializing' };
    }
    const subject = runtime.owner.id;
    if (boundSubject !== subject) destroyOwnedOverlays();
    boundSubject = subject;
    forgetClosedOverlays();
    return { kind: 'ready' };
  };

  const openNewConversationOverlay = (): ConversationOverlayOpenResult => {
    const claim = claimSubject();
    if (claim.kind === 'blocked') return claim.result;
    if (!passesOsPermissionGate({ kind: 'new' })) return 'recording_intro';
    // Every request starts an empty conversation in its own window. Reusing one
    // id would re-focus whatever conversation that window already holds, and a
    // window that may still be running must not be destroyed to make room.
    const overlayId = `standalone:${(params.createOverlayId ?? randomUUID)()}`;
    ownedOverlayIds.add(overlayId);
    return params.openOverlay(overlayId, null);
  };

  const openActionConversationOverlay = (actionId: string): ConversationOverlayOpenResult => {
    const normalizedActionId = actionId.trim();
    if (!normalizedActionId) {
      throw new ConversationOverlayStateError('Action conversation Overlay requires an Action.');
    }
    const claim = claimSubject();
    if (claim.kind === 'blocked') return claim.result;
    if (!passesOsPermissionGate({ kind: 'action', actionId: normalizedActionId })) {
      return 'recording_intro';
    }
    // An Action is displayed by at most one Overlay, and rebinding it would
    // redirect every live update away from the window that already shows it. So
    // whenever the association still names an open window — a standalone Overlay
    // this owner opened, or a Suggestion Overlay it does not own — that window is
    // focused instead of opening a second view of the same conversation.
    const associated = params.resolveOverlayIdForAction(normalizedActionId);
    const displayedOverlayId =
      associated && params.hasOverlayWindow(associated) ? associated : null;
    const overlayId = displayedOverlayId ?? `conversation:${normalizedActionId}`;
    if (displayedOverlayId === null) {
      ownedOverlayIds.add(overlayId);
      // Bind before opening so the refresh below, and every later live update,
      // reach this window instead of being dropped for an unmapped Action.
      params.bindActionToOverlay(normalizedActionId, overlayId);
    }
    const result = params.openOverlay(overlayId, normalizedActionId);
    // A read conversation reaches the window, which shows itself once it is laid out. Without
    // one, the window is shown anyway with its own retry rather than staying hidden.
    void params.refreshAndResumeConversation(normalizedActionId).then(
      (outcome) => {
        if (outcome !== 'refreshed') params.revealOverlay(overlayId);
      },
      (error: unknown) => {
        console.error('Failed to read the conversation for its window:', error);
        params.revealOverlay(overlayId);
      }
    );
    return result;
  };

  /** Opens the window a gated request asked for, once recording has started. */
  const openPendingRequest = (): void => {
    if (!pendingRequest || claimSubject().kind === 'blocked') return;
    const request = pendingRequest;
    pendingRequest = null;
    if (request?.kind === 'new') openNewConversationOverlay();
    else if (request) openActionConversationOverlay(request.actionId);
  };

  /**
   * What the recording screen reads — and the only place the app learns that the
   * gate's permission arrived, because it is granted in System Settings and nothing
   * here polls macOS. So this is also where a deferred conversation opens: the
   * screen closes on the very read that reports the permission granted, and a
   * request left waiting for a recording start made from it would never open at
   * all, then surprise the user in front of an unrelated start later.
   */
  const readGateState = (): CaptureGateState => {
    const osPermissionsGranted = params.holdsCaptureOsPermissions();
    if (osPermissionsGranted) openPendingRequest();
    return { osPermissionsGranted, introRequested: pendingRequest !== null, introDismissed };
  };

  /**
   * The user answered the recording screen with "later": the deferred conversation
   * is dropped, and the screen stops opening on its own until the app is relaunched
   * or another conversation asks for it.
   */
  const dismissIntro = (): CaptureGateState => {
    pendingRequest = null;
    introDismissed = true;
    return readGateState();
  };

  return {
    onOwnerChanged,
    openNewConversationOverlay,
    openActionConversationOverlay,
    openPendingRequest,
    readGateState,
    dismissIntro,
  };
}
