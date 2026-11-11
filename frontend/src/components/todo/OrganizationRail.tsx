import { LayoutGrid, Plus } from 'lucide-react';
import type { ReactNode } from 'react';

import { useI18n } from '@/context/useI18n';
import type { WorkspaceOrganization } from '@/pages/settings/components/workspaceSettingsModel';

import type { TodoScopeQuery } from '../../../electron/src/todos/todoContracts';

import './todo.css';

/** The rail's ＋, where focus returns once the new organization's dialog closes. */
export const ORGANIZATION_ADD_BUTTON_ID = 'app-rail-organization-add';

const sameScope = (a: TodoScopeQuery, b: TodoScopeQuery) =>
  a.scope === b.scope && a.organization_id === b.organization_id;

/**
 * The organizations to see TODOs for, as entries of the app's left rail: all of them, each one by
 * its icon or initial, and the projects in none; then ＋ to add one.
 */
export function OrganizationRail({
  organizations,
  selected,
  onSelect,
  onAdd,
}: {
  organizations: readonly WorkspaceOrganization[];
  selected: TodoScopeQuery;
  onSelect: (scope: TodoScopeQuery) => void;
  onAdd: () => void;
}) {
  const { t } = useI18n();
  const entries: { key: string; scope: TodoScopeQuery; label: string; mark: ReactNode }[] = [
    {
      key: 'all',
      scope: { scope: 'all' },
      label: t('todo.rail.all'),
      mark: <LayoutGrid size={18} strokeWidth={1.6} aria-hidden="true" />,
    },
    ...organizations.map((organization) => ({
      key: organization.organization_id,
      scope: { scope: 'organization' as const, organization_id: organization.organization_id },
      label: organization.display_name,
      mark: organization.icon_data_url ? (
        <img className="todo-rail__icon" src={organization.icon_data_url} alt="" />
      ) : (
        Array.from(organization.display_name)[0].toUpperCase()
      ),
    })),
    {
      key: 'none',
      scope: { scope: 'no_organization' },
      label: t('todo.rail.none'),
      mark: <span className="todo-rail__none">{t('todo.rail.noneInitial')}</span>,
    },
  ];
  return (
    <>
      <div className="todo-rail" role="group" aria-label={t('todo.rail.label')}>
        {entries.map(({ key, scope, label, mark }) => {
          const pressed = sameScope(scope, selected);
          return (
            <button
              key={key}
              type="button"
              className="app-rail-item todo-rail__entry"
              aria-pressed={pressed}
              aria-label={label}
              data-tooltip={label}
              onClick={() => onSelect(scope)}
            >
              {mark}
            </button>
          );
        })}
      </div>
      <button
        type="button"
        id={ORGANIZATION_ADD_BUTTON_ID}
        className="app-rail-item todo-rail__add"
        aria-label={t('todo.rail.add')}
        data-tooltip={t('todo.rail.add')}
        onClick={onAdd}
      >
        <Plus size={18} strokeWidth={1.6} aria-hidden="true" />
      </button>
    </>
  );
}
