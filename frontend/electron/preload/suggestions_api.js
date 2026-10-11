// A suggestion read into main's record, for the main window: main refuses these from an Overlay.
function createSuggestionsApi({ ipcRenderer }) {
  return {
    suggestions: {
      read: (request) => ipcRenderer.invoke('suggestion:read', request),
      onSnapshot: (callback) => {
        const listener = (_event, payload) => callback(payload);
        ipcRenderer.on('suggestion:snapshot', listener);
        return () => ipcRenderer.removeListener('suggestion:snapshot', listener);
      },
    },
  };
}

module.exports = { createSuggestionsApi };
