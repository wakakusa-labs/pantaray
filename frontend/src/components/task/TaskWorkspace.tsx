import { useState } from 'react';

import { OpenFileLinkContext } from '@/components/agent-overlay/openFileLinkContext';

import { ActionTaskPane } from './ActionTaskPane';
import { FileChips } from './FileChips';
import { FilePreviewPane } from './FilePreviewPane';
import type { TaskComposerDrafts } from './taskComposerDrafts';
import { deriveTaskFiles, latestCompletion, taskFileAt, type TaskFile } from './taskFiles';

type TaskWorkspaceProps = {
  /** The workspace holds one Action's preview, so the page keys it by this id. */
  actionId: string;
  title: string;
  onShowInChat: () => void;
  onAddProject: () => void;
  drafts: TaskComposerDrafts;
};

/**
 * One Action with the documents it produced: the conversation alone, or a document's preview
 * on the left with the conversation narrowed to a column on the right. A file link in an answer
 * opens its preview, as its chip does.
 */
export function TaskWorkspace({
  actionId,
  title,
  onShowInChat,
  onAddProject,
  drafts,
}: TaskWorkspaceProps) {
  const [previewFile, setPreviewFile] = useState<TaskFile | null>(null);
  const pane = (
    <ActionTaskPane
      actionId={actionId}
      title={title}
      onShowInChat={onShowInChat}
      onAddProject={onAddProject}
      drafts={drafts}
      renderPreview={(view) =>
        previewFile ? (
          <FilePreviewPane
            key={previewFile.path}
            actionId={actionId}
            file={previewFile}
            revision={view ? latestCompletion(view) : null}
            onClose={() => setPreviewFile(null)}
          />
        ) : null
      }
      renderFileChips={(view) => (
        <FileChips
          files={deriveTaskFiles(view)}
          selectedPath={previewFile?.path ?? null}
          onToggle={(file) => setPreviewFile(file.path === previewFile?.path ? null : file)}
        />
      )}
    />
  );
  return (
    <OpenFileLinkContext.Provider value={(path) => setPreviewFile(taskFileAt(path))}>
      {pane}
    </OpenFileLinkContext.Provider>
  );
}
