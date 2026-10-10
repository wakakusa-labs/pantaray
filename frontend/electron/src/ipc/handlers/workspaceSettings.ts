import type { MainContext } from '../context';
import type { IpcRegistrar } from '../registrar';
import { parseInput } from '../schemas/error';
import {
  CreateFolderInputSchema,
  CreateOrganizationInputSchema,
  CreateProjectInputSchema,
  IdSchema,
  ReadAccessScopeSchema,
  CommandNetworkEnabledSchema,
  RenameProjectInputSchema,
  ReorderProjectsInputSchema,
  UpdateFolderLinksInputSchema,
  UpdateProjectLinksInputSchema,
} from '../schemas/workspaceSettings';

export function registerWorkspaceSettingsHandlers(ctx: MainContext, registrar: IpcRegistrar): void {
  registrar.handle('workspaceSettings:get', async () => {
    return await ctx.workspaceSettings.get();
  });
  registrar.handle('workspaceSettings:getReadAccessScope', async () => {
    return await ctx.workspaceSettings.getReadAccessScope();
  });
  registrar.handle('workspaceSettings:createOrganization', async (_event, input) => {
    const parsed = parseInput(
      CreateOrganizationInputSchema,
      'workspaceSettings:createOrganization',
      input
    );
    return await ctx.workspaceSettings.createOrganization(parsed);
  });
  registrar.handle('workspaceSettings:createProject', async (_event, input) => {
    const parsed = parseInput(CreateProjectInputSchema, 'workspaceSettings:createProject', input);
    return await ctx.workspaceSettings.createProject(parsed);
  });
  registrar.handle('workspaceSettings:renameProject', async (_event, projectId, input) => {
    const id = parseInput(IdSchema, 'workspaceSettings:renameProject', projectId);
    const parsed = parseInput(RenameProjectInputSchema, 'workspaceSettings:renameProject', input);
    return await ctx.workspaceSettings.renameProject(id, parsed);
  });
  registrar.handle('workspaceSettings:createFolder', async (_event, input) => {
    const parsed = parseInput(CreateFolderInputSchema, 'workspaceSettings:createFolder', input);
    return await ctx.workspaceSettings.createFolder(parsed);
  });
  registrar.handle('workspaceSettings:reorderProjects', async (_event, input) => {
    const parsed = parseInput(
      ReorderProjectsInputSchema,
      'workspaceSettings:reorderProjects',
      input
    );
    return await ctx.workspaceSettings.reorderProjects(parsed);
  });
  registrar.handle('workspaceSettings:deleteOrganization', async (_event, organizationId) => {
    const id = parseInput(IdSchema, 'workspaceSettings:deleteOrganization', organizationId);
    return await ctx.workspaceSettings.deleteOrganization(id);
  });
  registrar.handle('workspaceSettings:deleteProject', async (_event, projectId) => {
    const id = parseInput(IdSchema, 'workspaceSettings:deleteProject', projectId);
    return await ctx.workspaceSettings.deleteProject(id);
  });
  registrar.handle('workspaceSettings:deleteFolder', async (_event, folderId) => {
    const id = parseInput(IdSchema, 'workspaceSettings:deleteFolder', folderId);
    return await ctx.workspaceSettings.deleteFolder(id);
  });
  registrar.handle('workspaceSettings:updateProjectLinks', async (_event, projectId, input) => {
    const id = parseInput(IdSchema, 'workspaceSettings:updateProjectLinks', projectId);
    const parsed = parseInput(
      UpdateProjectLinksInputSchema,
      'workspaceSettings:updateProjectLinks',
      input
    );
    return await ctx.workspaceSettings.updateProjectLinks(id, parsed);
  });
  registrar.handle('workspaceSettings:updateFolderLinks', async (_event, folderId, input) => {
    const id = parseInput(IdSchema, 'workspaceSettings:updateFolderLinks', folderId);
    const parsed = parseInput(
      UpdateFolderLinksInputSchema,
      'workspaceSettings:updateFolderLinks',
      input
    );
    return await ctx.workspaceSettings.updateFolderLinks(id, parsed);
  });
  registrar.handle('workspaceSettings:updateReadAccessScope', async (_event, readAccessScope) => {
    const parsed = parseInput(
      ReadAccessScopeSchema,
      'workspaceSettings:updateReadAccessScope',
      readAccessScope
    );
    return await ctx.workspaceSettings.updateReadAccessScope(parsed);
  });
  registrar.handle('workspaceSettings:getCommandNetwork', async () => {
    return await ctx.workspaceSettings.getCommandNetwork();
  });
  registrar.handle('workspaceSettings:updateCommandNetwork', async (_event, enabled) => {
    const parsed = parseInput(
      CommandNetworkEnabledSchema,
      'workspaceSettings:updateCommandNetwork',
      enabled
    );
    return await ctx.workspaceSettings.updateCommandNetwork(parsed);
  });
  registrar.handle('workspaceSettings:selectFolder', async () => {
    return await ctx.workspaceSettings.selectFolder();
  });
  registrar.handle('workspaceSettings:openFolder', async (_event, folderId) => {
    const id = parseInput(IdSchema, 'workspaceSettings:openFolder', folderId);
    return await ctx.workspaceSettings.openFolder(id);
  });
}
