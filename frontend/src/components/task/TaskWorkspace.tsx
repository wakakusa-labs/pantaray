import { useState } from 'react';

import { OpenFileLinkContext } from '@/components/agent-overlay/openFileLinkContext';

import { ActionTaskPane } from './ActionTaskPane';
import { FileChips } from './FileChips';
import { FilePreviewPane } from './FilePreviewPane';
import {
  deriveTaskFiles,
  latestCompletion,
  taskFileAt,
  type TaskFile,
  type TaskPreview,
} from './taskFiles';

type TaskWorkspaceProps = {
  /** The workspace holds one Action's preview, so the page keys it by this id. */
  actionId: string;
  title: string;
  /** The file the suggestion this Action started from had open, which stays open. */
  initialPreview?: TaskPreview | null;
  onAddProject: () => void;
};

/**
 * One Action with the documents it produced: the conversation alone, or a document's preview
 * on the left with the conversation narrowed to a column on the right. A file link in an answer
 * opens its preview, as its chip does.
 */
export function TaskWorkspace({
  actionId,
  title,
  initialPreview = null,
  onAddProject,
}: TaskWorkspaceProps) {
  const [preview, setPreview] = useState<TaskPreview | null>(initialPreview);
  const open = (file: TaskFile) => setPreview({ target: { kind: 'action', id: actionId }, file });
  const pane = (
    <ActionTaskPane
      actionId={actionId}
      title={title}
      onAddProject={onAddProject}
      renderPreview={(view) =>
        preview ? (
          <FilePreviewPane
            key={preview.file.path}
            target={preview.target}
            file={preview.file}
            revision={view ? latestCompletion(view) : null}
            onClose={() => setPreview(null)}
          />
        ) : null
      }
      renderFileChips={(view) => (
        <FileChips
          owner="action"
          files={deriveTaskFiles(view)}
          selectedPath={preview?.file.path ?? null}
          onOpen={(file) => (file.path === preview?.file.path ? setPreview(null) : open(file))}
        />
      )}
    />
  );
  return (
    <OpenFileLinkContext.Provider value={(path) => open(taskFileAt(path))}>
      {pane}
    </OpenFileLinkContext.Provider>
  );
}
