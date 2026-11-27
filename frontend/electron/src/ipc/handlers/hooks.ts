import type { MainContext } from '../context';
import type { IpcRegistrar } from '../registrar';
import { HookEnabledUpdateSchema } from '../../settings/hooksFetch';
import { parseInput } from '../schemas/error';

export function registerHookHandlers(ctx: MainContext, registrar: IpcRegistrar): void {
  registrar.handle('hooks:list', async () => ctx.hooks.list());
  registrar.handle('hooks:setEnabled', async (_event, update) =>
    ctx.hooks.setEnabled(parseInput(HookEnabledUpdateSchema, 'hooks:setEnabled', update))
  );
}
