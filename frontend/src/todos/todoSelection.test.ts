import { expect, it } from 'vitest';

import {
  DEFAULT_TODO_SELECTION,
  parseTodoSelection,
  todoSelectionSearch,
  type TodoSelection,
} from './todoSelection';

it('leaves the default selection out of the query', () => {
  expect(todoSelectionSearch(DEFAULT_TODO_SELECTION)).toBe('');
  expect(parseTodoSelection('')).toEqual(DEFAULT_TODO_SELECTION);
});

it('round-trips every part of a selection, ids with reserved characters included', () => {
  const selections: TodoSelection[] = [
    {
      scope: { scope: 'organization', organization_id: 'org 1&x=y' },
      project: { project: 'project', project_id: 'p:1/2' },
      weekStart: '2026-11-09',
      view: 'progress',
      progressSpan: 'year',
      progressOn: '2025-03-01',
      progressDay: '2025-03-04',
      todoId: 'todo #1',
    },
    {
      scope: { scope: 'no_organization' },
      project: { project: 'no_project' },
      weekStart: null,
      view: 'deleted',
      progressSpan: 'month',
      progressOn: null,
      progressDay: null,
      todoId: null,
    },
  ];
  for (const selection of selections) {
    expect(parseTodoSelection(todoSelectionSearch(selection))).toEqual(selection);
  }
  expect(todoSelectionSearch(selections[0])).toBe(
    '?scope=organization:org%201%26x%3Dy&project=project:p%3A1%2F2&week=2026-11-09&view=progress&span=year&on=2025-03-01&day=2025-03-04&todo=todo%20%231'
  );
});

it('falls back to the default for each malformed value', () => {
  for (const search of [
    '?scope=organization:',
    '?scope=organization:%20org',
    '?scope=org-1',
    '?project=project:',
    '?project=p-1',
    '?week=2026-02-30',
    '?week=2026-1-9',
    '?view=board',
    '?span=week',
    '?span=Year',
    '?on=2026-13-01',
    '?day=2026-11-31',
    '?todo=',
    '?todo=%20t1',
  ]) {
    expect(parseTodoSelection(search), search).toEqual(DEFAULT_TODO_SELECTION);
  }
});
