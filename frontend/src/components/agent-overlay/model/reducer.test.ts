import { describe, expect, it } from 'vitest';
import { createInitialAgentOverlayState, reduceAgentOverlayState } from './reducer';
import type { OverlayServerEvent, OverlaySnapshotPayload } from './overlayTypes';

function ev(
  event: OverlayServerEvent['event'],
  data: Record<string, unknown>,
  meta?: Record<string, unknown>
): OverlayServerEvent {
  return { event, data, meta } as OverlayServerEvent;
}

function snapshotPayload(
  overrides: Partial<OverlaySnapshotPayload['snapshot']> = {},
  initialUiState?: OverlaySnapshotPayload['initialUiState']
): OverlaySnapshotPayload {
  return {
    snapshot: {
      suggestionId: 'S1',
      commandId: 'CMD1',
      interactionContract: 'action_offer',
      suggestionText: '提案文',
      reactionState: null,
      reactionTimestamp: null,
      dismissReason: null,
      artifacts: [],
      actionPhase: 'idle',
      actionStatus: null,
      actionErrorCode: null,
      actionFailureStage: null,
      actionFailureMessagePublic: null,
      processId: null,
      actionId: null,
      updatedAt: '2026-01-01T00:00:00Z',
      lastSequence: 1,
      isLive: false,
      ...overrides,
    },
    initialUiState,
  };
}

describe('AgentOverlay reducer', () => {
  it('HYDRATE_SNAPSHOT(requesting) で提案本文を初期化する', () => {
    const next = reduceAgentOverlayState(createInitialAgentOverlayState(), {
      type: 'HYDRATE_SNAPSHOT',
      payload: snapshotPayload({ actionPhase: 'requesting', isLive: true }),
    });

    expect(next.suggestionId).toBe('S1');
    expect(next.suggestionText).toBe('提案文');
    expect(next.requestState).toBe('requesting');
    expect(next.isOverlayVisible).toBe(true);
    expect(next.lastSequence).toBe(1);
  });

  it('古い sequence の snapshot は無視する', () => {
    let state = reduceAgentOverlayState(createInitialAgentOverlayState(), {
      type: 'HYDRATE_SNAPSHOT',
      payload: snapshotPayload({ lastSequence: 3, actionPhase: 'accepted_pending_start' }),
    });

    state = reduceAgentOverlayState(state, {
      type: 'HYDRATE_SNAPSHOT',
      payload: snapshotPayload({
        lastSequence: 2,
        actionPhase: 'requesting',
        suggestionText: '古い',
      }),
    });

    expect(state.lastSequence).toBe(3);
    expect(state.requestState).toBe('accepted_pending_start');
    expect(state.suggestionText).toBe('提案文');
  });

  it('initialUiState.expand を反映する', () => {
    const next = reduceAgentOverlayState(createInitialAgentOverlayState(), {
      type: 'HYDRATE_SNAPSHOT',
      payload: snapshotPayload({ actionPhase: 'idle' }, { expand: false }),
    });

    expect(next.historyExpandOverride).toBe(false);
    expect(next.isExpanded).toBe(false);
  });

  it('履歴の確定済み suggestion snapshot は streaming 表示にしない', () => {
    const next = reduceAgentOverlayState(createInitialAgentOverlayState(), {
      type: 'HYDRATE_SNAPSHOT',
      payload: snapshotPayload({ actionPhase: 'idle', isLive: false }),
    });

    expect(next.suggestionText).toBe('提案文');
    expect(next.isSuggestionStreamFinished).toBe(true);
  });

  it('live suggestion の途中 snapshot は streaming 表示を維持する', () => {
    const next = reduceAgentOverlayState(createInitialAgentOverlayState(), {
      type: 'HYDRATE_SNAPSHOT',
      payload: snapshotPayload({
        actionPhase: 'idle',
        interactionContract: null,
        isLive: true,
      }),
    });

    expect(next.suggestionText).toBe('提案文');
    expect(next.isSuggestionStreamFinished).toBe(false);
  });

  it('action_requested で accepted_pending_start に遷移する', () => {
    const state = reduceAgentOverlayState(createInitialAgentOverlayState(), {
      type: 'SERVER_EVENT',
      event: ev(
        'action_requested',
        {
          suggestion_id: 'S1',
          command_id: 'CMD1',
          accepted_at: '2026-01-01T00:00:02Z',
          committed_at: '2026-01-01T00:00:02Z',
        },
        { suggestion_id: 'S1', command_id: 'CMD1', kind: 'action' }
      ),
    });

    expect(state.reactionState).toBe('accepted');
    expect(state.requestState).toBe('accepted_pending_start');
    expect(state.actionStatusState).toBe('idle');
    expect(state.currentCommandId).toBe('CMD1');
  });

  it('suggestion process_completed で interaction contract を確定する', () => {
    let state = reduceAgentOverlayState(createInitialAgentOverlayState(), {
      type: 'HYDRATE_SNAPSHOT',
      payload: snapshotPayload({ actionPhase: 'requesting', isLive: true }),
    });

    state = reduceAgentOverlayState(state, {
      type: 'SERVER_EVENT',
      event: ev(
        'process_completed',
        {
          kind: 'suggestion',
          process_id: 'P-SUG',
          status: 'success',
          suggestion_id: 'S1',
          interaction_contract: 'message_only',
        },
        { suggestion_id: 'S1', process_id: 'P-SUG', kind: 'suggestion' }
      ),
    });

    expect(state.interactionContract).toBe('message_only');
    expect(state.decisionLocked).toBe(true);
    expect(state.isSuggestionStreamFinished).toBe(true);
  });

  it('action process_completed(success) は terminal control state を確定する', () => {
    let state = reduceAgentOverlayState(createInitialAgentOverlayState(), {
      type: 'HYDRATE_SNAPSHOT',
      payload: snapshotPayload({
        actionPhase: 'processing',
        actionStatus: 'processing',
        reactionState: 'accepted',
        processId: 'P1',
        actionId: 'A1',
        isLive: true,
      }),
    });

    state = reduceAgentOverlayState(state, {
      type: 'SERVER_EVENT',
      event: ev(
        'process_completed',
        {
          kind: 'action',
          process_id: 'P1',
          status: 'success',
          suggestion_id: 'S1',
          action_id: 'A1',
          command_id: 'CMD1',
        },
        {
          suggestion_id: 'S1',
          process_id: 'P1',
          action_id: 'A1',
          command_id: 'CMD1',
          kind: 'action',
        }
      ),
    });

    expect(state.actionStatusState).toBe('success');
    expect(state.isActionStreamFinished).toBe(true);
    expect(state.currentProcessId).toBeNull();
  });

  it('process_paused は Action の processing control state を維持する', () => {
    let state = reduceAgentOverlayState(createInitialAgentOverlayState(), {
      type: 'HYDRATE_SNAPSHOT',
      payload: snapshotPayload({
        actionPhase: 'processing',
        actionStatus: 'processing',
        reactionState: 'accepted',
        processId: 'P1',
        actionId: 'A1',
        isLive: true,
      }),
    });

    state = reduceAgentOverlayState(state, {
      type: 'SERVER_EVENT',
      event: ev(
        'process_paused',
        {
          kind: 'action',
          process_id: 'P1',
          status: 'processing',
          reason: 'approval_pending',
          suggestion_id: 'S1',
          action_id: 'A1',
          command_id: 'CMD1',
          completed_at: '2026-03-08T00:00:03Z',
          approval_blockers: [
            {
              action_id: 'A1',
              approval_session_id: 'APS1',
              tool_request_id: 'TR1',
              tool_id: 'bash',
              intent_class: 'process_exec_local',
              command_summary: { kind: 'bash', command: 'npm test' },
            },
          ],
        },
        {
          suggestion_id: 'S1',
          process_id: 'P1',
          action_id: 'A1',
          command_id: 'CMD1',
          kind: 'action',
        }
      ),
    });

    expect(state.actionStatusState).toBe('processing');
    expect(state.isActionStreamFinished).toBe(false);
    expect(state.currentPhase).toBe('action');
    expect(state.currentProcessId).toBeNull();
  });

  it('preflight_rejected は decision を解除して idle に戻す', () => {
    let state = reduceAgentOverlayState(createInitialAgentOverlayState(), {
      type: 'HYDRATE_SNAPSHOT',
      payload: snapshotPayload({ actionPhase: 'requesting', isLive: true }),
    });

    state = reduceAgentOverlayState(state, {
      type: 'SERVER_EVENT',
      event: ev(
        'error',
        {
          error_code: 'WS_DEPENDENCY_UNAVAILABLE',
          error_type: 'internal_error',
          severity: 'error',
        },
        {
          kind: 'action',
          suggestion_id: 'S1',
          command_id: 'CMD1',
          stage: 'preflight_rejected',
          error_code: 'WS_DEPENDENCY_UNAVAILABLE',
        }
      ),
    });

    expect(state.requestState).toBe('idle');
    expect(state.reactionState).toBeNull();
    expect(state.decisionLocked).toBe(false);
    expect(state.currentCommandId).toBeNull();
  });

  it('persist_final_state_failed は terminal error を表示する', () => {
    let state = reduceAgentOverlayState(createInitialAgentOverlayState(), {
      type: 'HYDRATE_SNAPSHOT',
      payload: snapshotPayload({
        actionPhase: 'processing',
        actionStatus: 'processing',
        reactionState: 'accepted',
        processId: 'P1',
        actionId: 'A1',
        isLive: true,
      }),
    });

    state = reduceAgentOverlayState(state, {
      type: 'SERVER_EVENT',
      event: ev(
        'error',
        {
          error_code: 'WS_DEPENDENCY_UNAVAILABLE',
          error_type: 'internal_error',
          severity: 'error',
        },
        {
          kind: 'action',
          suggestion_id: 'S1',
          process_id: 'P1',
          action_id: 'A1',
          command_id: 'CMD1',
          stage: 'persist_final_state_failed',
          error_code: 'WS_DEPENDENCY_UNAVAILABLE',
        }
      ),
    });

    expect(state.actionStatusState).toBe('error');
    expect(state.isActionStreamFinished).toBe(true);
    expect(state.actionFailureStage).toBe('persist_final_state_failed');
  });

  it('message_only snapshot は decision をロックしたまま開く', () => {
    const state = reduceAgentOverlayState(createInitialAgentOverlayState(), {
      type: 'HYDRATE_SNAPSHOT',
      payload: snapshotPayload({
        interactionContract: 'message_only',
        actionPhase: 'idle',
      }),
    });

    expect(state.interactionContract).toBe('message_only');
    expect(state.decisionLocked).toBe(true);
  });
});
