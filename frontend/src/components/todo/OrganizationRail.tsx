import { LayoutGrid } from 'lucide-react';
import type { ReactNode } from 'react';

import { useI18n } from '@/context/useI18n';
import type { WorkspaceOrganization } from '@/pages/settings/components/workspaceSettingsModel';

import type { TodoScopeQuery } from '../../../electron/src/todos/todoContracts';

import './todo.css';

const sameScope = (a: TodoScopeQuery, b: TodoScopeQuery) =>
  a.scope === b.scope && a.organization_id === b.organization_id;

/**
 * The organizations to see TODOs for, as entries of the app's left rail: all of them, each one by
 * its initial, and the projects in none.
 */
export function OrganizationRail({
  organizations,
  selected,
  onSelect,
}: {
  organizations: readonly WorkspaceOrganization[];
  selected: TodoScopeQuery;
  onSelect: (scope: TodoScopeQuery) => void;
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
      mark: Array.from(organization.display_name)[0].toUpperCase(),
    })),
    {
      key: 'none',
      scope: { scope: 'no_organization' },
      label: t('todo.rail.none'),
      mark: <span className="todo-rail__none">{t('todo.rail.noneInitial')}</span>,
    },
  ];
  return (
    <div className="todo-rail" role="group" aria-label={t('todo.rail.label')}>
      {entries.map(({ key, scope, label, mark }) => {
        const pressed = sameScope(scope, selected);
        return (
          <button
            key={key}
            type="button"
            className={
              pressed
                ? 'app-rail-item todo-rail__entry app-rail-item--active'
                : 'app-rail-item todo-rail__entry'
            }
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
  );
}
