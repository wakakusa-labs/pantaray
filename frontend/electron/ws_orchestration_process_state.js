const { InboundEvent } = require('../shared/events');

function normalizeProcessKind(kind) {
  return kind === 'action' || kind === 'suggestion' ? kind : null;
}

function applyProcessIdentity(state, identity = {}) {
  if (!state || !identity || typeof identity !== 'object') return;
  const kind = normalizeProcessKind(identity.kind);
  if (kind) state.kind = kind;
  if (typeof identity.suggestionId === 'string' && identity.suggestionId) {
    state.suggestionId = identity.suggestionId;
  }
  if (typeof identity.actionId === 'string' && identity.actionId) {
    state.actionId = identity.actionId;
  }
  if (typeof identity.commandId === 'string' && identity.commandId) {
    state.commandId = identity.commandId;
  }
}

function createProcessResumeState({
  sendRaw,
  forwardStatusToRenderers,
  showNotification,
  getCurrentSessionId,
}) {
  const processState = new Map();
  const suggestionProcessMap = new Map();
  const actionProcessMap = new Map();
  const pendingResumeRequests = [];
  const shownOverlayForSuggestion = new Set();

  function ensureProcessState(processId, identity = {}) {
    if (!processId) return null;
    let state = processState.get(processId);
    if (!state) {
      state = {
        processId,
        suggestionId: null,
        actionId: null,
        commandId: null,
        kind: null,
        status: 'idle',
        lastChunkIndex: -1,
        lastCursor: null,
        resumeSessionId: null,
        needsResume: false,
      };
      processState.set(processId, state);
    }
    applyProcessIdentity(state, identity);
    return state;
  }

  function registerProcessMapping(state) {
    if (!state) return;
    if (state.suggestionId) {
      suggestionProcessMap.set(String(state.suggestionId), state.processId);
    }
    if (state.actionId) {
      actionProcessMap.set(String(state.actionId), state.processId);
    }
  }

  function removeProcessState(processId, { force = false } = {}) {
    if (!processId) return false;
    const state = processState.get(processId);
    if (!state) return false;
    const isCompleted = state.status === 'completed' || state.status === 'idle';
    if (!force && !isCompleted) return false;
    processState.delete(processId);
    if (state.suggestionId) {
      const sid = String(state.suggestionId);
      suggestionProcessMap.delete(sid);
      shownOverlayForSuggestion.delete(sid);
    }
    if (state.actionId) {
      actionProcessMap.delete(String(state.actionId));
    }
    return true;
  }

  function clearRegistries({ force = false } = {}) {
    const processIds = Array.from(processState.keys());
    for (const pid of processIds) {
      removeProcessState(pid, { force });
    }
    if (force) {
      suggestionProcessMap.clear();
      actionProcessMap.clear();
      shownOverlayForSuggestion.clear();
      pendingResumeRequests.length = 0;
    }
  }

  function markProcessEvent(processId, eventId) {
    const state = ensureProcessState(processId);
    if (!state) return;
    if (eventId) state.lastCursor = eventId;
  }

  function markProcessStarted({ processId, suggestionId, actionId, commandId, kind, eventId }) {
    const state = ensureProcessState(processId, {
      kind,
      suggestionId,
      actionId,
      commandId,
    });
    if (!state) return;
    state.status = 'running';
    state.lastChunkIndex = -1;
    if (state.kind !== 'action' && eventId) state.lastCursor = eventId;
    state.needsResume = false;
    state.resumeSessionId = null;
    registerProcessMapping(state);
  }

  function markProcessChunk(processId, eventId) {
    const state = ensureProcessState(processId);
    if (!state) return;
    const currentIndex = typeof state.lastChunkIndex === 'number' ? state.lastChunkIndex : -1;
    state.lastChunkIndex = currentIndex + 1;
    if (eventId) state.lastCursor = eventId;
  }

  function markProcessCompleted(processId, eventId) {
    const state = ensureProcessState(processId);
    if (!state) return;
    state.status = 'completed';
    state.needsResume = false;
    state.resumeSessionId = null;
    if (eventId) state.lastCursor = eventId;
  }

  function maybeShowOverlayForSuggestion(suggestionId, content) {
    const sid = suggestionId ? String(suggestionId) : null;
    const normalizedContent = typeof content === 'string' ? content : '';
    if (!sid || normalizedContent.trim().length === 0 || typeof showNotification !== 'function') {
      return;
    }
    if (shownOverlayForSuggestion.has(sid)) return;
    shownOverlayForSuggestion.add(sid);
    try {
      showNotification(sid);
    } catch (_) {
      try { shownOverlayForSuggestion.delete(sid); } catch {}
    }
  }

  function hideSuggestionOverlay(suggestionId) {
    if (!suggestionId) return;
    try { shownOverlayForSuggestion.delete(String(suggestionId)); } catch {}
  }

  function flagRunningProcessesForResume(sessionId) {
    for (const state of processState.values()) {
      if (state.status === 'running') {
        state.needsResume = true;
        state.resumeSessionId = sessionId || state.resumeSessionId;
      }
    }
  }

  function requestResumeForState(state, { fromStart = false } = {}) {
    if (!state) return false;
    if (state.kind !== 'suggestion' && state.kind !== 'action') {
      state.needsResume = true;
      return false;
    }
    const sessionId = state.resumeSessionId || getCurrentSessionId();
    if (!sessionId) {
      state.needsResume = true;
      return false;
    }
    const lastChunkIndex = fromStart
      ? -1
      : (typeof state.lastChunkIndex === 'number' ? state.lastChunkIndex : -1);
    const payload = {
      event: InboundEvent.RESUME_SESSION,
      data: {
        session_id: sessionId,
        process_id: state.processId,
        last_cursor: state.lastCursor,
        last_chunk_index: lastChunkIndex,
        kind: state.kind,
      },
    };
    if (state.kind === 'action') {
      if (state.suggestionId) payload.data.suggestion_id = state.suggestionId;
      if (state.actionId) payload.data.action_id = state.actionId;
      if (state.commandId) payload.data.command_id = state.commandId;
    }
    if (fromStart) state.lastChunkIndex = -1;
    state.needsResume = false;
    state.resumeSessionId = sessionId;
    try {
      console.info('WS resume_session send', {
        sessionId,
        processId: state.processId,
        suggestionId: state.suggestionId,
        actionId: state.actionId,
        lastChunkIndex,
        fromStart,
      });
    } catch (_) {
      // noop logging guard
    }
    sendRaw(payload);
    forwardStatusToRenderers && forwardStatusToRenderers({
      status: 'resume_requested',
      process_id: state.processId,
      suggestion_id: state.suggestionId,
      action_id: state.actionId,
    });
    return true;
  }

  function performResume({ kind, processId, suggestionId, actionId, commandId, fromStart = false }) {
    let targetProcessId = processId || null;
    if (!targetProcessId && actionId) {
      targetProcessId = actionProcessMap.get(String(actionId)) || null;
    }
    if (!targetProcessId && suggestionId) {
      targetProcessId = suggestionProcessMap.get(String(suggestionId)) || null;
    }
    if (!targetProcessId) return false;
    const state = ensureProcessState(targetProcessId, {
      kind,
      suggestionId,
      actionId,
      commandId,
    });
    if (!state) return false;
    if (!state.resumeSessionId) {
      state.resumeSessionId = getCurrentSessionId() || state.resumeSessionId;
    }
    registerProcessMapping(state);
    return requestResumeForState(state, { fromStart });
  }

  function flushPendingResumeRequests() {
    if (!pendingResumeRequests.length) return;
    const remaining = [];
    for (const req of pendingResumeRequests) {
      const success = performResume(req);
      if (!success) remaining.push(req);
    }
    pendingResumeRequests.length = 0;
    pendingResumeRequests.push(...remaining);
  }

  function enqueueResumeRequest(args) {
    pendingResumeRequests.push(args);
  }

  function resumePendingProcesses() {
    for (const state of processState.values()) {
      if (state.status === 'running' && state.needsResume) {
        requestResumeForState(state);
      }
    }
  }

  return {
    clearRegistries,
    enqueueResumeRequest,
    flagRunningProcessesForResume,
    flushPendingResumeRequests,
    hideSuggestionOverlay,
    markProcessChunk,
    markProcessCompleted,
    markProcessEvent,
    markProcessStarted,
    maybeShowOverlayForSuggestion,
    performResume,
    removeProcessState,
    resumePendingProcesses,
  };
}

module.exports = { createProcessResumeState };
