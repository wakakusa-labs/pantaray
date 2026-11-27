import { useState, type ReactNode } from 'react';

import { OpenFileLinkContext } from '@/components/agent-overlay/openFileLinkContext';

import { ActionTaskPane } from './ActionTaskPane';
import { BrowserViewPane } from './BrowserViewPane';
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
  /** The page's controls for the pane (expand, shrink, close), last in its header. */
  pageControls?: ReactNode;
  /** What the conversation opens with, above its messages. */
  conversationStart?: ReactNode;
};

/**
 * What the left of the conversation shows: a file, or the app's page the Action works on. Each
 * request to view the page counts up, so it watches afresh, as after that page has closed.
 */
type WorkspacePreview =
  | Readonly<{ kind: 'file' } & TaskPreview>
  | Readonly<{ kind: 'browser'; request: number }>;

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
  pageControls,
  conversationStart,
}: TaskWorkspaceProps) {
  const [preview, setPreview] = useState<WorkspacePreview | null>(
    initialPreview && { kind: 'file', ...initialPreview }
  );
  const open = (file: TaskFile) =>
    setPreview({ kind: 'file', target: { kind: 'action', id: actionId }, file });
  const watchBrowser = () =>
    setPreview((current) => ({
      kind: 'browser',
      request: current?.kind === 'browser' ? current.request + 1 : 0,
    }));
  const filePath = preview?.kind === 'file' ? preview.file.path : null;
  const pane = (
    <ActionTaskPane
      actionId={actionId}
      title={title}
      onAddProject={onAddProject}
      pageControls={pageControls}
      conversationStart={conversationStart}
      onWatchBrowser={watchBrowser}
      renderPreview={(view) =>
        preview?.kind === 'browser' ? (
          <BrowserViewPane
            key={`browser:${preview.request}`}
            actionId={actionId}
            onClose={() => setPreview(null)}
          />
        ) : preview ? (
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
          selectedPath={filePath}
          onOpen={(file) => (file.path === filePath ? setPreview(null) : open(file))}
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
