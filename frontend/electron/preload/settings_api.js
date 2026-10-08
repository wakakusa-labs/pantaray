function createSettingsApi({ ipcRenderer }) {
  return {
    aiConnection: {
      getState: () => ipcRenderer.invoke('aiConnection:getState'),
      update: (command) => ipcRenderer.invoke('aiConnection:update', command),
      onChanged: (callback) => {
        const listener = () => callback();
        ipcRenderer.on('aiConnection:changed', listener);
        return () => ipcRenderer.removeListener('aiConnection:changed', listener);
      },
    },
    approval: {
      getWorkspaceEditCommandPreference: () =>
        ipcRenderer.invoke('approval:getWorkspaceEditCommandPreference'),
      setWorkspaceEditCommandPreference: (approvalMode) =>
        ipcRenderer.invoke('approval:setWorkspaceEditCommandPreference', approvalMode),
    },
    shortcut: {
      getState: () => ipcRenderer.invoke('shortcut:getState'),
      setAccelerator: (accelerator) => ipcRenderer.invoke('shortcut:setAccelerator', accelerator),
    },
    overlayPlacement: {
      get: () => ipcRenderer.invoke('overlayPlacement:get'),
      set: (update) => ipcRenderer.invoke('overlayPlacement:set', update),
    },
    workspaceSettings: {
      get: () => ipcRenderer.invoke('workspaceSettings:get'),
      getReadAccessScope: () => ipcRenderer.invoke('workspaceSettings:getReadAccessScope'),
      getCommandNetwork: () => ipcRenderer.invoke('workspaceSettings:getCommandNetwork'),
      updateCommandNetwork: (enabled) =>
        ipcRenderer.invoke('workspaceSettings:updateCommandNetwork', enabled),
      createOrganization: (input) =>
        ipcRenderer.invoke('workspaceSettings:createOrganization', input),
      createProject: (input) => ipcRenderer.invoke('workspaceSettings:createProject', input),
      createFolder: (input) => ipcRenderer.invoke('workspaceSettings:createFolder', input),
      reorderProjects: (input) => ipcRenderer.invoke('workspaceSettings:reorderProjects', input),
      deleteOrganization: (organizationId) =>
        ipcRenderer.invoke('workspaceSettings:deleteOrganization', organizationId),
      deleteProject: (projectId) =>
        ipcRenderer.invoke('workspaceSettings:deleteProject', projectId),
      deleteFolder: (folderId) => ipcRenderer.invoke('workspaceSettings:deleteFolder', folderId),
      updateProjectLinks: (projectId, input) =>
        ipcRenderer.invoke('workspaceSettings:updateProjectLinks', projectId, input),
      updateFolderLinks: (folderId, input) =>
        ipcRenderer.invoke('workspaceSettings:updateFolderLinks', folderId, input),
      updateReadAccessScope: (readAccessScope) =>
        ipcRenderer.invoke('workspaceSettings:updateReadAccessScope', readAccessScope),
      selectFolder: () => ipcRenderer.invoke('workspaceSettings:selectFolder'),
    },
  };
}

module.exports = { createSettingsApi };
