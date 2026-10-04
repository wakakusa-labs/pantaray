const TRANSPARENT_APP_STYLES = `
  html, body, #root {
    background-color: transparent !important;
  }

  /* Glass fields belong to the transparent overlay (notification.html); the main window's
     fields use their own styles. */
  body.overlay-window input,
  body.overlay-window select {
    background-color: rgba(255, 255, 255, 0.2) !important;
    border: 1px solid rgba(255, 255, 255, 0.3) !important;
    color: #ffffff !important;
    backdrop-filter: blur(5px);
    -webkit-backdrop-filter: blur(5px);
  }

  body.overlay-window input::placeholder {
    color: rgba(255, 255, 255, 0.6) !important;
  }
`;

function installDomBootstrap({ windowRef, documentRef, logError }) {
  windowRef.addEventListener('DOMContentLoaded', () => {
    documentRef.documentElement.style.backgroundColor = 'transparent';
    documentRef.body.style.backgroundColor = 'transparent';
    const root = documentRef.getElementById('root');
    if (root) {
      root.classList.add('electron-app');
      root.style.backgroundColor = 'transparent';
    }
    const style = documentRef.createElement('style');
    style.textContent = TRANSPARENT_APP_STYLES;
    documentRef.head.appendChild(style);
  });

  windowRef.addEventListener('error', (event) => {
    logError('renderer global error', event.error, { intervalMs: 0 });
  });
}

module.exports = { installDomBootstrap };
