// The single chat, for the main window: main refuses these channels from an Overlay.
function createChatApi({ ipcRenderer }) {
  return {
    chat: {
      sendMessage: (request) => ipcRenderer.invoke('chat:sendMessage', request),
      listItems: (request) => ipcRenderer.invoke('chat:listItems', request),
      retryTurn: (request) => ipcRenderer.invoke('chat:retryTurn', request),
      getTurnState: () => ipcRenderer.invoke('chat:getTurnState'),
      onItemAppended: (callback) => {
        const listener = (_event, item) => callback(item);
        ipcRenderer.on('chat:itemAppended', listener);
        return () => ipcRenderer.removeListener('chat:itemAppended', listener);
      },
      onTurnState: (callback) => {
        const listener = (_event, state) => callback(state);
        ipcRenderer.on('chat:turnState', listener);
        return () => ipcRenderer.removeListener('chat:turnState', listener);
      },
    },
  };
}

module.exports = { createChatApi };
