import type { TaskFile } from './taskFiles';
import './taskFiles.css';

type FileChipsProps = {
  files: readonly TaskFile[];
  /** The file the preview shows, if any. */
  selectedPath: string | null;
  /** Clicking the shown file's chip again closes its preview. */
  onToggle: (file: TaskFile) => void;
};

/** The documents an Action produced, under its answer; each opens beside the conversation. */
export function FileChips({ files, selectedPath, onToggle }: FileChipsProps) {
  if (files.length === 0) return null;
  return (
    <div className="task-file-chips">
      {files.map((file) => (
        <button
          key={file.path}
          type="button"
          className="task-file-chip"
          aria-pressed={file.path === selectedPath}
          title={file.path}
          onClick={() => onToggle(file)}
        >
          <span className="task-file-chip__label">{file.label}</span>
          <span className="task-file-chip__name">{file.name}</span>
        </button>
      ))}
    </div>
  );
}
