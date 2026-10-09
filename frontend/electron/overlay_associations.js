function normalizeId(value) {
  if (!value && value !== 0) return null;
  const str = String(value).trim();
  return str.length ? str : null;
}

// Which Overlay window a process or Action event belongs to. The window registry stays with
// notification_window.js; this module only reads it to route events and to protect an open
// window's binding.
function createOverlayAssociations({ overlayWindows, hasOverlayWindow, sendToOverlay }) {
  const processToSuggestion = new Map(); // processId -> suggestionId
  const actionToOverlayId = new Map(); // actionId -> Overlay id

  function registerProcessAssociation(processId, suggestionId) {
    const pid = normalizeId(processId);
    const sid = normalizeId(suggestionId);
    if (!pid || !sid) return;
    processToSuggestion.set(pid, sid);
  }

  // An explicit open or send binds the Action to the window the user is looking at.
  function registerActionAssociation(actionId, overlayId) {
    const aid = normalizeId(actionId);
    const oid = normalizeId(overlayId);
    if (!aid || !oid) return;
    actionToOverlayId.set(aid, oid);
  }

  // A server event or Suggestion snapshot names the Action's Suggestion, which is not necessarily
  // the window showing it: a conversation opened from History is a different window, and every run
  // of the Action, follow-ups and replays included, still carries the Suggestion id. So these binds
  // take the Action only from no window, never from one that is open.
  function adoptActionAssociation(actionId, overlayId) {
    const aid = normalizeId(actionId);
    const oid = normalizeId(overlayId);
    if (!aid || !oid) return;
    const current = actionToOverlayId.get(aid);
    if (current && current !== oid && hasOverlayWindow(current)) return;
    actionToOverlayId.set(aid, oid);
  }

  function cleanupMappingsForSuggestion(suggestionId) {
    const sid = normalizeId(suggestionId);
    if (!sid) return;
    for (const [pid, mappedSid] of [...processToSuggestion.entries()]) {
      if (mappedSid === sid) {
        processToSuggestion.delete(pid);
      }
    }
    for (const [aid, mappedOverlayId] of [...actionToOverlayId.entries()]) {
      if (mappedOverlayId === sid) {
        actionToOverlayId.delete(aid);
      }
    }
  }

  function cleanupMappingsForProcess(processId) {
    const pid = normalizeId(processId);
    if (pid) {
      processToSuggestion.delete(pid);
    }
  }

  // A finished Action stays bound to a window that still shows it: a follow-up must reach that
  // window, and opening the Action again must find it instead of opening a second one. Closing
  // the window releases it (cleanupMappingsForSuggestion); only a binding to no open window goes.
  function releaseActionAssociationWithoutWindow(actionId) {
    const aid = normalizeId(actionId);
    if (aid && !hasOverlayWindow(actionToOverlayId.get(aid))) actionToOverlayId.delete(aid);
  }

  function clearActionAssociations() {
    actionToOverlayId.clear();
  }

  function clearAllAssociations() {
    processToSuggestion.clear();
    actionToOverlayId.clear();
  }

  function resolveOverlayId({ suggestionId, processId, actionId }) {
    const sid = normalizeId(suggestionId);
    const pid = normalizeId(processId);
    const aid = normalizeId(actionId);

    if (sid && overlayWindows.has(sid)) {
      return sid;
    }
    if (aid && actionToOverlayId.has(aid)) {
      return actionToOverlayId.get(aid);
    }
    if (pid && processToSuggestion.has(pid)) {
      return processToSuggestion.get(pid);
    }
    return null;
  }

  function dispatchEventToOverlay(channel, payload) {
    if (!payload || typeof payload !== 'object') return false;
    const meta = payload.meta || {};
    const data = payload.data || {};
    const targetId = resolveOverlayId({
      suggestionId: meta.suggestion_id || data.suggestion_id,
      processId: meta.process_id || data.process_id,
      actionId: meta.action_id || data.action_id,
    });
    if (!targetId) return false;
    sendToOverlay(targetId, channel, payload);
    return true;
  }

  return {
    registerProcessAssociation,
    registerActionAssociation,
    adoptActionAssociation,
    cleanupMappingsForSuggestion,
    cleanupMappingsForProcess,
    releaseActionAssociationWithoutWindow,
    clearActionAssociations,
    clearAllAssociations,
    resolveOverlayId,
    dispatchEventToOverlay,
  };
}

module.exports = { normalizeId, createOverlayAssociations };
