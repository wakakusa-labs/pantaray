import { ChartNoAxesColumn, ListTodo, Pencil } from 'lucide-react';
import type { ReactNode } from 'react';

import { useI18n } from '@/context/useI18n';

import type { TodoScopeQuery } from '../../../electron/src/todos/todoContracts';

/** The title's edit button, where focus returns once the organization's dialog closes. */
export const ORGANIZATION_EDIT_BUTTON_ID = 'history-sidebar-organization-edit';

/** Which TODO list the main area shows. Progress is of the one project in view, or of none. */
export type TodoListPlace =
  | { kind: 'all' }
  | { kind: 'project'; projectId: string }
  | { kind: 'progress'; projectId: string | null }
  | { kind: 'deleted' };

/** The sidebar's entries into the TODO list. */
export type TodoSidebarNav = {
  /** Null while the main area shows the chat or a conversation instead. */
  current: TodoListPlace | null;
  /** The rail's scope in view. */
  scope: TodoScopeQuery;
  /** The rail's scope in view, by name; null while its organization is not read yet. */
  scopeName: string | null;
  /** Renames the organization in view or changes its icon; null when no organization is. */
  onEditOrganization: (() => void) | null;
  /** Open TODOs by project id, for the week in view. */
  openCounts: ReadonlyMap<string, number>;
  onShowAll: () => void;
  onShowProgress: () => void;
  onShowDeleted: () => void;
  onSelectProject: (projectId: string) => void;
};

/** One of the sidebar's plain entries: an icon and a label, current while it is shown. */
function SidebarEntry({
  icon,
  label,
  current,
  onShow,
}: {
  icon: ReactNode;
  label: string;
  current: boolean;
  onShow: () => void;
}) {
  return (
    <button
      type="button"
      className="history-chat-row"
      aria-current={current ? 'true' : undefined}
      onClick={onShow}
    >
      {icon}
      <span className="history-chat-row__label">{label}</span>
    </button>
  );
}

/** The scope in view by name, as the sidebar's title; an organization's can be edited. */
export function TodoScopeTitle({ nav }: { nav: TodoSidebarNav }) {
  const { t } = useI18n();
  if (nav.scopeName === null) return null;
  const editLabel = t('todo.organization.edit', { name: nav.scopeName });
  return (
    <div className="history-sidebar__scope-row">
      <h2 className="history-sidebar__scope">{nav.scopeName}</h2>
      {nav.onEditOrganization ? (
        <button
          type="button"
          id={ORGANIZATION_EDIT_BUTTON_ID}
          className="history-projects__add"
          aria-label={editLabel}
          data-tooltip={editLabel}
          onClick={nav.onEditOrganization}
        >
          <Pencil size={14} aria-hidden="true" />
        </button>
      ) : null}
    </div>
  );
}

/** All of the scope's TODOs and their progress, above the projects. */
export function TodoSidebarEntries({ nav }: { nav: TodoSidebarNav }) {
  const { t } = useI18n();
  const place = nav.current;
  return (
    <div className="history-sidebar__chat">
      <SidebarEntry
        icon={<ListTodo size={16} strokeWidth={1.8} aria-hidden="true" />}
        label={t('todo.nav.all')}
        current={place?.kind === 'all'}
        onShow={nav.onShowAll}
      />
      <SidebarEntry
        icon={<ChartNoAxesColumn size={16} strokeWidth={1.8} aria-hidden="true" />}
        label={t('todo.nav.progress')}
        current={place?.kind === 'progress'}
        onShow={nav.onShowProgress}
      />
    </div>
  );
}

/** The deleted TODOs: rarely visited, so a quiet link at the sidebar's foot. */
export function DeletedTodosLink({ nav }: { nav: TodoSidebarNav }) {
  const { t } = useI18n();
  return (
    <div className="history-sidebar__foot">
      <button
        type="button"
        className="history-sidebar__deleted"
        aria-current={nav.current?.kind === 'deleted' ? 'true' : undefined}
        onClick={nav.onShowDeleted}
      >
        {t('todo.nav.deleted')}
      </button>
    </div>
  );
}
