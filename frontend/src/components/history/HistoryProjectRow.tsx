import { useSortable } from '@dnd-kit/sortable';
import { CSS } from '@dnd-kit/utilities';
import { ChevronDown, ChevronRight, FolderOpen, GripVertical, X } from 'lucide-react';
import { useState } from 'react';

import { LineTextField } from '@/components/ui/LineTextField';
import type { useI18n } from '@/context/useI18n';
import {
  splitPathForMiddleEllipsis,
  type WorkspaceFolder,
  type WorkspaceOrganization,
  type WorkspaceProject,
} from '@/pages/settings/components/workspaceSettingsModel';

import { HistoryProjectMenu, type HistoryProjectMenuItem } from './HistoryProjectMenu';

const projectNameId = (projectId: string) => `history-project:${projectId}`;
const projectToggleId = (projectId: string) => `history-project-toggle:${projectId}`;
const openCountId = (projectId: string) => `history-project-open:${projectId}`;
/** Past this the badge shows "99+"; the description still says the number. */
const MAX_SHOWN_OPEN = 99;
const folderRemoveId = (projectId: string, folderId: string) =>
  `history-project-folder-remove:${projectId}:${folderId}`;

const focusById = (id: string) => document.getElementById(id)?.focus();

/** What the TODO list adds to a project's row. */
export type ProjectRowTodo = {
  /** Its TODOs are the ones in view. */
  selected: boolean;
  /** The open TODOs of the week in view; none shows no badge. */
  openCount: number;
  onSelect: () => void;
};

/**
 * One project in the sidebar. With the TODO list its name selects its TODOs, with how many are
 * open beside it, and the chevron before it toggles its folders and organization; without it the
 * name, chevron and all, toggles them, as before the TODO list. Its 「…」 menu holds the actions,
 * and it is dragged by the row (or moved from the keyboard by its handle).
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
  initiallyExpanded,
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
  /** A project just created opens on its empty folder list, with its add button focused. */
  initiallyExpanded: boolean;
}) {
  const [expanded, setExpanded] = useState(initiallyExpanded);
  // The folder whose whole path is shown, wrapped, instead of cut to the line.
  const [fullPathId, setFullPathId] = useState<string | null>(null);
  const { attributes, listeners, setNodeRef, setActivatorNodeRef, transform, transition } =
    useSortable({ id: project.project_id, disabled: dragDisabled });
  const detailsId = `history-project-details:${project.project_id}`;
  const projectFolderIds = folders.map((folder) => folder.folder_id);
  // What opens and closes the folders: the chevron, or without the TODO list the name.
  const toggleId = todo ? projectToggleId(project.project_id) : projectNameId(project.project_id);
  const toggleIcon = expanded ? (
    <ChevronDown size={13} aria-hidden="true" />
  ) : (
    <ChevronRight size={13} aria-hidden="true" />
  );
  const toggle = () => setExpanded((open) => !open);
  const openCount = todo?.openCount ?? 0;

  const endRename = () => {
    if (onRenameEnd()) focusById(projectNameId(project.project_id));
  };
  const removeFolder = async (folderId: string) => {
    if (!(await onRemoveFolder(folderId))) return;
    const remaining = projectFolderIds.filter((id) => id !== folderId);
    const next = remaining[Math.min(projectFolderIds.indexOf(folderId), remaining.length - 1)];
    focusById(next ? folderRemoveId(project.project_id, next) : toggleId);
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
      {todo ? (
        <button
          type="button"
          id={toggleId}
          className="history-project__toggle"
          aria-label={t('history.projects.toggleFolders', { name: project.display_name })}
          aria-expanded={expanded}
          aria-controls={detailsId}
          onClick={toggle}
        >
          {toggleIcon}
        </button>
      ) : null}
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
          id={toggleId}
          className="history-project__name-toggle"
          aria-expanded={expanded}
          aria-controls={detailsId}
          onClick={toggle}
        >
          {toggleIcon}
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
      <div id={detailsId} className="history-project__details" hidden={!expanded}>
        <ul aria-label={t('history.projects.folders')}>
          {folders.map((folder) => {
            const [pathStart, pathEnd] = splitPathForMiddleEllipsis(folder.real_path);
            return (
              <li key={folder.folder_id} className="history-project__folder">
                <button
                  type="button"
                  className="history-project__path"
                  // The two halves are drawn apart; the whole path is what the button is.
                  aria-label={folder.real_path}
                  title={folder.real_path}
                  aria-expanded={fullPathId === folder.folder_id}
                  onClick={() =>
                    setFullPathId((shown) => (shown === folder.folder_id ? null : folder.folder_id))
                  }
                >
                  <span className="history-project__path-start">{pathStart}</span>
                  <span className="history-project__path-end">{pathEnd}</span>
                </button>
                <button
                  type="button"
                  className="history-project__folder-action"
                  aria-label={t('history.projects.openInFinder')}
                  data-tooltip={t('history.projects.openInFinder')}
                  onClick={() => onOpenFolder(folder.folder_id)}
                >
                  <FolderOpen size={13} aria-hidden="true" />
                </button>
                <button
                  type="button"
                  id={folderRemoveId(project.project_id, folder.folder_id)}
                  className="history-project__folder-action"
                  aria-label={t('history.projects.removeFolder', { name: folder.display_name })}
                  data-tooltip={t('history.projects.removeFolder', { name: folder.display_name })}
                  aria-busy={isFolderPending(folder.folder_id)}
                  disabled={isFolderPending(folder.folder_id)}
                  onClick={() => void removeFolder(folder.folder_id)}
                >
                  <X size={13} aria-hidden="true" />
                </button>
              </li>
            );
          })}
        </ul>
        {folders.length === 0 ? (
          <button
            type="button"
            className="history-filter-button history-project__add-folder"
            // Only a project just created opens here; focus follows it from the name field.
            autoFocus={initiallyExpanded}
            onClick={onAddFolder}
          >
            {t('history.projects.addFolder')}
          </button>
        ) : null}
        {organizations.length > 0 ? (
          <p className="history-project__organization">
            {t('history.projects.organization')}:{' '}
            {organizations.map((organization) => organization.display_name).join(', ')}
          </p>
        ) : null}
      </div>
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
