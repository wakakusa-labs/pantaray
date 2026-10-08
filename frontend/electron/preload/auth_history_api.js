function createAuthHistoryApi({ ipcRenderer }) {
  return {
    auth: {
      confirmationComplete: () => ipcRenderer.invoke('auth:confirmationComplete'),
      saveLoginState: (isLoggedIn) => ipcRenderer.invoke('auth:saveLoginState', isLoggedIn),
      getState: () => ipcRenderer.invoke('auth:getState'),
      signOut: () => ipcRenderer.invoke('auth:signOut'),
      startBrowserLogin: (route) => ipcRenderer.invoke('auth:startBrowserLogin', route),
      onStateChanged: (callback) => {
        const listener = (_event, payload) => callback(payload);
        ipcRenderer.on('auth:stateChanged', listener);
        return () => ipcRenderer.removeListener('auth:stateChanged', listener);
      },
    },
    history: {
      fetch: (params) => ipcRenderer.invoke('history:fetch', params),
      markCompletionViewed: (request) =>
        ipcRenderer.invoke('history:markCompletionViewed', request),
      deleteItem: (request) => ipcRenderer.invoke('history:deleteItem', request),
      openNewConversation: () => ipcRenderer.invoke('history:openNewConversation'),
      openConversation: (request) => ipcRenderer.invoke('history:openConversation', request),
      onChanged: (callback) => {
        const listener = (_event, payload) => callback(payload);
        ipcRenderer.on('history:changed', listener);
        return () => ipcRenderer.removeListener('history:changed', listener);
      },
      onShowChat: (callback) => {
        const listener = (_event, payload) => callback(payload);
        ipcRenderer.on('history:showChat', listener);
        return () => ipcRenderer.removeListener('history:showChat', listener);
      },
    },
    actionFiles: {
      open: (params) => ipcRenderer.invoke('actionFile:open', params),
    },
  };
}

module.exports = { createAuthHistoryApi };
