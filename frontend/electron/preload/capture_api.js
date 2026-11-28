function createCaptureApi({ ipcRenderer }) {
  return {
    screenshot: {
      getStatus: () => ipcRenderer.invoke('screenshot:getStatus'),
      start: () => ipcRenderer.invoke('screenshot:start'),
      stop: () => ipcRenderer.invoke('screenshot:stop'),
      getGateState: () => ipcRenderer.invoke('recording:getGateState'),
      dismissIntro: (ownerId) => ipcRenderer.invoke('recording:dismissIntro', ownerId),
      getPermissions: () => ipcRenderer.invoke('recording:getPermissions'),
      askForPermission: (permission) =>
        ipcRenderer.invoke('recording:askForPermission', permission),
      openPermissionSettings: (permission) =>
        ipcRenderer.invoke('recording:openPermissionSettings', permission),
      getPermissionRequired: () => ipcRenderer.invoke('recording:getPermissionRequired'),
      onPermissionRequiredChanged: (callback) => {
        const listener = (_event, isRequired) => callback(isRequired);
        ipcRenderer.on('recording:permissionRequiredChanged', listener);
        return () => ipcRenderer.removeListener('recording:permissionRequiredChanged', listener);
      },
      onStatusChanged: (callback) => {
        const listener = (_event, isEnabled) => callback(isEnabled);
        ipcRenderer.on('screenshot:statusChanged', listener);
        return () => ipcRenderer.removeListener('screenshot:statusChanged', listener);
      },
      // "Read the gate again now": main sends it when it brings this window
      // forward for the recording screen, which fires no focus event of its own.
      onGateStateChanged: (callback) => {
        const listener = () => callback();
        ipcRenderer.on('recording:gateStateChanged', listener);
        return () => ipcRenderer.removeListener('recording:gateStateChanged', listener);
      },
    },
    meeting: {
      getCall: () => ipcRenderer.invoke('meeting:getCall'),
      getAvailability: () => ipcRenderer.invoke('meeting:getAvailability'),
      getSettings: () => ipcRenderer.invoke('meeting:getSettings'),
      record: (callId) => ipcRenderer.invoke('meeting:record', callId),
      stop: (callId) => ipcRenderer.invoke('meeting:stop', callId),
      decline: (callId) => ipcRenderer.invoke('meeting:decline', callId),
      prepare: () => ipcRenderer.invoke('meeting:prepare'),
      updateSettings: (settings) => ipcRenderer.invoke('meeting:updateSettings', settings),
      onCallChanged: (callback) => {
        const listener = (_event, call) => callback(call);
        ipcRenderer.on('meeting:callChanged', listener);
        return () => ipcRenderer.removeListener('meeting:callChanged', listener);
      },
    },
    privacy: {
      getCaptureSettings: () => ipcRenderer.invoke('privacy:getCaptureSettings'),
      updateCaptureSettings: (settings) =>
        ipcRenderer.invoke('privacy:updateCaptureSettings', settings),
      listInstalledApps: () => ipcRenderer.invoke('privacy:listInstalledApps'),
      getIdeFileRules: () => ipcRenderer.invoke('privacy:getIdeFileRules'),
      updateIdeFileRules: (rules) => ipcRenderer.invoke('privacy:updateIdeFileRules', rules),
      setCaptureEditing: (request) => ipcRenderer.invoke('privacy:setCaptureEditing', request),
      onCaptureSettingsUpdated: (callback) => {
        const listener = (_event, settings) => callback(settings);
        ipcRenderer.on('privacy:captureSettingsUpdated', listener);
        return () => ipcRenderer.removeListener('privacy:captureSettingsUpdated', listener);
      },
    },
  };
}

module.exports = { createCaptureApi };
