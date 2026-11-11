/**
 * Zod schemas for `workspaceSettings:*` IPC payloads.
 */

import { z } from 'zod';

import {
  MAX_ARRAY_LENGTH,
  MAX_DISPLAY_NAME_LENGTH,
  MAX_ICON_DATA_URL_LENGTH,
  MAX_ID_LENGTH,
  MAX_PATH_LENGTH,
} from './limits';

export const IdSchema = z.string().trim().min(1).max(MAX_ID_LENGTH);
const DisplayNameSchema = z.string().trim().min(1).max(MAX_DISPLAY_NAME_LENGTH);
const RealPathSchema = z.string().trim().min(1).max(MAX_PATH_LENGTH);
const IdArraySchema = z.array(IdSchema).max(MAX_ARRAY_LENGTH);
// The backend checks the image itself; null is "no icon".
const IconDataUrlSchema = z.string().max(MAX_ICON_DATA_URL_LENGTH).nullable();

export const ReorderProjectsInputSchema = z.object({
  projectIds: IdArraySchema.refine((ids) => ids.length === new Set(ids).size, {
    message: 'projectIds must not contain duplicates',
  }),
});

export const CreateOrganizationInputSchema = z.object({
  displayName: DisplayNameSchema,
  iconDataUrl: IconDataUrlSchema,
});

/** The organization's whole editable state: a null icon clears it. */
export const UpdateOrganizationInputSchema = z.object({
  displayName: DisplayNameSchema,
  iconDataUrl: IconDataUrlSchema,
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
export type UpdateOrganizationInput = z.infer<typeof UpdateOrganizationInputSchema>;
export type CreateProjectInput = z.infer<typeof CreateProjectInputSchema>;
export type RenameProjectInput = z.infer<typeof RenameProjectInputSchema>;
export type CreateFolderInput = z.infer<typeof CreateFolderInputSchema>;
export type UpdateProjectLinksInput = z.infer<typeof UpdateProjectLinksInputSchema>;
export type UpdateFolderLinksInput = z.infer<typeof UpdateFolderLinksInputSchema>;
export type ReorderProjectsInput = z.infer<typeof ReorderProjectsInputSchema>;
