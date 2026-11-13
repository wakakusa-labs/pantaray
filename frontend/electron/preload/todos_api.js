// The TODO list, its progress and its imports, for the main window: main refuses these channels
// from an Overlay.
function createTodosApi({ ipcRenderer }) {
  return {
    todos: {
      list: (query) => ipcRenderer.invoke('todos:list', query),
      create: (request) => ipcRenderer.invoke('todos:create', request),
      setCheck: (request) => ipcRenderer.invoke('todos:setCheck', request),
      setDue: (request) => ipcRenderer.invoke('todos:setDue', request),
      setStart: (request) => ipcRenderer.invoke('todos:setStart', request),
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
    todoImports: {
      start: (request) => ipcRenderer.invoke('todoImports:start', request),
      listOpen: (query) => ipcRenderer.invoke('todoImports:listOpen', query),
      read: (request) => ipcRenderer.invoke('todoImports:read', request),
      write: (request) => ipcRenderer.invoke('todoImports:write', request),
      register: (request) => ipcRenderer.invoke('todoImports:register', request),
      cancel: (request) => ipcRenderer.invoke('todoImports:cancel', request),
    },
  };
}

module.exports = { createTodosApi };
