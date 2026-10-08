// The single chat, for the main window: main refuses these channels from an Overlay.
function createChatApi({ ipcRenderer }) {
  return {
    chat: {
      sendMessage: (request) => ipcRenderer.invoke('chat:sendMessage', request),
      listItems: (request) => ipcRenderer.invoke('chat:listItems', request),
      onItemAppended: (callback) => {
        const listener = (_event, item) => callback(item);
        ipcRenderer.on('chat:itemAppended', listener);
        return () => ipcRenderer.removeListener('chat:itemAppended', listener);
      },
    },
  };
}

module.exports = { createChatApi };
