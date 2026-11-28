import { expect, it } from 'vitest';

import type { TodoListPage, TodoView } from '../../electron/src/todos/todoContracts';
import { groupTodosByProject, todoPlaceName, todoProjectSlots } from './groupTodosByProject';

const settings = {
  organizations: [
    { organization_id: 'org-a', display_name: 'Alpha', icon_data_url: null },
    { organization_id: 'org-b', display_name: 'Beta', icon_data_url: null },
  ],
  // Listed out of order: the groups follow sort_order, not this list.
  projects: [
    { project_id: 'p-beta', display_name: 'Beta only', sort_order: 2, organization_ids: ['org-b'] },
    {
      project_id: 'p-both',
      display_name: 'Both',
      sort_order: 0,
      organization_ids: ['org-b', 'org-a'],
    },
    { project_id: 'p-loose2', display_name: 'Loose 2', sort_order: 4, organization_ids: [] },
    {
      project_id: 'p-alpha',
      display_name: 'Alpha only',
      sort_order: 1,
      organization_ids: ['org-a'],
    },
    { project_id: 'p-loose1', display_name: 'Loose 1', sort_order: 3, organization_ids: [] },
  ],
};

function todo(
  todoId: string,
  projectId: string | null,
  done = false,
  organizationId: string | null = null
): TodoView {
  return {
    todo_id: todoId,
    project_id: projectId,
    organization_id: organizationId,
    text: todoId,
    author: 'user',
    origin: { kind: 'written' },
    starts_on: '2026-11-02',
    due: null,
    state: done
      ? { kind: 'done', by: 'user', at: '2026-11-03T00:00:00Z' }
      : { kind: 'not_started', reason: 'starting' },
    deletion_request: null,
    conversation: { kind: 'none' },
  };
}

const page = (items: TodoView[]): TodoListPage => ({ today: '2026-11-07', items });

const ids = (slots: readonly { projectId: string | null }[]) => slots.map((g) => g.projectId);

it('orders all organizations by organization, then sort_order, with no organization and no project last', () => {
  const groups = todoProjectSlots(settings, { scope: 'all' }, { project: 'any' });
  // A project in two organizations is shown once, under the first organization in order; each
  // organization's own TODOs in no project follow its projects.
  expect(ids(groups)).toEqual([
    'p-both',
    'p-alpha',
    null,
    'p-beta',
    null,
    'p-loose1',
    'p-loose2',
    null,
  ]);
  expect(groups.map((g) => g.organizationId)).toEqual([
    null,
    null,
    'org-a',
    null,
    'org-b',
    null,
    null,
    null,
  ]);
  expect(groups.map((g) => g.organization)).toEqual([
    { kind: 'organization', name: 'Alpha' },
    { kind: 'organization', name: 'Alpha' },
    { kind: 'organization', name: 'Alpha' },
    { kind: 'organization', name: 'Beta' },
    { kind: 'organization', name: 'Beta' },
    { kind: 'none' },
    { kind: 'none' },
    { kind: 'none' },
  ]);
});

it('shows one organization by sort_order, then its own no project, without an organization heading', () => {
  const groups = todoProjectSlots(
    settings,
    { scope: 'organization', organization_id: 'org-b' },
    { project: 'any' }
  );
  expect(ids(groups)).toEqual(['p-both', 'p-beta', null]);
  expect(groups[2].organizationId).toBe('org-b');
  expect(groups.every((g) => g.organization === null)).toBe(true);
});

it('shows the projects in no organization, then the TODOs in no project', () => {
  const groups = todoProjectSlots(settings, { scope: 'no_organization' }, { project: 'any' });
  expect(ids(groups)).toEqual(['p-loose1', 'p-loose2', null]);
});

it('groups every place in view, empty ones too, counting done ones and keeping the order given', () => {
  const items = [
    todo('t1', 'p-alpha', true),
    todo('t2', null),
    todo('t3', 'p-alpha'),
    todo('t4', null, false, 'org-b'),
  ];
  const groups = groupTodosByProject(
    page(items),
    todoProjectSlots(
      settings,
      { scope: 'organization', organization_id: 'org-b' },
      { project: 'any' }
    )
  );
  // A TODO in no project goes to its own organization's place only.
  expect(groups.map((g) => [g.projectId, g.totalCount])).toEqual([
    ['p-both', 0],
    ['p-beta', 0],
    [null, 1],
  ]);
  expect(groups[2].items.map((item) => item.todo_id)).toEqual(['t4']);
  const alpha = groupTodosByProject(
    page(items),
    todoProjectSlots(settings, { scope: 'all' }, { project: 'any' })
  ).find((g) => g.projectId === 'p-alpha');
  expect(alpha).toMatchObject({ projectName: 'Alpha only', doneCount: 1, totalCount: 2 });
  expect(alpha?.items.map((item) => item.todo_id)).toEqual(['t1', 't3']);
});

it('keeps only the project the filter names', () => {
  const one = todoProjectSlots(
    settings,
    { scope: 'all' },
    { project: 'project', project_id: 'p-alpha' }
  );
  expect(ids(one)).toEqual(['p-alpha']);
  const none = todoProjectSlots(settings, { scope: 'no_organization' }, { project: 'no_project' });
  expect(none).toEqual([
    { projectId: null, organizationId: null, projectName: null, organization: null },
  ]);
});

it('names a TODO’s place as the list groups it', () => {
  const place = (project_id: string | null, organization_id: string | null = null) =>
    todoPlaceName(settings, { project_id, organization_id });
  // A project in two organizations is under the first in their order, as in the list.
  expect(place('p-both')).toEqual({ organization: 'Alpha', project: 'Both' });
  expect(place('p-loose1')).toEqual({ organization: null, project: 'Loose 1' });
  expect(place(null, 'org-b')).toEqual({ organization: 'Beta', project: null });
  expect(place(null)).toEqual({ organization: null, project: null });
  // Not in the settings read yet: nothing rather than a wrong place.
  expect(place('p-new')).toBeNull();
  expect(place(null, 'org-new')).toBeNull();
});
