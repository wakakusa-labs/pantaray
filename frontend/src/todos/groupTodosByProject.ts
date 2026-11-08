import {
  compareProjects,
  type WorkspaceProject,
  type WorkspaceSettings,
} from '@/pages/settings/components/workspaceSettingsModel';

import type {
  TodoScopeQuery,
  TodoView,
  TodoWeekPage,
} from '../../electron/src/todos/todoContracts';
import type { TodoProjectFilter } from './todoSelection';

/** The organization a group is shown under while all organizations are in view. */
export type TodoGroupOrganization = { kind: 'organization'; name: string } | { kind: 'none' };

export type TodoProjectGroupView = {
  /** Null for the TODOs in no project. */
  projectId: string | null;
  projectName: string | null;
  /** Null unless all organizations are in view. */
  organization: TodoGroupOrganization | null;
  doneCount: number;
  totalCount: number;
  /** In the order the week gave them; a done TODO stays in its place. */
  items: TodoView[];
};

/** A project in view: a group of the week, and a place the add row can put a TODO. */
export type TodoProjectSlot = Pick<
  TodoProjectGroupView,
  'projectId' | 'projectName' | 'organization'
>;

/** A week that has not ended takes new TODOs; an ended week only shows what it held. */
export const weekTakesTodos = (page: TodoWeekPage): boolean =>
  page.week.start >= page.current_week_start;

/**
 * The projects the scope shows, in order: projects by the workspace's order and, while all
 * organizations are in view, by organization first. Projects in no organization, then no
 * project, come last. A project in several organizations is shown once, under the first of
 * them, so each TODO is one row.
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
    projectName: project.display_name,
    organization,
  });
  const unorganized = projects.filter((project) => project.organization_ids.length === 0);
  switch (scope.scope) {
    case 'organization':
      return projects
        .filter((project) => project.organization_ids.some((id) => id === scope.organization_id))
        .map((project) => slot(project, null));
    case 'no_organization':
      return [
        ...unorganized.map((project) => slot(project, null)),
        { projectId: null, projectName: null, organization: null },
      ];
    case 'all': {
      const none: TodoGroupOrganization = { kind: 'none' };
      const firstOrganization = (project: WorkspaceProject) =>
        settings.organizations.find((organization) =>
          project.organization_ids.includes(organization.organization_id)
        );
      return [
        ...settings.organizations.flatMap((organization) =>
          projects
            .filter((project) => firstOrganization(project) === organization)
            .map((project) =>
              slot(project, { kind: 'organization', name: organization.display_name })
            )
        ),
        ...unorganized.map((project) => slot(project, none)),
        { projectId: null, projectName: null, organization: none },
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
 * The week's TODOs by project, for the projects that hold any. A TODO whose project is not in
 * `slots` is not shown: the settings are read with the page, and a deleted project's TODOs move
 * to no project.
 */
export function groupTodosByProject(
  page: TodoWeekPage,
  slots: readonly TodoProjectSlot[]
): TodoProjectGroupView[] {
  return slots
    .map((slot) => {
      const items = page.items.filter((item) => item.project_id === slot.projectId);
      return {
        ...slot,
        doneCount: items.filter((item) => item.state.kind === 'done').length,
        totalCount: items.length,
        items,
      };
    })
    .filter((group) => group.totalCount > 0);
}
