import { expect, it } from 'vitest';

import type { TodoView, TodoWeekPage } from '../../electron/src/todos/todoContracts';
import { groupTodosByProject, todoProjectSlots } from './groupTodosByProject';

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

function todo(todoId: string, projectId: string | null, done = false): TodoView {
  return {
    todo_id: todoId,
    project_id: projectId,
    text: todoId,
    author: 'user',
    origin: 'written',
    starts_on: '2026-11-02',
    carried: false,
    due: null,
    state: done
      ? { kind: 'done', by: 'user', at: '2026-11-03T00:00:00Z' }
      : { kind: 'not_started', reason: 'starting' },
    deletion_request: null,
    conversation: { kind: 'none' },
  };
}

const page = (items: TodoView[]): TodoWeekPage => ({
  today: '2026-11-07',
  current_week_start: '2026-11-02',
  week: { start: '2026-11-02', end: '2026-11-08' },
  items,
});

const ids = (slots: readonly { projectId: string | null }[]) => slots.map((g) => g.projectId);

it('orders all organizations by organization, then sort_order, with no organization and no project last', () => {
  const groups = todoProjectSlots(settings, { scope: 'all' }, { project: 'any' });
  // A project in two organizations is shown once, under the first organization in order.
  expect(ids(groups)).toEqual(['p-both', 'p-alpha', 'p-beta', 'p-loose1', 'p-loose2', null]);
  expect(groups.map((g) => g.organization)).toEqual([
    { kind: 'organization', name: 'Alpha' },
    { kind: 'organization', name: 'Alpha' },
    { kind: 'organization', name: 'Beta' },
    { kind: 'none' },
    { kind: 'none' },
    { kind: 'none' },
  ]);
});

it('shows one organization by sort_order without an organization heading or a no-project group', () => {
  const groups = todoProjectSlots(
    settings,
    { scope: 'organization', organization_id: 'org-b' },
    { project: 'any' }
  );
  expect(ids(groups)).toEqual(['p-both', 'p-beta']);
  expect(groups.every((g) => g.organization === null)).toBe(true);
});

it('shows the projects in no organization, then the TODOs in no project', () => {
  const groups = todoProjectSlots(settings, { scope: 'no_organization' }, { project: 'any' });
  expect(ids(groups)).toEqual(['p-loose1', 'p-loose2', null]);
});

it('groups the projects holding TODOs, counting done ones and keeping the order given', () => {
  const items = [todo('t1', 'p-alpha', true), todo('t2', null), todo('t3', 'p-alpha')];
  const groups = groupTodosByProject(
    page(items),
    todoProjectSlots(settings, { scope: 'all' }, { project: 'any' })
  );
  expect(ids(groups)).toEqual(['p-alpha', null]);
  const alpha = groups.find((g) => g.projectId === 'p-alpha');
  expect(alpha).toMatchObject({ projectName: 'Alpha only', doneCount: 1, totalCount: 2 });
  expect(alpha?.items.map((item) => item.todo_id)).toEqual(['t1', 't3']);
  expect(groups[groups.length - 1]).toMatchObject({
    projectId: null,
    projectName: null,
    totalCount: 1,
  });
});

it('keeps only the project the filter names', () => {
  const one = todoProjectSlots(
    settings,
    { scope: 'all' },
    { project: 'project', project_id: 'p-alpha' }
  );
  expect(ids(one)).toEqual(['p-alpha']);
  const none = todoProjectSlots(settings, { scope: 'all' }, { project: 'no_project' });
  expect(none).toEqual([{ projectId: null, projectName: null, organization: { kind: 'none' } }]);
});
