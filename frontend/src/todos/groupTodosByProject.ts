import {
  compareProjects,
  type WorkspaceProject,
  type WorkspaceSettings,
} from '@/pages/settings/components/workspaceSettingsModel';

import type {
  TodoListPage,
  TodoScopeQuery,
  TodoView,
} from '../../electron/src/todos/todoContracts';
import type { TodoProjectFilter } from './todoSelection';

/** The organization a group is shown under while all organizations are in view. */
export type TodoGroupOrganization = { kind: 'organization'; name: string } | { kind: 'none' };

/**
 * Where a TODO sits, as the backend stores it: in a project, which puts it in the project's
 * organization, or in no project with an organization of its own (`organizationId`) or none.
 * `organizationId` is null with a project.
 */
export type TodoPlace = { projectId: string | null; organizationId: string | null };

/** A place in view: a group of the list, whose new line puts a TODO there. */
export type TodoProjectSlot = TodoPlace & {
  /** Null for no project. */
  projectName: string | null;
  /** Null unless all organizations are in view. */
  organization: TodoGroupOrganization | null;
};

export type TodoProjectGroupView = TodoProjectSlot & {
  doneCount: number;
  totalCount: number;
  /** In the order the list gave them: open ones first, then done ones. */
  items: TodoView[];
};

/**
 * The places the scope shows, in order: projects by the workspace's order, each organization's
 * own no project after its projects and, while all organizations are in view, by organization
 * first. Projects in no organization, then no project in none, come last. A project in several
 * organizations is shown once, under the first of them, so each TODO is one row.
 */
function scopeSlots(
  settings: Pick<WorkspaceSettings, 'organizations' | 'projects'>,
  scope: TodoScopeQuery
): TodoProjectSlot[] {
  const projects = [...settings.projects].sort(compareProjects);
  const slot = (
    project: WorkspaceProject,
    organization: TodoGroupOrganization | null
  ): TodoProjectSlot => ({
    projectId: project.project_id,
    organizationId: null,
    projectName: project.display_name,
    organization,
  });
  const noProject = (
    organizationId: string | null,
    organization: TodoGroupOrganization | null
  ): TodoProjectSlot => ({ projectId: null, organizationId, projectName: null, organization });
  const unorganized = projects.filter((project) => project.organization_ids.length === 0);
  switch (scope.scope) {
    case 'organization':
      return [
        ...projects
          .filter((project) => project.organization_ids.some((id) => id === scope.organization_id))
          .map((project) => slot(project, null)),
        noProject(scope.organization_id ?? null, null),
      ];
    case 'no_organization':
      return [...unorganized.map((project) => slot(project, null)), noProject(null, null)];
    case 'all': {
      const none: TodoGroupOrganization = { kind: 'none' };
      const firstOrganization = (project: WorkspaceProject) =>
        settings.organizations.find((organization) =>
          project.organization_ids.includes(organization.organization_id)
        );
      return [
        ...settings.organizations.flatMap((organization) => {
          const heading: TodoGroupOrganization = {
            kind: 'organization',
            name: organization.display_name,
          };
          return [
            ...projects
              .filter((project) => firstOrganization(project) === organization)
              .map((project) => slot(project, heading)),
            noProject(organization.organization_id, heading),
          ];
        }),
        ...unorganized.map((project) => slot(project, none)),
        noProject(null, none),
      ];
    }
  }
}

const inFilter = (projectId: string | null, filter: TodoProjectFilter): boolean =>
  filter.project === 'any' ||
  (filter.project === 'no_project' ? projectId === null : projectId === filter.project_id);

/** The projects in view under the sidebar's project filter. */
export const todoProjectSlots = (
  settings: Pick<WorkspaceSettings, 'organizations' | 'projects'>,
  scope: TodoScopeQuery,
  filter: TodoProjectFilter
): TodoProjectSlot[] =>
  scopeSlots(settings, scope).filter((slot) => inFilter(slot.projectId, filter));

/**
 * The list's TODOs by place, one group for every slot, empty or not. A TODO whose place is not in
 * `slots` is not shown: the settings are read with the page, and a deleted project's TODOs move
 * to no project, a deleted organization's own ones to no organization.
 */
export function groupTodosByProject(
  page: TodoListPage,
  slots: readonly TodoProjectSlot[]
): TodoProjectGroupView[] {
  return slots.map((slot) => {
    const items = page.items.filter(
      (item) => item.project_id === slot.projectId && item.organization_id === slot.organizationId
    );
    return {
      ...slot,
      doneCount: items.filter((item) => item.state.kind === 'done').length,
      totalCount: items.length,
      items,
    };
  });
}
