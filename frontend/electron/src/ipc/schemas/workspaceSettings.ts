/**
 * Zod schemas for `workspaceSettings:*` IPC payloads.
 */

import { z } from 'zod';

import {
  MAX_ARRAY_LENGTH,
  MAX_DISPLAY_NAME_LENGTH,
  MAX_ID_LENGTH,
  MAX_PATH_LENGTH,
} from './limits';

export const IdSchema = z.string().trim().min(1).max(MAX_ID_LENGTH);
const DisplayNameSchema = z.string().trim().min(1).max(MAX_DISPLAY_NAME_LENGTH);
const RealPathSchema = z.string().trim().min(1).max(MAX_PATH_LENGTH);
const IdArraySchema = z.array(IdSchema).max(MAX_ARRAY_LENGTH);

export const ReorderProjectsInputSchema = z.object({
  projectIds: IdArraySchema.refine((ids) => ids.length === new Set(ids).size, {
    message: 'projectIds must not contain duplicates',
  }),
});

export const CreateOrganizationInputSchema = z.object({
  displayName: DisplayNameSchema,
});

export const CreateProjectInputSchema = z.object({
  displayName: DisplayNameSchema,
  organizationIds: IdArraySchema,
});

export const RenameProjectInputSchema = z.object({
  displayName: DisplayNameSchema,
});

export const CreateFolderInputSchema = z.object({
  displayName: DisplayNameSchema,
  realPath: RealPathSchema,
  organizationIds: IdArraySchema,
  projectIds: IdArraySchema,
});

export const UpdateProjectLinksInputSchema = z.object({
  organizationIds: IdArraySchema,
});

export const UpdateFolderLinksInputSchema = z.object({
  organizationIds: IdArraySchema,
  projectIds: IdArraySchema,
});

export const ReadAccessScopeSchema = z.enum(['workspace', 'full_access']);
export const CommandNetworkEnabledSchema = z.boolean();

export type CreateOrganizationInput = z.infer<typeof CreateOrganizationInputSchema>;
export type CreateProjectInput = z.infer<typeof CreateProjectInputSchema>;
export type RenameProjectInput = z.infer<typeof RenameProjectInputSchema>;
export type CreateFolderInput = z.infer<typeof CreateFolderInputSchema>;
export type UpdateProjectLinksInput = z.infer<typeof UpdateProjectLinksInputSchema>;
export type UpdateFolderLinksInput = z.infer<typeof UpdateFolderLinksInputSchema>;
export type ReorderProjectsInput = z.infer<typeof ReorderProjectsInputSchema>;
