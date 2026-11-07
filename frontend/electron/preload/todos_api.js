// The TODO list, for the main window: main refuses these channels from an Overlay.
function createTodosApi({ ipcRenderer }) {
  return {
    todos: {
      listWeek: (query) => ipcRenderer.invoke('todos:listWeek', query),
      create: (request) => ipcRenderer.invoke('todos:create', request),
      setCheck: (request) => ipcRenderer.invoke('todos:setCheck', request),
      setDue: (request) => ipcRenderer.invoke('todos:setDue', request),
      delete: (request) => ipcRenderer.invoke('todos:delete', request),
      answerDeletion: (request) => ipcRenderer.invoke('todos:answerDeletion', request),
      restore: (request) => ipcRenderer.invoke('todos:restore', request),
      listDeleted: (query) => ipcRenderer.invoke('todos:listDeleted', query),
      onChanged: (callback) => {
        const listener = (_event, change) => callback(change);
        ipcRenderer.on('todos:changed', listener);
        return () => ipcRenderer.removeListener('todos:changed', listener);
      },
    },
  };
}

module.exports = { createTodosApi };
