import type { MainContext } from '../context';
import type { IpcRegistrar } from '../registrar';
import {
  IntegrationAccessUpdateSchema,
  IntegrationAccountRefSchema,
  IntegrationToolBlockUpdateSchema,
} from '../../settings/integrationSettingsFetch';
import { parseInput } from '../schemas/error';

export function registerIntegrationHandlers(ctx: MainContext, registrar: IpcRegistrar): void {
  registrar.handle('integrations:getStatus', async () => ctx.integrations.getStatus());
  registrar.handle('integrations:setAccountAccess', async (_event, update) =>
    ctx.integrations.setAccountAccess(
      parseInput(IntegrationAccessUpdateSchema, 'integrations:setAccountAccess', update)
    )
  );
  registrar.handle('integrations:listAccountTools', async (_event, ref) =>
    ctx.integrations.listAccountTools(
      parseInput(IntegrationAccountRefSchema, 'integrations:listAccountTools', ref)
    )
  );
  registrar.handle('integrations:setToolBlocked', async (_event, update) =>
    ctx.integrations.setToolBlocked(
      parseInput(IntegrationToolBlockUpdateSchema, 'integrations:setToolBlocked', update)
    )
  );
}
