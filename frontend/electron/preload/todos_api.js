// The TODO list and its progress, for the main window: main refuses these channels from an Overlay.
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
      progress: (query) => ipcRenderer.invoke('todos:progress', query),
      setGoal: (request) => ipcRenderer.invoke('todos:setGoal', request),
      clearGoal: (request) => ipcRenderer.invoke('todos:clearGoal', request),
      addProgressRecord: (request) => ipcRenderer.invoke('todos:addProgressRecord', request),
      deleteProgressRecord: (request) => ipcRenderer.invoke('todos:deleteProgressRecord', request),
      onChanged: (callback) => {
        const listener = (_event, change) => callback(change);
        ipcRenderer.on('todos:changed', listener);
        return () => ipcRenderer.removeListener('todos:changed', listener);
      },
    },
  };
}

module.exports = { createTodosApi };
