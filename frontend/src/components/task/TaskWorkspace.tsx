import { useState } from 'react';

import { ActionTaskPane } from './ActionTaskPane';
import { FileChips } from './FileChips';
import { FilePreviewPane } from './FilePreviewPane';
import { deriveTaskFiles, type TaskFile } from './taskFiles';

type TaskWorkspaceProps = {
  /** The workspace holds one Action's preview, so the page keys it by this id. */
  actionId: string;
  title: string;
  onShowInChat: () => void;
  onAddProject: () => void;
};

/**
 * One Action with the documents it produced: the conversation alone, or a document's preview
 * on the left with the conversation narrowed to a column on the right.
 */
export function TaskWorkspace({ actionId, title, onShowInChat, onAddProject }: TaskWorkspaceProps) {
  const [previewFile, setPreviewFile] = useState<TaskFile | null>(null);
  return (
    <ActionTaskPane
      actionId={actionId}
      title={title}
      onShowInChat={onShowInChat}
      onAddProject={onAddProject}
      preview={
        previewFile ? (
          <FilePreviewPane
            key={previewFile.path}
            actionId={actionId}
            file={previewFile}
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
}
