import { CredentialStorageError } from '../../auth/encryptedJsonFile';
import { ConnectionSettingsReadError } from '../../aiConnection/localConnectionRuntime';
import { EMPTY_CONNECTION_PREFERENCES } from '../../aiConnection/preferences';
import type { MainContext } from '../context';
import type { IpcRegistrar } from '../registrar';
import {
  ConnectionCommandSchema,
  type ConnectionCommand,
  type ConnectionFailure,
  type ConnectionStateResult,
  type ConnectionRuntimeResult,
  type ConnectionRecoveryCommand,
  type ConnectionUpdateResult,
} from '../schemas/aiConnection';
import { runAuditedMutation } from './auditedMutation';

function recovery(target: ConnectionSettingsReadError['target']): ConnectionRecoveryCommand {
  switch (target) {
    case 'chatgpt':
      return { operation: 'disconnect_chatgpt' };
    case 'preferences':
      return { operation: 'save_preferences', preferences: EMPTY_CONNECTION_PREFERENCES };
    case 'tavily':
      return { operation: 'remove_web_search_key' };
    case 'composio':
      return { operation: 'remove_integration_key' };
    default:
      return { operation: 'remove_api_key', provider: target };
  }
}

function failure(error: unknown): ConnectionFailure {
  return {
    ok: false,
    // Never forward native, provider, or transport error text over IPC.
    error: error instanceof CredentialStorageError ? error.code : 'runtime_unavailable',
  };
}

async function apply(
  ctx: MainContext,
  command: ConnectionCommand
): Promise<ConnectionUpdateResult> {
  const connection = ctx.aiConnection;
  switch (command.operation) {
    case 'save_preferences':
      await connection.savePreferences(command.preferences);
      break;
    case 'save_api_key':
      await connection.saveApiKey(command.provider, command.apiKey);
      break;
    case 'remove_api_key':
      await connection.removeApiKey(command.provider);
      break;
    case 'save_web_search_key':
      await connection.saveWebSearchKey(command.apiKey);
      break;
    case 'remove_web_search_key':
      await connection.removeWebSearchKey();
      break;
    case 'save_integration_key':
      await connection.saveIntegrationKey(command.apiKey);
      break;
    case 'remove_integration_key':
      await connection.removeIntegrationKey();
      break;
    case 'sign_in_chatgpt':
      return connection.signInToChatgpt();
    case 'cancel_chatgpt_sign_in':
      connection.cancelChatgptSignIn();
      break;
    case 'disconnect_chatgpt':
      return connection.disconnectChatgpt();
  }
  return { ok: true };
}

export function registerAiConnectionHandlers(ctx: MainContext, registrar: IpcRegistrar): void {
  registrar.handle('aiConnection:getState', async (): Promise<ConnectionStateResult> => {
    try {
      const settings = await ctx.aiConnection.getSettings();
      let runtime: ConnectionRuntimeResult;
      try {
        runtime = { ok: true, status: await ctx.aiConnection.getStatus() };
      } catch {
        runtime = { ok: false, error: 'runtime_unavailable' };
      }
      return { ok: true, settings, runtime };
    } catch (error) {
      if (error instanceof ConnectionSettingsReadError) {
        return { ...failure(error), recovery: recovery(error.target) };
      }
      return failure(error);
    }
  });
  registrar.handle('aiConnection:update', (_event, input) =>
    runAuditedMutation(
      ctx,
      'aiConnection:update',
      async (): Promise<ConnectionUpdateResult> => {
        const parsed = ConnectionCommandSchema.safeParse(input);
        if (!parsed.success) return { ok: false, error: 'invalid_input' };
        try {
          return await apply(ctx, parsed.data);
        } catch (error) {
          return failure(error);
        }
      },
      (result) => !result.ok
    )
  );
}
