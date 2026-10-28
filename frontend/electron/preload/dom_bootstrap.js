/* Glass fields belong to the transparent overlay (notification.html); the main window's fields
   use their own styles. The overlay's own transparency is in index.css (body.overlay-window). */
const OVERLAY_FIELD_STYLES = `
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
    const style = documentRef.createElement('style');
    style.textContent = OVERLAY_FIELD_STYLES;
    documentRef.head.appendChild(style);
  });

  windowRef.addEventListener('error', (event) => {
    logError('renderer global error', event.error, { intervalMs: 0 });
  });
}

module.exports = { installDomBootstrap };
