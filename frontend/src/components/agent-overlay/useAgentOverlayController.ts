import { useCallback, useEffect, useReducer, useRef, useState } from 'react';
import { MIN_HEIGHT, getMaxHeight } from './Styled';
import type { AgentOverlayState, OverlaySnapshotPayload } from './model/overlayTypes';
import { createInitialAgentOverlayState, reduceAgentOverlayState } from './model/reducer';
import type { AcceptActionRequest } from '@/types/websocket';
type SuggestionAcceptance = Omit<AcceptActionRequest, 'suggestionId' | 'commandId'>;
import { getCollapsedPreviewHeightPx, shouldExpandScrollableContent } from './layoutMetrics';

const RESIZE_EXPAND_THRESHOLD_PX = 4;
const ACTION_RESIZE_BUFFER_PX = 32;
const SUGGESTION_RESIZE_BUFFER_PX = 18;
const REQUEST_STATE_IDLE: AgentOverlayState['requestState'] = 'idle';
const REQUEST_STATE_REQUESTING: AgentOverlayState['requestState'] = 'requesting';

export type AgentOverlayController = {
  state: AgentOverlayState;
  // refs
  headerRef: React.RefObject<HTMLDivElement>;
  scrollableContentRef: React.RefObject<HTMLDivElement>;
  contentInnerRef: React.RefObject<HTMLDivElement>;
  answerAreaRef: React.RefObject<HTMLDivElement>;
  footerRef: React.RefObject<HTMLDivElement>;
  composerRef: React.RefObject<HTMLDivElement>;
  containerRef: React.RefObject<HTMLDivElement>;
  // handlers
  onToggleExpand: () => void;
  onClose: () => void;
  onAccept: (options: SuggestionAcceptance) => void;
  acceptFailed: boolean;
  onReject: () => void;
  onStop: (processId?: string) => void;
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === 'object';
}

function isOverlaySnapshotPayload(value: unknown): value is OverlaySnapshotPayload {
  return (
    isRecord(value) &&
    isRecord(value.snapshot) &&
    typeof value.snapshot.suggestionId === 'string' &&
    typeof value.snapshot.lastSequence === 'number'
  );
}

export function useAgentOverlayController(isStandalone: boolean): AgentOverlayController {
  const [state, dispatch] = useReducer(
    reduceAgentOverlayState,
    undefined,
    createInitialAgentOverlayState
  );
  const effectiveVisible = isStandalone || state.isOverlayVisible;
  const effectiveActionPhase = isStandalone || state.isActionPhase;

  const headerRef = useRef<HTMLDivElement>(null);
  const scrollableContentRef = useRef<HTMLDivElement>(null);
  const contentInnerRef = useRef<HTMLDivElement>(null);
  const answerAreaRef = useRef<HTMLDivElement>(null);
  const footerRef = useRef<HTMLDivElement>(null);
  const composerRef = useRef<HTMLDivElement>(null);
  const containerRef = useRef<HTMLDivElement>(null);

  const [failedSuggestionId, setFailedSuggestionId] = useState<string | null>(null);
  const manualResizeRef = useRef<boolean>(false);
  const isActionPhaseRef = useRef<boolean>(false);

  // 最新値参照用（stale closure回避）
  const suggestionIdRef = useRef<string | null>(null);
  const processIdRef = useRef<string | null>(null);
  const reactionStateRef = useRef<'accepted' | 'rejected' | null>(null);
  const interactionContractRef = useRef<'action_offer' | 'message_only' | null>(null);
  const decisionLockedRef = useRef<boolean>(false);
  const requestStateRef = useRef<AgentOverlayState['requestState']>(REQUEST_STATE_IDLE);
  const historyExpandOverrideRef = useRef<boolean | null>(null);
  const isExpandedRef = useRef<boolean>(true);

  useEffect(() => {
    suggestionIdRef.current = state.suggestionId;
    processIdRef.current = state.currentProcessId;
    reactionStateRef.current = state.reactionState;
    interactionContractRef.current = state.interactionContract;
    decisionLockedRef.current = state.decisionLocked;
    requestStateRef.current = state.requestState;
    historyExpandOverrideRef.current = state.historyExpandOverride;
    isExpandedRef.current = state.isExpanded;
    isActionPhaseRef.current = effectiveActionPhase;
  }, [
    state.suggestionId,
    state.currentProcessId,
    state.reactionState,
    state.interactionContract,
    state.reactionTimestamp,
    state.actionStatusState,
    state.decisionLocked,
    state.requestState,
    state.historyExpandOverride,
    state.isExpanded,
    effectiveActionPhase,
  ]);

  const measureAndResize = useCallback((options?: { manual?: boolean }) => {
    const container = containerRef.current;
    if (!container) return;

    // NOTE:
    // - offsetHeight は「現在表示されている高さ」なので、内容が増えてスクロールになっても値が増えず、
    //   ウィンドウが伸びない（=スクロールが残る）根本原因になる。
    // - ここでは scrollHeight を使って「必要な内容高さ」を推定し、その分だけウィンドウをリサイズする。
    const headerEl = headerRef.current;
    const footerEl = footerRef.current;
    const composerEl = composerRef.current;
    const scrollableEl = scrollableContentRef.current;
    const contentInnerEl = contentInnerRef.current;

    const px = (v: string) => {
      const n = Number.parseFloat(v || '0');
      return Number.isFinite(n) ? n : 0;
    };
    const outerMarginY = (el: HTMLElement) => {
      const cs = window.getComputedStyle(el);
      return px(cs.marginTop) + px(cs.marginBottom);
    };

    // Header/Footer は「見える分 + 外側余白」
    const headerH = headerEl
      ? Math.ceil(headerEl.getBoundingClientRect().height + outerMarginY(headerEl))
      : 0;
    const footerH = footerEl
      ? Math.ceil(footerEl.getBoundingClientRect().height + outerMarginY(footerEl))
      : 0;
    // composer はスクロール領域の外にあるので、その分の高さを別に確保する。
    const composerH = composerEl
      ? Math.ceil(composerEl.getBoundingClientRect().height + outerMarginY(composerEl))
      : 0;

    // ScrollableContent は scrollHeight が margin を含まないため、外側余白も足す
    const fullContentHeight = contentInnerEl
      ? Math.ceil(contentInnerEl.getBoundingClientRect().height + outerMarginY(contentInnerEl))
      : scrollableEl
        ? Math.ceil(scrollableEl.scrollHeight + outerMarginY(scrollableEl))
        : 0;
    const scrollNeeded =
      !isExpandedRef.current && scrollableEl
        ? Math.ceil(scrollableEl.getBoundingClientRect().height + outerMarginY(scrollableEl))
        : fullContentHeight;

    const cs = window.getComputedStyle(container);
    const containerPad =
      px(cs.paddingTop) + px(cs.paddingBottom) + px(cs.borderTopWidth) + px(cs.borderBottomWidth);

    const needed = Math.ceil(containerPad + headerH + footerH + composerH + scrollNeeded);
    if (!needed) return;

    const mode = isActionPhaseRef.current ? 'action' : 'suggestion';
    const maxHeight = getMaxHeight(mode);
    // NOTE:
    // - フォントレンダリングや sub-pixel、スクロールバー幅などで「あと少し」足りずに
    //   overflow が発生することがあるため、バッファを持たせてスクロール残りを防ぐ。
    // - action は「最大で全画面くらい」を優先するため、少し多めに確保する。
    const resizeBufferPx = isActionPhaseRef.current
      ? ACTION_RESIZE_BUFFER_PX
      : SUGGESTION_RESIZE_BUFFER_PX;
    const targetHeight = Math.min(maxHeight, Math.max(MIN_HEIGHT, needed + resizeBufferPx));

    try {
      window.electron?.agentOverlay?.resize(targetHeight);
    } catch {
      // no-op
    }

    if (options?.manual) return;

    // Auto expand/collapse (既存挙動互換)
    if (historyExpandOverrideRef.current === null && !manualResizeRef.current) {
      const collapsedPreviewHeight = scrollableEl
        ? getCollapsedPreviewHeightPx(px(window.getComputedStyle(scrollableEl).lineHeight))
        : MIN_HEIGHT;
      const contentRequiresExpansion = shouldExpandScrollableContent({
        contentHeightPx: fullContentHeight,
        collapsedPreviewHeightPx: collapsedPreviewHeight,
        thresholdPx: RESIZE_EXPAND_THRESHOLD_PX,
      });
      const shouldExpand =
        contentRequiresExpansion || targetHeight > MIN_HEIGHT + RESIZE_EXPAND_THRESHOLD_PX;
      if (shouldExpand !== isExpandedRef.current) {
        dispatch({ type: 'SET_EXPANDED', value: shouldExpand });
      }
    }
  }, []);

  // IPC: orchestration events
  useEffect(() => {
    const off = window.electron?.orchestration?.onEvent?.((payload) => {
      dispatch({ type: 'SERVER_EVENT', event: payload });
    });
    return () => {
      try {
        off?.();
      } catch {
        // no-op
      }
    };
  }, []);

  // IPC: overlay snapshot
  useEffect(() => {
    const removeSnapshotListener = window.electron?.agentOverlay?.onSnapshot?.(
      (payload: unknown) => {
        try {
          if (isOverlaySnapshotPayload(payload)) {
            dispatch({ type: 'HYDRATE_SNAPSHOT', payload });
          }
        } catch {
          // no-op
        }
      }
    );
    return () => {
      try {
        removeSnapshotListener?.();
      } catch {
        // no-op
      }
    };
  }, []);

  // Auto resize when visibility or expansion changes
  useEffect(() => {
    if (!effectiveVisible) return;
    const effect = () => {
      measureAndResize();
      manualResizeRef.current = false;
    };
    if (typeof window !== 'undefined' && 'requestAnimationFrame' in window) {
      window.requestAnimationFrame(effect);
    } else {
      effect();
    }
  }, [
    effectiveVisible,
    state.isExpanded,
    state.isSuggestionStreamFinished,
    state.isActionStreamFinished,
    state.reactionState,
    measureAndResize,
  ]);

  // 本文と折り畳みプレビューの実サイズを監視して再計測する。
  useEffect(() => {
    if (!effectiveVisible) return;
    if (typeof ResizeObserver === 'undefined') return;

    let rafId: number | null = null;
    const scheduleMeasure = () => {
      if (rafId !== null) return;
      rafId = window.requestAnimationFrame(() => {
        rafId = null;
        measureAndResize();
      });
    };

    const observer = new ResizeObserver(() => {
      scheduleMeasure();
    });

    const targets = [
      contentInnerRef.current,
      scrollableContentRef.current,
      headerRef.current,
      footerRef.current,
      composerRef.current,
    ].filter((element): element is HTMLDivElement => Boolean(element));

    for (const target of targets) {
      observer.observe(target);
    }

    scheduleMeasure();

    return () => {
      observer.disconnect();
      if (rafId !== null) {
        window.cancelAnimationFrame(rafId);
      }
    };
  }, [effectiveVisible, measureAndResize]);

  // Orchestration status (resume hints)
  useEffect(() => {
    const offStatus = window.electron?.orchestration?.onStatus?.((st: unknown) => {
      if (!st || typeof st !== 'object') return;
      if (!st) return;
      if ('status' in st && st.status === 'resume_requested') {
        if ('process_id' in st && st.process_id) {
          dispatch({ type: 'SET_CURRENT_PROCESS_ID', processId: String(st.process_id) });
        }
        if ('suggestion_id' in st && st.suggestion_id) {
          // 既存挙動は「suggestionIdだけ上書き」なので、known ids は残す
          dispatch({
            type: 'SET_SUGGESTION_ID',
            suggestionId: String(st.suggestion_id),
            resetKnownIds: false,
          });
        }
      }
    });
    return () => {
      offStatus?.();
    };
  }, []);

  const onToggleExpand = useCallback(() => {
    manualResizeRef.current = true;
    dispatch({ type: 'TOGGLE_EXPAND' });

    const finalizeResize = () => {
      measureAndResize({ manual: true });
      manualResizeRef.current = false;
    };
    if (typeof window !== 'undefined' && 'requestAnimationFrame' in window) {
      window.requestAnimationFrame(() => window.requestAnimationFrame(finalizeResize));
    } else {
      setTimeout(finalizeResize, 0);
    }
  }, [measureAndResize]);

  const onReject = useCallback(() => {
    if (decisionLockedRef.current) return;
    if (interactionContractRef.current !== 'action_offer') return;
    if (reactionStateRef.current !== null) return;
    dispatch({ type: 'SET_DECISION_LOCKED', value: true });
    try {
      window.electron?.agentOverlay?.rejectAction({
        id: 'some-notification-id',
        action: 'dismiss',
      });
    } catch {
      // no-op
    }
    const sid = suggestionIdRef.current;
    if (sid) {
      window.electron?.orchestration?.send({
        event: 'dismiss_suggestion',
        data: { suggestion_id: sid },
      });
    }
  }, []);

  const onAccept = useCallback(async (options: SuggestionAcceptance) => {
    if (decisionLockedRef.current) return;
    if (interactionContractRef.current !== 'action_offer') return;
    if (reactionStateRef.current !== null) return;
    const sid = suggestionIdRef.current;
    const acceptAction = window.electron?.orchestration?.acceptAction;
    if (!sid || !acceptAction) return;
    decisionLockedRef.current = true;
    setFailedSuggestionId(null);
    dispatch({ type: 'SET_DECISION_LOCKED', value: true });
    dispatch({ type: 'SET_REQUEST_STATE', value: REQUEST_STATE_REQUESTING });
    try {
      await acceptAction({ suggestionId: sid, commandId: null, ...options });
    } catch {
      if (suggestionIdRef.current !== sid) return;
      decisionLockedRef.current = false;
      dispatch({ type: 'SET_REQUEST_STATE', value: REQUEST_STATE_IDLE });
      dispatch({ type: 'SET_DECISION_LOCKED', value: false });
      setFailedSuggestionId(sid);
    }
  }, []);

  const onClose = useCallback(() => {
    try {
      window.electron?.agentOverlay?.hide();
    } catch {
      // no-op
    }
  }, []);

  const onStop = useCallback((processId?: string) => {
    try {
      window.electron?.agentOverlay?.stopAction();
    } catch {
      // no-op
    }
    const pid = processId ?? processIdRef.current;
    if (pid) {
      window.electron?.orchestration?.send({ event: 'stop_process', data: { process_id: pid } });
    }
  }, []);

  return {
    state,
    headerRef,
    scrollableContentRef,
    contentInnerRef,
    answerAreaRef,
    footerRef,
    composerRef,
    containerRef,
    onToggleExpand,
    onClose,
    onAccept,
    acceptFailed: failedSuggestionId !== null && failedSuggestionId === state.suggestionId,
    onReject,
    onStop,
  };
}
