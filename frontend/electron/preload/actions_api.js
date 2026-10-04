// A fast run can finish before its Overlay mounts and subscribes, so the latest few finished
// Actions stay for that late subscriber. Older ones are dropped so a main window that stays open
// for days does not keep the page of every conversation it saw finish.
const RETAINED_TERMINAL_UPDATES = 20;

// Mirrors the renderer: the lifecycle event wins until the canonical page read replaces it.
function isTerminalSnapshot(snapshot) {
  if (snapshot.lifecycle) return snapshot.lifecycle.status !== 'processing';
  const status = snapshot.page?.action.status;
  return status !== undefined && status !== 'queued' && status !== 'processing';
}

function createActionsApi({ ipcRenderer }) {
  // The main window receives every Action's updates and the History list needs each running one,
  // so a late subscriber gets the latest update of every running Action, oldest change first.
  const latestUpdates = new Map();
  let pendingReset = false;
  const subscribers = new Set();

  ipcRenderer.on('action:conversationUpdated', (_event, update) => {
    if (update?.kind === 'reset') {
      latestUpdates.clear();
      pendingReset = subscribers.size === 0;
    } else if (update?.kind === 'action_updated' && update.snapshot?.actionId) {
      latestUpdates.delete(update.snapshot.actionId);
      latestUpdates.set(update.snapshot.actionId, update);
      const terminalActionIds = [...latestUpdates]
        .filter(([, retained]) => isTerminalSnapshot(retained.snapshot))
        .map(([actionId]) => actionId);
      terminalActionIds
        .slice(0, Math.max(0, terminalActionIds.length - RETAINED_TERMINAL_UPDATES))
        .forEach((actionId) => latestUpdates.delete(actionId));
    } else {
      return;
    }
    subscribers.forEach((subscriber) => subscriber(update));
  });

  return {
    actions: {
      submitMessage: (request) => ipcRenderer.invoke('action:submitMessage', request),
      resumeAction: (request) => ipcRenderer.invoke('action:resume', request),
      attachImage: (request) => ipcRenderer.invoke('action:attachImage', request),
      revealImage: (request) => ipcRenderer.invoke('actionImage:reveal', request),
      attachFile: (request) => ipcRenderer.invoke('action:attachFile', request),
      discardAttachment: (request) => ipcRenderer.invoke('action:discardAttachment', request),
      readConversationPage: (request) => ipcRenderer.invoke('action:readConversationPage', request),
      readToolOutputPage: (request) => ipcRenderer.invoke('action:readToolOutputPage', request),
      onConversationUpdated: (callback) => {
        subscribers.add(callback);
        if (pendingReset) {
          pendingReset = false;
          callback({ kind: 'reset' });
        }
        latestUpdates.forEach((update) => callback(update));
        return () => subscribers.delete(callback);
      },
    },
  };
}

module.exports = { createActionsApi };
