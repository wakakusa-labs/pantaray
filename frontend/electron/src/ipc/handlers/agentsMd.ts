import type { MainContext } from '../context';
import type { IpcRegistrar } from '../registrar';
import { AgentsMdContentSchema } from '../schemas/agentsMd';
import { parseInput } from '../schemas/error';
import { runAuditedMutation } from './auditedMutation';

/** The renderer sends only the text; main owns the one path it is written to. */
export function registerAgentsMdHandlers(ctx: MainContext, registrar: IpcRegistrar): void {
  registrar.handle('agentsMd:read', () => ctx.agentsMd.read());
  registrar.handle('agentsMd:write', (_event, content) => {
    const parsed = parseInput(AgentsMdContentSchema, 'agentsMd:write', content);
    // Audited like the other settings that change what an Action does.
    return runAuditedMutation(ctx, 'agentsMd.write', () => ctx.agentsMd.write(parsed));
  });
}
