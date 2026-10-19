import type { MainContext } from '../context';
import type { IpcRegistrar } from '../registrar';
import {
  IntegrationAccessUpdateSchema,
  IntegrationAccountRefSchema,
  IntegrationConnectionRefSchema,
  IntegrationToolBlockUpdateSchema,
  IntegrationToolkitRefSchema,
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
  registrar.handle('integrations:connect', async (_event, ref) =>
    ctx.integrations.connect(parseInput(IntegrationToolkitRefSchema, 'integrations:connect', ref))
  );
  registrar.handle('integrations:reconnect', async (_event, ref) =>
    ctx.integrations.reconnect(
      parseInput(IntegrationAccountRefSchema, 'integrations:reconnect', ref)
    )
  );
  registrar.handle('integrations:disconnect', async (_event, ref) =>
    ctx.integrations.disconnect(
      parseInput(IntegrationAccountRefSchema, 'integrations:disconnect', ref)
    )
  );
  registrar.handle('integrations:getConnectionState', async (_event, ref) =>
    ctx.integrations.getConnectionState(
      parseInput(IntegrationConnectionRefSchema, 'integrations:getConnectionState', ref)
    )
  );
}
