function createAuthHistoryApi({ ipcRenderer }) {
  // Main may ask to show a History item before the page's shell subscribes (it mounts after the
  // auth state is read), so the latest unanswered request waits for that subscriber.
  let showItemSubscriber = null;
  let pendingShowItem = null;
  ipcRenderer.on('history:showItem', (_event, payload) => {
    if (showItemSubscriber) showItemSubscriber(payload);
    else pendingShowItem = payload;
  });
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
      onShowItem: (callback) => {
        showItemSubscriber = callback;
        if (pendingShowItem !== null) {
          const payload = pendingShowItem;
          pendingShowItem = null;
          callback(payload);
        }
        return () => {
          if (showItemSubscriber === callback) showItemSubscriber = null;
        };
      },
    },
    actionFiles: {
      open: (params) => ipcRenderer.invoke('actionFile:open', params),
      read: (params) => ipcRenderer.invoke('actionFile:read', params),
      openInApp: (params) => ipcRenderer.invoke('actionFile:openInApp', params),
      openWithApp: (params) => ipcRenderer.invoke('actionFile:openWithApp', params),
      reveal: (params) => ipcRenderer.invoke('actionFile:reveal', params),
      quickLook: (params) => ipcRenderer.invoke('actionFile:quickLook', params),
    },
  };
}

module.exports = { createAuthHistoryApi };
