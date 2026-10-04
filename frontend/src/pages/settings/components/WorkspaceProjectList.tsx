import { DndContext } from '@dnd-kit/core';
import { SortableContext, useSortable, verticalListSortingStrategy } from '@dnd-kit/sortable';
import { CSS } from '@dnd-kit/utilities';
import { GripVertical } from 'lucide-react';

import type { Translate } from '../types';
import type { WorkspaceProjectDragController } from '../useWorkspaceProjectDragController';
import type { WorkspaceFolder, WorkspaceProject } from './workspaceSettingsModel';

interface WorkspaceProjectListProps {
  disabled: boolean;
  dragController: WorkspaceProjectDragController;
  folders: WorkspaceFolder[];
  projects: WorkspaceProject[];
  selectedProjectId: string | null;
  t: Translate;
  onSelect: (projectId: string) => void;
}

/** The project list in the master column: select a project, or drag it into a new order. */
export function WorkspaceProjectList(props: WorkspaceProjectListProps) {
  return (
    <DndContext
      sensors={props.dragController.sensors}
      accessibility={props.dragController.accessibility}
      onDragStart={props.dragController.onDragStart}
      onDragOver={props.dragController.onDragOver}
      onDragEnd={props.dragController.onDragEnd}
      onDragCancel={props.dragController.onDragCancel}
    >
      <SortableContext
        items={props.projects.map((project) => project.project_id)}
        strategy={verticalListSortingStrategy}
      >
        <div
          className="workspace-project-list"
          aria-label={props.t('settings.workspace.structureAriaLabel')}
        >
          {props.projects.map((project) => (
            <SortableProjectItem
              key={project.project_id}
              project={project}
              folderCount={
                props.folders.filter((folder) => folder.project_ids.includes(project.project_id))
                  .length
              }
              disabled={props.disabled}
              isSelected={project.project_id === props.selectedProjectId}
              t={props.t}
              onSelect={props.onSelect}
            />
          ))}
        </div>
      </SortableContext>
    </DndContext>
  );
}

function SortableProjectItem(props: {
  disabled: boolean;
  folderCount: number;
  isSelected: boolean;
  project: WorkspaceProject;
  t: Translate;
  onSelect: (projectId: string) => void;
}) {
  const { attributes, listeners, setNodeRef, transform, transition, isDragging } = useSortable({
    id: props.project.project_id,
    disabled: props.disabled,
  });

  return (
    <div
      ref={setNodeRef}
      className={[
        'workspace-master-item',
        props.isSelected ? 'is-selected' : null,
        isDragging ? 'is-dragging' : null,
      ]
        .filter(Boolean)
        .join(' ')}
      data-project-id={props.project.project_id}
      style={{ transform: CSS.Transform.toString(transform), transition }}
    >
      <button
        type="button"
        className="workspace-project-drag-handle"
        disabled={props.disabled}
        aria-label={props.t('settings.workspace.drag.handle', {
          name: props.project.display_name,
        })}
        {...attributes}
        {...listeners}
      >
        <GripVertical size={14} aria-hidden="true" />
      </button>
      <button
        type="button"
        className="workspace-master-select"
        aria-current={props.isSelected ? 'true' : undefined}
        onClick={() => props.onSelect(props.project.project_id)}
      >
        {props.project.display_name}
      </button>
      <span
        className={`workspace-project-folder-count${props.folderCount === 0 ? ' is-empty' : ''}`}
      >
        {props.t('settings.workspace.folderCount', { count: props.folderCount })}
      </span>
    </div>
  );
}
