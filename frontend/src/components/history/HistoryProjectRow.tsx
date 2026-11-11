import { useSortable } from '@dnd-kit/sortable';
import { CSS } from '@dnd-kit/utilities';
import { ChevronDown, ChevronRight, GripVertical } from 'lucide-react';
import { useState } from 'react';

import { LineTextField } from '@/components/ui/LineTextField';
import type { useI18n } from '@/context/useI18n';
import type {
  WorkspaceFolder,
  WorkspaceOrganization,
  WorkspaceProject,
} from '@/pages/settings/components/workspaceSettingsModel';

import { HistoryProjectMenu, type HistoryProjectMenuItem } from './HistoryProjectMenu';
import { ProjectFolders } from './ProjectFolders';

const projectNameId = (projectId: string) => `history-project:${projectId}`;
const openCountId = (projectId: string) => `history-project-open:${projectId}`;
/** Past this the badge shows "99+"; the description still says the number. */
const MAX_SHOWN_OPEN = 99;

const focusById = (id: string) => document.getElementById(id)?.focus();

/** What the TODO list adds to a project's row. */
export type ProjectRowTodo = {
  /** Its TODOs are the ones in view. */
  selected: boolean;
  /** The open TODOs of the scope in view; none shows no badge. */
  openCount: number;
  onSelect: () => void;
};

/**
 * One project in the sidebar. With the TODO list its name selects its TODOs, with how many are
 * open beside it, and its folders are on the project's page; without it the name, chevron and
 * all, toggles its folders and organization, as before the TODO list. Its 「…」 menu holds the
 * actions, and it is dragged by the row (or moved from the keyboard by its handle).
 */
export function HistoryProjectRow({
  project,
  folders,
  organizations,
  menuItems,
  menuButtonId,
  renaming,
  dragDisabled,
  todo,
  t,
  onRename,
  onRenameEnd,
  onRemoveFolder,
  isFolderPending,
  onAddFolder,
  onOpenFolder,
  justCreated,
}: {
  project: WorkspaceProject;
  folders: WorkspaceFolder[];
  organizations: WorkspaceOrganization[];
  menuItems: HistoryProjectMenuItem[];
  menuButtonId: string;
  renaming: boolean;
  dragDisabled: boolean;
  /** Null without the TODO list. */
  todo: ProjectRowTodo | null;
  t: ReturnType<typeof useI18n>['t'];
  onRename: (displayName: string) => Promise<boolean>;
  /** False when another row's rename has begun since, which keeps its field and its focus. */
  onRenameEnd: () => boolean;
  onRemoveFolder: (folderId: string) => Promise<boolean>;
  /** A folder being added or removed elsewhere cannot be removed until that settles. */
  isFolderPending: (folderId: string) => boolean;
  onOpenFolder: (folderId: string) => void;
  onAddFolder: () => void;
  /**
   * A project just created takes focus from the name field: its name with the TODO list, or
   * without it its open folder list's ＋.
   */
  justCreated: boolean;
}) {
  const [expanded, setExpanded] = useState(justCreated);
  const { attributes, listeners, setNodeRef, setActivatorNodeRef, transform, transition } =
    useSortable({ id: project.project_id, disabled: dragDisabled });
  const detailsId = `history-project-details:${project.project_id}`;
  const openCount = todo?.openCount ?? 0;

  const endRename = () => {
    if (onRenameEnd()) focusById(projectNameId(project.project_id));
  };

  return (
    <li
      ref={setNodeRef}
      className="history-item history-project"
      data-project-id={project.project_id}
      style={{ transform: CSS.Translate.toString(transform), transition }}
      // A pointer drags the whole row; the keyboard picks it up from the handle, so Enter and
      // Space on the name still press it.
      onPointerDown={(event) => listeners?.onPointerDown?.(event)}
    >
      <button
        type="button"
        ref={setActivatorNodeRef}
        className="history-project__handle"
        disabled={dragDisabled}
        aria-label={t('settings.workspace.drag.handle', { name: project.display_name })}
        data-tooltip={t('settings.workspace.drag.handle', { name: project.display_name })}
        {...attributes}
        onKeyDown={(event) => listeners?.onKeyDown?.(event)}
      >
        <GripVertical size={13} aria-hidden="true" />
      </button>
      {renaming ? (
        <ProjectNameField
          initialName={project.display_name}
          label={t('history.projects.nameLabel')}
          saveOnBlur
          onSave={async (name) => {
            if (await onRename(name)) endRename();
          }}
          onCancel={endRename}
        />
      ) : todo ? (
        <button
          type="button"
          id={projectNameId(project.project_id)}
          className="history-project__select"
          aria-current={todo.selected ? 'true' : undefined}
          aria-describedby={openCount > 0 ? openCountId(project.project_id) : undefined}
          // Focus follows a new project from the name field.
          autoFocus={justCreated}
          onClick={todo.onSelect}
        >
          <span className="history-project__name">{project.display_name}</span>
          {openCount > 0 ? (
            <span className="history-project__count" aria-hidden="true">
              {openCount > MAX_SHOWN_OPEN ? `${MAX_SHOWN_OPEN}+` : openCount}
            </span>
          ) : null}
        </button>
      ) : (
        <button
          type="button"
          id={projectNameId(project.project_id)}
          className="history-project__name-toggle"
          aria-expanded={expanded}
          aria-controls={detailsId}
          onClick={() => setExpanded((open) => !open)}
        >
          {expanded ? (
            <ChevronDown size={13} aria-hidden="true" />
          ) : (
            <ChevronRight size={13} aria-hidden="true" />
          )}
          <span className="history-project__name">{project.display_name}</span>
        </button>
      )}
      {openCount > 0 ? (
        <span id={openCountId(project.project_id)} hidden>
          {t('history.projects.openCount', { count: openCount })}
        </span>
      ) : null}
      <HistoryProjectMenu
        label={t('history.projects.menu', { name: project.display_name })}
        buttonId={menuButtonId}
        items={menuItems}
      />
      {todo ? null : (
        <div id={detailsId} className="history-project__details" hidden={!expanded}>
          <ProjectFolders
            projectId={project.project_id}
            folders={folders}
            isFolderPending={isFolderPending}
            onOpenFolder={onOpenFolder}
            onRemoveFolder={onRemoveFolder}
            onAddFolder={onAddFolder}
            autoFocusAdd={justCreated}
            t={t}
          />
          {organizations.length > 0 ? (
            <p className="history-project__organization">
              {t('history.projects.organization')}:{' '}
              {organizations.map((organization) => organization.display_name).join(', ')}
            </p>
          ) : null}
        </div>
      )}
    </li>
  );
}

/**
 * A project's name, being renamed or created. Enter saves and Escape cancels; leaving the field
 * saves a rename, as a file name field does, and cancels a new name left empty.
 */
export function ProjectNameField({
  initialName,
  label,
  saveOnBlur,
  onSave,
  onCancel,
}: {
  initialName: string;
  label: string;
  saveOnBlur: boolean;
  onSave: (name: string) => Promise<void>;
  onCancel: () => void;
}) {
  return (
    <LineTextField
      className="history-project__rename"
      label={label}
      initialText={initialName}
      // Opened from ＋ or the menu on purpose, so taking focus here is what the user asked for.
      autoFocus
      submitOnBlur={saveOnBlur}
      onSubmit={async (name) => {
        await onSave(name);
        // A saved name closes the field, and one that was not saved stays to be fixed.
        return false;
      }}
      onCancel={onCancel}
    />
  );
}
