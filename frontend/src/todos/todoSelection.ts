import type { TodoScopeQuery, TodoWeekQuery } from '../../electron/src/todos/todoContracts';

/** Which of the week's projects are in view. */
export type TodoProjectFilter = Pick<TodoWeekQuery, 'project' | 'project_id'>;

export type TodoPageView = 'list' | 'progress' | 'deleted';

/** The progress calendar: a month of days, or a year of months. */
export type ProgressSpan = 'month' | 'year';

/** What the TODO page shows, kept in its query so reloading and going back return to it. */
export type TodoSelection = {
  scope: TodoScopeQuery;
  project: TodoProjectFilter;
  /** Null is the current week; any day of a week names that week. */
  weekStart: string | null;
  view: TodoPageView;
  progressSpan: ProgressSpan;
  /** Null is the month (year) holding today; any day of a month (year) names that one. */
  progressOn: string | null;
  /** The day whose records are open beside the calendar. */
  progressDay: string | null;
  /** The TODO whose conversation is open beside the list. */
  todoId: string | null;
};

export const DEFAULT_TODO_SELECTION: TodoSelection = {
  scope: { scope: 'all' },
  project: { project: 'any' },
  weekStart: null,
  view: 'list',
  progressSpan: 'month',
  progressOn: null,
  progressDay: null,
  todoId: null,
};

const SCOPE_PARAM = 'scope';
const PROJECT_PARAM = 'project';
const WEEK_PARAM = 'week';
const VIEW_PARAM = 'view';
const SPAN_PARAM = 'span';
const ON_PARAM = 'on';
const DAY_PARAM = 'day';
const TODO_PARAM = 'todo';
/** The value for TODOs in no organization, and for TODOs in no project. */
const NONE = 'none';
const ORGANIZATION_PREFIX = 'organization:';
const PROJECT_PREFIX = 'project:';
const LOCAL_DATE = /^\d{4}-\d{2}-\d{2}$/;
const VIEWS: readonly TodoPageView[] = ['list', 'progress', 'deleted'];
const SPANS: readonly ProgressSpan[] = ['month', 'year'];

/** Ids are canonical: never blank, never padded. */
const canonicalId = (value: string | null): string | null =>
  value && value === value.trim() ? value : null;

/** The id after `prefix`, or null when the value does not name one. */
const prefixedId = (value: string | null, prefix: string): string | null =>
  value?.startsWith(prefix) ? canonicalId(value.slice(prefix.length)) : null;

/** A YYYY-MM-DD that is a day on the calendar, or null. */
function localDate(value: string | null): string | null {
  if (!value || !LOCAL_DATE.test(value)) return null;
  const day = new Date(`${value}T00:00:00Z`);
  return !Number.isNaN(day.getTime()) && day.toISOString().startsWith(value) ? value : null;
}

function parseScope(value: string | null): TodoScopeQuery {
  if (value === NONE) return { scope: 'no_organization' };
  const organizationId = prefixedId(value, ORGANIZATION_PREFIX);
  return organizationId === null
    ? DEFAULT_TODO_SELECTION.scope
    : { scope: 'organization', organization_id: organizationId };
}

function parseProject(value: string | null): TodoProjectFilter {
  if (value === NONE) return { project: 'no_project' };
  const projectId = prefixedId(value, PROJECT_PREFIX);
  return projectId === null
    ? DEFAULT_TODO_SELECTION.project
    : { project: 'project', project_id: projectId };
}

/** The selection a query names. A missing or malformed value is its default. */
export function parseTodoSelection(search: string): TodoSelection {
  const params = new URLSearchParams(search);
  const view = params.get(VIEW_PARAM);
  const span = params.get(SPAN_PARAM);
  return {
    scope: parseScope(params.get(SCOPE_PARAM)),
    project: parseProject(params.get(PROJECT_PARAM)),
    weekStart: localDate(params.get(WEEK_PARAM)),
    view: VIEWS.find((candidate) => candidate === view) ?? DEFAULT_TODO_SELECTION.view,
    progressSpan:
      SPANS.find((candidate) => candidate === span) ?? DEFAULT_TODO_SELECTION.progressSpan,
    progressOn: localDate(params.get(ON_PARAM)),
    progressDay: localDate(params.get(DAY_PARAM)),
    todoId: canonicalId(params.get(TODO_PARAM)),
  };
}

/** The query for a selection; defaults are left out, so the default selection is ''. */
export function todoSelectionSearch(selection: TodoSelection): string {
  // A query may hold `:` as is, so the kind's separator stays readable; ids are encoded.
  const entries: string[] = [];
  const { scope, project } = selection;
  if (scope.scope === 'no_organization') entries.push(`${SCOPE_PARAM}=${NONE}`);
  if (scope.scope === 'organization' && scope.organization_id !== undefined) {
    entries.push(
      `${SCOPE_PARAM}=${ORGANIZATION_PREFIX}${encodeURIComponent(scope.organization_id)}`
    );
  }
  if (project.project === 'no_project') entries.push(`${PROJECT_PARAM}=${NONE}`);
  if (project.project === 'project' && project.project_id !== undefined) {
    entries.push(`${PROJECT_PARAM}=${PROJECT_PREFIX}${encodeURIComponent(project.project_id)}`);
  }
  if (selection.weekStart !== null) entries.push(`${WEEK_PARAM}=${selection.weekStart}`);
  if (selection.view !== DEFAULT_TODO_SELECTION.view) {
    entries.push(`${VIEW_PARAM}=${selection.view}`);
  }
  if (selection.progressSpan !== DEFAULT_TODO_SELECTION.progressSpan) {
    entries.push(`${SPAN_PARAM}=${selection.progressSpan}`);
  }
  if (selection.progressOn !== null) entries.push(`${ON_PARAM}=${selection.progressOn}`);
  if (selection.progressDay !== null) entries.push(`${DAY_PARAM}=${selection.progressDay}`);
  if (selection.todoId !== null) {
    entries.push(`${TODO_PARAM}=${encodeURIComponent(selection.todoId)}`);
  }
  return entries.length === 0 ? '' : `?${entries.join('&')}`;
}
