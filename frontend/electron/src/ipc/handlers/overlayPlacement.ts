import type { MainContext } from '../context';
import type { IpcRegistrar } from '../registrar';
import { parseInput } from '../schemas/error';
import { OverlayPlacementUpdateSchema } from '../schemas/overlayPlacement';

export function registerOverlayPlacementHandlers(ctx: MainContext, registrar: IpcRegistrar): void {
  registrar.handle('overlayPlacement:get', () => ctx.overlayPlacement.get());
  registrar.handle('overlayPlacement:set', (_event, update) =>
    ctx.overlayPlacement.set(
      parseInput(OverlayPlacementUpdateSchema, 'overlayPlacement:set', update)
    )
  );
}
