import type { MainContext } from '../context';
import type { IpcRegistrar } from '../registrar';
import { AgentsMdWriteRequestSchema } from '../schemas/agentsMd';
import { parseInput } from '../schemas/error';
import { runAuditedMutation } from './auditedMutation';

/** The renderer sends only the text and its revision; main owns the one path it is written to. */
export function registerAgentsMdHandlers(ctx: MainContext, registrar: IpcRegistrar): void {
  registrar.handle('agentsMd:read', () => ctx.agentsMd.read());
  registrar.handle('agentsMd:write', (_event, request) => {
    const parsed = parseInput(AgentsMdWriteRequestSchema, 'agentsMd:write', request);
    // Audited like the other settings that change what an Action does.
    return runAuditedMutation(
      ctx,
      'agentsMd.write',
      () => ctx.agentsMd.write(parsed),
      (result) => result.kind === 'conflict'
    );
  });
}
