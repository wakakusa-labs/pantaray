import { test, expect, type ElectronApplication } from '@playwright/test';
import type { BrowserWindowConstructorOptions } from 'electron';
import { launchElectronE2E } from './harness';
import { PAGE_VIEWPORT, pageWindowOptions } from '../../electron/src/browser/pageWindowOptions';

// The page's colors keep red equal to blue, so a sampled pixel reads the same whichever byte
// order the platform's bitmap uses (nativeImage.toBitmap is BGRA on some platforms).
const FIRST_COLOR = [40, 160, 40] as const;
const CLICKED_COLOR = [200, 40, 200] as const;
const COLOR_TOLERANCE = 8;
// A point of plain body background, away from the button and the input.
const SAMPLE_POINT = { x: 20, y: 700 } as const;
const BUTTON_CENTER = { x: 640, y: 400 } as const;
const TYPED_TEXT = 'typed while hidden';
const PNG_SIGNATURE = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);

const PAGE_HTML = `<!doctype html>
<html><head><meta charset="utf-8"><style>
body { margin: 0; background: rgb(${FIRST_COLOR.join(',')}); }
#paint { position: absolute; left: 540px; top: 350px; width: 200px; height: 100px; }
#field { position: absolute; left: 540px; top: 40px; width: 200px; }
</style></head><body>
<input id="field">
<button id="paint">Paint</button>
<script>
document.getElementById('paint').addEventListener('click', () => {
  document.body.style.background = 'rgb(${CLICKED_COLOR.join(',')})';
  document.body.dataset.painted = 'yes';
});
</script>
</body></html>`;
const PAGE_URL = `data:text/html;charset=utf-8,${encodeURIComponent(PAGE_HTML)}`;

function cdp<T>(
  app: ElectronApplication,
  windowId: number,
  method: string,
  params: object = {}
): Promise<T> {
  return app.evaluate(
    ({ BrowserWindow }, call) => {
      const win = BrowserWindow.fromId(call.windowId);
      if (win === null) throw new Error('The page window is gone');
      return win.webContents.debugger.sendCommand(call.method, call.params);
    },
    { windowId, method, params }
  ) as Promise<T>;
}

async function pageValue(
  app: ElectronApplication,
  windowId: number,
  expression: string
): Promise<unknown> {
  const { result } = await cdp<{ result: { value?: unknown } }>(app, windowId, 'Runtime.evaluate', {
    expression,
    returnByValue: true,
  });
  return result.value;
}

/** Captures the viewport and reads the PNG's size and the color at SAMPLE_POINT. */
async function capture(
  app: ElectronApplication,
  windowId: number
): Promise<{ width: number; height: number; pixel: number[] }> {
  const { data } = await cdp<{ data: string }>(app, windowId, 'Page.captureScreenshot', {
    format: 'png',
  });
  const png = Buffer.from(data, 'base64');
  expect(png.subarray(0, PNG_SIGNATURE.length).equals(PNG_SIGNATURE)).toBe(true);
  // IHDR is the first chunk: width and height are big-endian at bytes 16 and 20.
  const width = png.readUInt32BE(16);
  const height = png.readUInt32BE(20);
  const pixel = await app.evaluate(
    ({ nativeImage }, sample) => {
      const bitmap = nativeImage.createFromBuffer(Buffer.from(sample.data, 'base64')).toBitmap();
      const offset = (sample.y * sample.width + sample.x) * 4;
      return Array.from(bitmap.subarray(offset, offset + 4));
    },
    { data, width, ...SAMPLE_POINT }
  );
  return { width, height, pixel };
}

function matchesColor(pixel: number[], color: readonly number[]): boolean {
  return (
    color.every((channel, index) => Math.abs(pixel[index] - channel) <= COLOR_TOLERANCE) &&
    pixel[3] === 255
  );
}

// eslint-disable-next-line no-empty-pattern
test('a hidden page window is driven and captured through CDP while the app is hidden', async ({}, testInfo) => {
  const { harness, stop } = await launchElectronE2E();
  const { app } = harness;
  let windowId: number | null = null;
  try {
    await test.step('hide every app window and the app', async () => {
      const visibleWindows = await app.evaluate(({ app: electronApp, BrowserWindow }) => {
        for (const win of BrowserWindow.getAllWindows()) win.hide();
        electronApp.hide();
        return BrowserWindow.getAllWindows().filter((win) => win.isVisible()).length;
      });
      expect(visibleWindows).toBe(0);
    });

    const id =
      await test.step('open the page in a window built from pageWindowOptions', async () => {
        const opened = await app.evaluate(
          async ({ BrowserWindow }, open) => {
            // Playwright's argument typing unwraps class types; the value is the plain options object.
            const win = new BrowserWindow(open.options as BrowserWindowConstructorOptions);
            await win.webContents.loadURL(open.url);
            win.webContents.debugger.attach('1.3');
            return win.id;
          },
          { options: pageWindowOptions('browser-hidden-page-e2e'), url: PAGE_URL }
        );
        windowId = opened;
        expect(await pageValue(app, opened, '[innerWidth, innerHeight]')).toEqual([
          PAGE_VIEWPORT.width,
          PAGE_VIEWPORT.height,
        ]);
        return opened;
      });

    const first = await test.step('(a) a capture is a viewport-sized PNG of the page', async () => {
      // Pin the device scale so the image is the viewport's size on any display.
      await cdp(app, id, 'Emulation.setDeviceMetricsOverride', {
        width: PAGE_VIEWPORT.width,
        height: PAGE_VIEWPORT.height,
        deviceScaleFactor: 1,
        mobile: false,
      });
      const shot = await capture(app, id);
      expect([shot.width, shot.height]).toEqual([PAGE_VIEWPORT.width, PAGE_VIEWPORT.height]);
      expect(matchesColor(shot.pixel, FIRST_COLOR), `pixel ${shot.pixel.join(',')}`).toBe(true);
      return shot;
    });

    const clicked = await test.step('(b) a CDP click repaints the hidden page', async () => {
      for (const type of ['mouseMoved', 'mousePressed', 'mouseReleased']) {
        await cdp(app, id, 'Input.dispatchMouseEvent', {
          type,
          ...BUTTON_CENTER,
          button: type === 'mouseMoved' ? 'none' : 'left',
          clickCount: type === 'mouseMoved' ? 0 : 1,
        });
      }
      expect(await pageValue(app, id, 'document.body.dataset.painted')).toBe('yes');
      await expect
        .poll(async () => matchesColor((await capture(app, id)).pixel, CLICKED_COLOR), {
          timeout: 10_000,
        })
        .toBe(true);
      return capture(app, id);
    });

    await test.step('(c) focus emulation lets Input.insertText fill a focused input', async () => {
      await cdp(app, id, 'Emulation.setFocusEmulationEnabled', { enabled: true });
      const { root } = await cdp<{ root: { nodeId: number } }>(app, id, 'DOM.getDocument', {
        depth: 0,
      });
      const { nodeId } = await cdp<{ nodeId: number }>(app, id, 'DOM.querySelector', {
        nodeId: root.nodeId,
        selector: '#field',
      });
      await cdp(app, id, 'DOM.focus', { nodeId });
      await cdp(app, id, 'Input.insertText', { text: TYPED_TEXT });
      expect(await pageValue(app, id, 'document.activeElement.id')).toBe('field');
      expect(await pageValue(app, id, "document.getElementById('field').value")).toBe(TYPED_TEXT);
    });

    const pdfBytes = await test.step('(d) printToPDF prints the same window', async () => {
      const pdf = Buffer.from(
        await app.evaluate(async ({ BrowserWindow }, pageWindowId) => {
          const win = BrowserWindow.fromId(pageWindowId);
          if (win === null) throw new Error('The page window is gone');
          return (await win.webContents.printToPDF({})).toString('base64');
        }, id),
        'base64'
      );
      expect(pdf.subarray(0, 5).toString('latin1')).toBe('%PDF-');
      return pdf.length;
    });

    await test.step('nothing became visible or focused', async () => {
      const state = await app.evaluate(({ BrowserWindow }, pageWindowId) => {
        const win = BrowserWindow.fromId(pageWindowId);
        if (win === null) throw new Error('The page window is gone');
        return {
          pageVisible: win.isVisible(),
          pageFocused: win.isFocused(),
          offscreen: win.webContents.isOffscreen(),
          visibleWindows: BrowserWindow.getAllWindows().filter((other) => other.isVisible()).length,
        };
      }, id);
      expect(state).toMatchObject({ pageVisible: false, pageFocused: false, visibleWindows: 0 });
      // The PR records this line as the evidence for the chosen window mode.
      console.log(
        `[browser-hidden-page] offscreen=${state.offscreen} capture=${first.width}x${first.height} ` +
          `first=${first.pixel.join(',')} clicked=${clicked.pixel.join(',')} pdf_bytes=${pdfBytes}`
      );
    });
  } finally {
    try {
      if (windowId !== null) {
        await app.evaluate(({ BrowserWindow }, pageWindowId) => {
          BrowserWindow.fromId(pageWindowId)?.destroy();
        }, windowId);
      }
    } finally {
      await stop({ keepArtifacts: testInfo.status !== testInfo.expectedStatus });
    }
  }
});
