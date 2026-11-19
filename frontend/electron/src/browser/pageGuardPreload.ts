/**
 * The frame preload of every browser session, and the one list of page APIs the session takes
 * away. Blink has no switch for any of them:
 * - connections Electron never passes through `webRequest`: WebRTC (over TCP under any IP
 *   handling policy) and WebTransport (Electron does not intercept it);
 * - OS UI a click's user activation opens from the hidden window: the File System Access
 *   pickers (a same-origin sandbox allows them), the screen color sampler (`OpenEyeDropper`),
 *   the picture-in-picture window (`EnterPictureInPicture`), and Web Bluetooth, whose chooser
 *   picks the first device when nothing listens for `select-bluetooth-device`.
 *
 * As a preload it runs in Electron's isolated world while the page's own world is being set up,
 * before any page script, in every committed document of every frame, out-of-process frames
 * included (`nodeIntegrationInSubFrames`). `contextBridge.executeInMainWorld` compiles and runs
 * the removal in the page's world at once. It exposes nothing to the page. Electron gives no
 * preload to an `about:blank` frame, so the session also runs `removeGuardedApis` there through
 * CDP. Main imports this module for the list and the function only.
 */

import { contextBridge } from 'electron';

export const GUARDED_APIS: readonly (readonly [owner: string, name: string])[] = [
  ['globalThis', 'RTCPeerConnection'],
  ['globalThis', 'webkitRTCPeerConnection'],
  ['globalThis', 'WebTransport'],
  ['globalThis', 'showOpenFilePicker'],
  ['globalThis', 'showSaveFilePicker'],
  ['globalThis', 'showDirectoryPicker'],
  ['globalThis', 'EyeDropper'],
  ['HTMLVideoElement.prototype', 'requestPictureInPicture'],
  ['Navigator.prototype', 'bluetooth'],
];

/** Runs in the page's world, serialized, so it refers to nothing outside itself. */
export function removeGuardedApis(apis: readonly (readonly [string, string])[]): void {
  for (const [owner, name] of apis) {
    const target = owner
      .split('.')
      .reduce<
        Record<string, unknown>
      >((object, key) => object[key] as Record<string, unknown>, globalThis as unknown as Record<string, unknown>);
    delete target[name];
  }
}

if (process.type === 'renderer') {
  contextBridge.executeInMainWorld({ func: removeGuardedApis, args: [GUARDED_APIS] });
}
