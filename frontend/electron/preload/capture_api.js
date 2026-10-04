function createCaptureApi({ ipcRenderer }) {
  return {
    screenshot: {
      getStatus: () => ipcRenderer.invoke('screenshot:getStatus'),
      start: () => ipcRenderer.invoke('screenshot:start'),
      stop: () => ipcRenderer.invoke('screenshot:stop'),
      getGateState: () => ipcRenderer.invoke('recording:getGateState'),
      dismissIntro: (ownerId) => ipcRenderer.invoke('recording:dismissIntro', ownerId),
      openPermissionSettings: () => ipcRenderer.invoke('recording:openPermissionSettings'),
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
    privacy: {
      getCaptureSettings: () => ipcRenderer.invoke('privacy:getCaptureSettings'),
      updateCaptureSettings: (settings) =>
        ipcRenderer.invoke('privacy:updateCaptureSettings', settings),
      listInstalledApps: () => ipcRenderer.invoke('privacy:listInstalledApps'),
      getIdeFileRules: () => ipcRenderer.invoke('privacy:getIdeFileRules'),
      updateIdeFileRules: (rules) => ipcRenderer.invoke('privacy:updateIdeFileRules', rules),
      setCaptureEditing: (request) =>
        ipcRenderer.invoke('privacy:setCaptureEditing', request),
      onCaptureSettingsUpdated: (callback) => {
        const listener = (_event, settings) => callback(settings);
        ipcRenderer.on('privacy:captureSettingsUpdated', listener);
        return () => ipcRenderer.removeListener('privacy:captureSettingsUpdated', listener);
      },
    },
  };
}

module.exports = { createCaptureApi };
