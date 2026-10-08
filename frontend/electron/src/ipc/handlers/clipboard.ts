import type { MainContext } from '../context';
import type { IpcRegistrar } from '../registrar';
import { ClipboardWriteTextInputSchema } from '../schemas/clipboard';
import { parseInput } from '../schemas/error';

/**
 * The renderer's Clipboard API rejects a write from a document without focus, which an Overlay
 * panel clicked while another window is key is; main's clipboard has no such condition.
 */
export function registerClipboardHandlers(ctx: MainContext, registrar: IpcRegistrar): void {
  registrar.handle('clipboard:writeText', (_event, params) => {
    ctx.clipboard.writeText(
      parseInput(ClipboardWriteTextInputSchema, 'clipboard:writeText', params).text
    );
  });
}
