import { useSortable } from '@dnd-kit/sortable';
import { CSS } from '@dnd-kit/utilities';
import { ChevronDown, ChevronRight, GripVertical, X } from 'lucide-react';
import { useRef, useState } from 'react';

import type { useI18n } from '@/context/useI18n';
import {
  splitPathForMiddleEllipsis,
  type WorkspaceFolder,
  type WorkspaceOrganization,
  type WorkspaceProject,
} from '@/pages/settings/components/workspaceSettingsModel';

import { HistoryProjectMenu, type HistoryProjectMenuItem } from './HistoryProjectMenu';

const projectToggleId = (projectId: string) => `history-project:${projectId}`;
const folderRemoveId = (projectId: string, folderId: string) =>
  `history-project-folder-remove:${projectId}:${folderId}`;

const focusById = (id: string) => document.getElementById(id)?.focus();

/**
 * One project in the sidebar: its name toggles its folders and organization, its 「…」 menu
 * holds the actions, and it is dragged by the row (or moved from the keyboard by its handle).
 */
export function HistoryProjectRow({
  project,
  folders,
  organizations,
  menuItems,
  menuButtonId,
  renaming,
  dragDisabled,
  t,
  onRename,
  onRenameEnd,
  onRemoveFolder,
  isFolderPending,
  onAddFolder,
  initiallyExpanded,
}: {
  project: WorkspaceProject;
  folders: WorkspaceFolder[];
  organizations: WorkspaceOrganization[];
  menuItems: HistoryProjectMenuItem[];
  menuButtonId: string;
  renaming: boolean;
  dragDisabled: boolean;
  t: ReturnType<typeof useI18n>['t'];
  onRename: (displayName: string) => Promise<boolean>;
  /** False when another row's rename has begun since, which keeps its field and its focus. */
  onRenameEnd: () => boolean;
  onRemoveFolder: (folderId: string) => Promise<boolean>;
  /** A folder being added or removed elsewhere cannot be removed until that settles. */
  isFolderPending: (folderId: string) => boolean;
  onAddFolder: () => void;
  /** A project just created opens on its empty folder list, with its add button focused. */
  initiallyExpanded: boolean;
}) {
  const [expanded, setExpanded] = useState(initiallyExpanded);
  const { attributes, listeners, setNodeRef, setActivatorNodeRef, transform, transition } =
    useSortable({ id: project.project_id, disabled: dragDisabled });
  const detailsId = `history-project-details:${project.project_id}`;
  const projectFolderIds = folders.map((folder) => folder.folder_id);

  const endRename = () => {
    if (onRenameEnd()) focusById(projectToggleId(project.project_id));
  };
  const removeFolder = async (folderId: string) => {
    if (!(await onRemoveFolder(folderId))) return;
    const remaining = projectFolderIds.filter((id) => id !== folderId);
    const next = remaining[Math.min(projectFolderIds.indexOf(folderId), remaining.length - 1)];
    focusById(
      next ? folderRemoveId(project.project_id, next) : projectToggleId(project.project_id)
    );
  };

  return (
    <li
      ref={setNodeRef}
      className="history-item history-project"
      data-project-id={project.project_id}
      style={{ transform: CSS.Translate.toString(transform), transition }}
      // A pointer drags the whole row; the keyboard picks it up from the handle, so Enter and
      // Space on the name still open and close it.
      onPointerDown={(event) => listeners?.onPointerDown?.(event)}
    >
      <button
        type="button"
        ref={setActivatorNodeRef}
        className="history-project__handle"
        disabled={dragDisabled}
        aria-label={t('settings.workspace.drag.handle', { name: project.display_name })}
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
      ) : (
        <button
          type="button"
          id={projectToggleId(project.project_id)}
          className="history-project__toggle"
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
                <span className="history-project__path" title={folder.real_path}>
                  <span className="history-project__path-start">{pathStart}</span>
                  <span className="history-project__path-end">{pathEnd}</span>
                </span>
                <button
                  type="button"
                  id={folderRemoveId(project.project_id, folder.folder_id)}
                  className="history-project__folder-remove"
                  aria-label={t('history.projects.removeFolder', { name: folder.display_name })}
                  title={t('history.projects.removeFolder', { name: folder.display_name })}
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

/** The keyCode of a key the IME processed, sent for the Enter that ends a composition. */
const IME_PROCESS_KEY_CODE = 229;

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
  const [name, setName] = useState(initialName);
  // Set once Enter or Escape has decided, so the blur that follows does not save again.
  const settledRef = useRef(false);
  const save = async () => {
    if (settledRef.current) return;
    settledRef.current = true;
    await onSave(name);
    settledRef.current = false;
  };
  return (
    <input
      type="text"
      className="history-project__rename"
      aria-label={label}
      placeholder={label}
      value={name}
      // Opened from ＋ or the menu on purpose, so taking focus here is what the user asked for.
      autoFocus
      onFocus={(event) => event.currentTarget.select()}
      onChange={(event) => setName(event.target.value)}
      // Selecting text with the pointer must not pick up the row.
      onPointerDown={(event) => event.stopPropagation()}
      onBlur={() => {
        if (saveOnBlur) void save();
        else if (!name.trim() && !settledRef.current) onCancel();
      }}
      onKeyDown={(event) => {
        // Enter and Escape while an IME is composing belong to the IME.
        if (event.nativeEvent.isComposing || event.keyCode === IME_PROCESS_KEY_CODE) return;
        if (event.key === 'Enter') {
          event.preventDefault();
          void save();
        } else if (event.key === 'Escape') {
          event.preventDefault();
          settledRef.current = true;
          onCancel();
        }
      }}
    />
  );
}
