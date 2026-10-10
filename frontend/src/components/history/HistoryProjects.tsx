import { Plus, Trash2 } from 'lucide-react';
import { useRef, useState } from 'react';
import { flushSync } from 'react-dom';

import type { useI18n } from '@/context/useI18n';
import {
  workspacePendingKey,
  type useWorkspaceSettingsController,
} from '@/pages/settings/useWorkspaceSettingsController';
import type { WorkspaceProject } from '@/pages/settings/components/workspaceSettingsModel';

import { HistoryDeleteDialog } from './HistoryDeleteDialog';

export type WorkspaceProjects = Pick<
  ReturnType<typeof useWorkspaceSettingsController>,
  'settings' | 'errorMessage' | 'pending' | 'addProjectFromFolder' | 'removeProject'
>;

const deleteButtonId = (projectId: string) => `history-project-delete:${projectId}`;

/**
 * The sidebar's projects: the workspace's registered folders, added with ＋ and removed per row.
 * They do not filter or group the tasks below.
 */
export function HistoryProjects({
  projects,
  t,
}: {
  projects: WorkspaceProjects;
  t: ReturnType<typeof useI18n>['t'];
}) {
  const { settings, errorMessage, pending, addProjectFromFolder, removeProject } = projects;
  const addButtonRef = useRef<HTMLButtonElement>(null);
  const [confirming, setConfirming] = useState<WorkspaceProject | null>(null);
  const focusDeleteButton = (projectId: string) =>
    document.getElementById(deleteButtonId(projectId))?.focus();

  const folderPaths = (projectId: string) =>
    (settings?.folders ?? [])
      .filter((folder) => folder.project_ids.includes(projectId))
      .map((folder) => folder.real_path)
      .join('\n');

  // The modal dialog holds focus until it is gone, so it closes before focus moves.
  const cancelDelete = (project: WorkspaceProject) => {
    flushSync(() => setConfirming(null));
    focusDeleteButton(project.project_id);
  };
  const confirmDelete = async (project: WorkspaceProject) => {
    setConfirming(null);
    if (await removeProject(project.project_id)) addButtonRef.current?.focus();
    else focusDeleteButton(project.project_id);
  };

  return (
    <section className="history-projects" aria-labelledby="history-projects-title">
      <div className="history-projects__head">
        <h2 id="history-projects-title">{t('history.projects.title')}</h2>
        <button
          type="button"
          ref={addButtonRef}
          className="history-projects__add"
          aria-label={t('history.projects.add')}
          title={t('history.projects.add')}
          aria-busy={pending.has(workspacePendingKey.projectCreate)}
          disabled={settings === null}
          onClick={() => void addProjectFromFolder()}
        >
          <Plus size={15} aria-hidden="true" />
        </button>
      </div>
      {errorMessage ? (
        <div className="history-error" role="alert">
          {errorMessage}
        </div>
      ) : null}
      {settings && settings.projects.length > 0 ? (
        <ul className="history-projects__list">
          {settings.projects.map((project) => (
            <li key={project.project_id} className="history-item history-project">
              <span className="history-project__name" title={folderPaths(project.project_id)}>
                {project.display_name}
              </span>
              <button
                type="button"
                id={deleteButtonId(project.project_id)}
                className="history-item-delete"
                aria-label={`${t('common.delete')} ${project.display_name}`}
                title={t('common.delete')}
                aria-busy={pending.has(workspacePendingKey.projectDelete(project.project_id))}
                onClick={() => setConfirming(project)}
              >
                <Trash2 size={15} aria-hidden="true" />
              </button>
            </li>
          ))}
        </ul>
      ) : null}
      {confirming ? (
        <HistoryDeleteDialog
          t={t}
          title={t('history.projects.deleteConfirmTitle', { name: confirming.display_name })}
          body={t('history.projects.deleteConfirmBody')}
          onCancel={() => cancelDelete(confirming)}
          onConfirm={() => void confirmDelete(confirming)}
        />
      ) : null}
    </section>
  );
}
