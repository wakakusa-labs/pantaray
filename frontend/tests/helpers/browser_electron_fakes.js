/** A fake Electron for the browser engine: sessions by partition, hidden windows, CDP. */
const { EventEmitter } = require('node:events');

const PDF = Buffer.from('%PDF-1.7 printed');

/** A PNG header of the given size; the code under test reads only the signature and IHDR. */
function png(width, height) {
  const size = Buffer.alloc(8);
  size.writeUInt32BE(width, 0);
  size.writeUInt32BE(height, 4);
  return Buffer.concat([
    Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 0, 0, 0, 13]),
    Buffer.from('IHDR'),
    size,
    Buffer.alloc(16),
  ]);
}

/** Fake Electron: one session per partition, windows whose page `load` and `cdp` script. */
function fakeBrowserElectron({ load = async () => {}, cdp = async () => ({}) } = {}) {
  const partitions = new Map();
  const windows = [];
  function fromPartition(name) {
    if (partitions.has(name)) return partitions.get(name);
    const ses = new EventEmitter();
    Object.assign(ses, {
      proxies: [],
      closedConnections: 0,
      cleared: [],
      protocol: {
        handle: (_scheme, handler) => (ses.fileHandler = handler),
        unhandle: (scheme) => (ses.unhandled = scheme),
      },
      webRequest: {
        onBeforeRequest: (listener) => (ses.beforeRequest = listener),
        onHeadersReceived: (listener) => (ses.headersReceived = listener),
      },
      registerPreloadScript: () => undefined,
      setPermissionRequestHandler: (handler) => (ses.permissionRequest = handler),
      setPermissionCheckHandler: (handler) => (ses.permissionCheck = handler),
      setDevicePermissionHandler: (handler) => (ses.devicePermission = handler),
      setProxy: async (config) => ses.proxies.push(config),
      closeAllConnections: async () => (ses.closedConnections += 1),
      clearStorageData: async () => ses.cleared.push('storage'),
      clearCache: async () => ses.cleared.push('cache'),
    });
    partitions.set(name, ses);
    return ses;
  }
  class BrowserWindow extends EventEmitter {
    static getAllWindows() {
      return windows.filter((window) => !window.destroyed);
    }

    constructor(options) {
      super();
      this.options = options;
      this.destroyed = false;
      this.shown = false;
      const history = [];
      const contents = new EventEmitter();
      const debuggerApi = new EventEmitter();
      Object.assign(debuggerApi, {
        commands: [],
        attach: (version) => (debuggerApi.attached = version),
        sendCommand: async (method, params, sessionId) => {
          debuggerApi.commands.push({ method, params, sessionId });
          return cdp(method, params, { contents, debuggerApi });
        },
      });
      const ses = fromPartition(options.webPreferences.partition);
      Object.assign(contents, {
        debugger: debuggerApi,
        history,
        getURL: () => history.at(-1) ?? '',
        getTitle: () => `title of ${history.at(-1)}`,
        // The session loads about:blank before it attaches CDP; the page's history starts after.
        loadURL: async (url) => {
          if (url === 'about:blank') return;
          await load(url, { ses, contents });
          history.push(url);
        },
        isLoading: () => false,
        printToPDF: async (printOptions) => {
          contents.printOptions = printOptions;
          return PDF;
        },
        navigationHistory: {
          canGoBack: () => history.length > 1,
          goBack: () => {
            history.pop();
            setImmediate(() => contents.emit('did-navigate'));
          },
        },
        setWindowOpenHandler: (handler) => (contents.openHandler = handler),
        setWebRTCIPHandlingPolicy: (policy) => (contents.webRtcPolicy = policy),
      });
      this.webContents = contents;
      windows.push(this);
    }

    show() {
      this.shown = true;
    }

    focus() {
      this.shown = true;
    }

    isDestroyed() {
      return this.destroyed;
    }

    destroy() {
      this.destroyed = true;
      this.emit('closed');
    }
  }
  return { partitions, windows, electron: { BrowserWindow, session: { fromPartition } } };
}

module.exports = { PDF, png, fakeBrowserElectron };
