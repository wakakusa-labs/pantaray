import { describe, expect, it } from 'vitest';

import {
  applyWorkspaceMutation,
  orderProjectsByIds,
  resolveProjectOrganizations,
  type WorkspaceFolder,
  type WorkspaceOrganization,
  type WorkspaceProject,
  type WorkspaceSettings,
} from './workspaceSettingsModel';

const organizations: WorkspaceOrganization[] = [
  { organization_id: 'org-z', display_name: 'Alpha' },
  { organization_id: 'org-a', display_name: 'Alpha' },
  { organization_id: 'org-b', display_name: 'Beta' },
];

const projects: WorkspaceProject[] = [
  { project_id: 'project-z', display_name: 'Zulu', sort_order: 0, organization_ids: [] },
  {
    project_id: 'project-b',
    display_name: 'Bravo',
    sort_order: 1,
    organization_ids: ['org-b', 'org-z'],
  },
  {
    project_id: 'project-a',
    display_name: 'Alpha',
    sort_order: 2,
    organization_ids: ['missing-org'],
  },
  {
    project_id: 'project-c',
    display_name: 'Charlie',
    sort_order: 3,
    organization_ids: ['org-a'],
  },
];

const folders: WorkspaceFolder[] = [
  {
    folder_id: 'folder-z',
    display_name: 'Shared',
    real_path: '/z',
    canonical_real_path: '/z',
    organization_ids: ['org-b'],
    project_ids: ['project-b'],
  },
  {
    folder_id: 'folder-a',
    display_name: 'Shared',
    real_path: '/a',
    canonical_real_path: '/a',
    organization_ids: ['org-a'],
    project_ids: ['project-a'],
  },
];

const settings: WorkspaceSettings = {
  read_access_scope: 'workspace',
  organizations,
  projects,
  folders,
};

describe('workspaceSettingsModel', () => {
  it('resolves organizations by display name and ID while dropping unknown IDs', () => {
    expect(resolveProjectOrganizations(projects[1], organizations)).toEqual([
      organizations[0],
      organizations[2],
    ]);
    expect(resolveProjectOrganizations(projects[2], organizations)).toEqual([]);
  });

  it('rebuilds a complete persisted order with contiguous sort values', () => {
    const reordered = orderProjectsByIds(projects, [
      'project-c',
      'project-z',
      'project-a',
      'project-b',
    ]);
    expect(reordered.map((project) => [project.project_id, project.sort_order])).toEqual([
      ['project-c', 0],
      ['project-z', 1],
      ['project-a', 2],
      ['project-b', 3],
    ]);
    expect(projects[0].sort_order).toBe(0);
  });

  it('orders mutation upserts with the SQLite BINARY collation', () => {
    const lowercaseSettings: WorkspaceSettings = {
      read_access_scope: 'workspace',
      organizations: [{ organization_id: 'org-apple', display_name: 'apple' }],
      projects: [
        {
          project_id: 'project-apple',
          display_name: 'apple',
          sort_order: 0,
          organization_ids: [],
        },
      ],
      folders: [
        {
          folder_id: 'folder-apple',
          display_name: 'apple',
          real_path: '/apple',
          canonical_real_path: '/apple',
          organization_ids: [],
          project_ids: [],
        },
      ],
    };
    const withOrganization = applyWorkspaceMutation(lowercaseSettings, {
      type: 'organizationCreated',
      organization: { organization_id: 'org-zebra', display_name: 'Zebra' },
    });
    const withProject = applyWorkspaceMutation(lowercaseSettings, {
      type: 'projectCreated',
      project: {
        project_id: 'project-zebra',
        display_name: 'Zebra',
        sort_order: 0,
        organization_ids: [],
      },
    });
    const withFolder = applyWorkspaceMutation(lowercaseSettings, {
      type: 'folderCreated',
      folder: {
        folder_id: 'folder-zebra',
        display_name: 'Zebra',
        real_path: '/Zebra',
        canonical_real_path: '/Zebra',
        organization_ids: [],
        project_ids: [],
      },
    });

    expect(withOrganization.organizations.map((item) => item.display_name)).toEqual([
      'Zebra',
      'apple',
    ]);
    expect(withProject.projects.map((item) => item.display_name)).toEqual(['Zebra', 'apple']);
    expect(withFolder.folders.map((item) => item.display_name)).toEqual(['Zebra', 'apple']);
  });

  it.each([
    {
      event: {
        type: 'organizationCreated' as const,
        organization: { organization_id: 'org-new', display_name: 'Aardvark' },
      },
      select: (next: WorkspaceSettings) => next.organizations,
      expectedIds: ['org-new', 'org-a', 'org-z', 'org-b'],
    },
    {
      event: {
        type: 'projectCreated' as const,
        project: {
          project_id: 'project-new',
          display_name: 'New',
          sort_order: 9,
          organization_ids: [],
        },
      },
      select: (next: WorkspaceSettings) => next.projects,
      expectedIds: ['project-z', 'project-b', 'project-a', 'project-c', 'project-new'],
    },
    {
      event: { type: 'projectDeleted' as const, projectId: 'project-b' },
      select: (next: WorkspaceSettings) => next.projects,
      expectedIds: ['project-z', 'project-a', 'project-c'],
    },
    {
      event: {
        type: 'folderCreated' as const,
        folder: {
          folder_id: 'folder-new',
          display_name: 'Alpha',
          real_path: '/new',
          canonical_real_path: '/new',
          organization_ids: [],
          project_ids: ['project-a'],
        },
      },
      select: (next: WorkspaceSettings) => next.folders,
      expectedIds: ['folder-new', 'folder-a', 'folder-z'],
    },
    {
      event: { type: 'folderDeleted' as const, folderId: 'folder-a' },
      select: (next: WorkspaceSettings) => next.folders,
      expectedIds: ['folder-z'],
    },
    {
      event: {
        type: 'projectLinksUpdated' as const,
        project: { ...projects[1], organization_ids: ['org-a'] },
      },
      select: (next: WorkspaceSettings) => next.projects,
      expectedIds: ['project-z', 'project-b', 'project-a', 'project-c'],
    },
    {
      event: {
        type: 'folderLinksUpdated' as const,
        folder: { ...folders[0], organization_ids: [], project_ids: ['project-c'] },
      },
      select: (next: WorkspaceSettings) => next.folders,
      expectedIds: ['folder-a', 'folder-z'],
    },
    {
      event: {
        type: 'projectsReordered' as const,
        projectIds: ['project-c', 'project-a', 'project-b', 'project-z'],
      },
      select: (next: WorkspaceSettings) => next.projects,
      expectedIds: ['project-c', 'project-a', 'project-b', 'project-z'],
    },
    {
      event: {
        type: 'projectsOrderPreview' as const,
        projectIds: ['project-c', 'project-a', 'project-b', 'project-z'],
      },
      select: (next: WorkspaceSettings) => next.projects,
      expectedIds: ['project-c', 'project-a', 'project-b', 'project-z'],
    },
  ])('applies $event.type using the server response shape', ({ event, select, expectedIds }) => {
    const next = applyWorkspaceMutation(settings, event);
    expect(select(next).map(entityId)).toEqual(expectedIds);
  });

  it('upserts same-name responses by canonical ID and preserves project sort holes', () => {
    const updatedProject = {
      ...projects[1],
      display_name: projects[2].display_name,
      sort_order: 7,
      organization_ids: ['org-a'],
    };
    const next = applyWorkspaceMutation(settings, {
      type: 'projectCreated',
      project: updatedProject,
    });

    expect(next.projects.filter((project) => project.project_id === 'project-b')).toEqual([
      updatedProject,
    ]);
    expect(next.projects.map((project) => project.sort_order)).toEqual([0, 2, 3, 7]);
  });

  it('keeps canonical folder normalization returned by the server', () => {
    const normalizedFolder = {
      ...folders[0],
      organization_ids: [],
      project_ids: ['project-c'],
    };
    const next = applyWorkspaceMutation(settings, {
      type: 'folderLinksUpdated',
      folder: normalizedFolder,
    });

    expect(next.folders.find((folder) => folder.folder_id === 'folder-z')).toEqual(
      normalizedFolder
    );
  });

  it('mirrors link-table cascades and drops folders only a deleted project held', () => {
    const withoutProject = applyWorkspaceMutation(settings, {
      type: 'projectDeleted',
      projectId: 'project-b',
    });
    expect(withoutProject.folders.map((folder) => folder.folder_id)).toEqual(['folder-a']);

    const shared = applyWorkspaceMutation(
      { ...settings, folders: [{ ...folders[0], project_ids: ['project-b', 'project-c'] }] },
      { type: 'projectDeleted', projectId: 'project-b' }
    );
    expect(shared.folders.map((folder) => folder.project_ids)).toEqual([['project-c']]);
  });

  it('reorders current project values during preview without rolling back concurrent link updates', () => {
    const linked = applyWorkspaceMutation(settings, {
      type: 'projectLinksUpdated',
      project: { ...projects[1], organization_ids: ['org-a'] },
    });
    const next = applyWorkspaceMutation(linked, {
      type: 'projectsOrderPreview',
      projectIds: ['project-b', 'project-z', 'project-a', 'project-c'],
    });

    expect(next.projects.map((project) => project.sort_order)).toEqual([0, 1, 2, 3]);
    expect(next.projects[0].organization_ids).toEqual(['org-a']);
  });
});

function entityId(entity: WorkspaceOrganization | WorkspaceProject | WorkspaceFolder): string {
  if ('organization_id' in entity) return entity.organization_id;
  if ('project_id' in entity) return entity.project_id;
  return entity.folder_id;
}
