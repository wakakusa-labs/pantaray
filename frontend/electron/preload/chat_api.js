// The single chat, for the main window: main refuses these channels from an Overlay.
function createChatApi({ ipcRenderer }) {
  // Main sends the turn's state only when it changes, so a History page mounted mid-turn gets
  // the latest one on subscribe. The next WS session sends it again.
  let latestTurnState = null;
  const turnStateSubscribers = new Set();
  ipcRenderer.on('chat:turnState', (_event, state) => {
    latestTurnState = state;
    turnStateSubscribers.forEach((subscriber) => subscriber(state));
  });

  return {
    chat: {
      sendMessage: (request) => ipcRenderer.invoke('chat:sendMessage', request),
      listItems: (request) => ipcRenderer.invoke('chat:listItems', request),
      retryTurn: (request) => ipcRenderer.invoke('chat:retryTurn', request),
      onItemAppended: (callback) => {
        const listener = (_event, item) => callback(item);
        ipcRenderer.on('chat:itemAppended', listener);
        return () => ipcRenderer.removeListener('chat:itemAppended', listener);
      },
      onTurnState: (callback) => {
        turnStateSubscribers.add(callback);
        if (latestTurnState !== null) callback(latestTurnState);
        return () => turnStateSubscribers.delete(callback);
      },
    },
  };
}

module.exports = { createChatApi };
