import { SquareArrowOutUpRight } from 'lucide-react';

import type { TaskFileTarget } from '../../../electron/src/actions/actionFileAccess';
import { useI18n } from '@/context/useI18n';

import type { TaskFile } from './taskFiles';
import './taskFiles.css';

const GROUP_LABEL = {
  action: { ja: 'この作業の成果物', en: "This task's files" },
  suggestion: { ja: 'この提案の成果物', en: "This suggestion's files" },
} as const;
const OPEN_IN_APP = { ja: '既定のアプリで開く', en: 'Open in default app' } as const;

type FileChipsProps = {
  owner: TaskFileTarget['kind'];
  files: readonly TaskFile[];
  /**
   * The file the preview beside the chips shows; clicking its chip again closes it. Absent where
   * the preview opens elsewhere, so the chips are plain buttons there.
   */
  selectedPath?: string | null;
  onOpen: (file: TaskFile) => void;
  /** Offered beside each chip where no preview with its Open menu is at hand. */
  onOpenInApp?: (file: TaskFile) => void;
};

/** The documents an Action or a Suggestion produced; each opens in the main window's preview. */
export function FileChips({ owner, files, selectedPath, onOpen, onOpenInApp }: FileChipsProps) {
  const { language } = useI18n();
  if (files.length === 0) return null;
  return (
    <div className="task-file-chips" role="group" aria-label={GROUP_LABEL[owner][language]}>
      {files.map((file) => {
        const chip = (
          <button
            key={file.path}
            type="button"
            className="task-file-chip"
            aria-pressed={selectedPath === undefined ? undefined : file.path === selectedPath}
            title={file.path}
            onClick={() => onOpen(file)}
          >
            <span className="task-file-chip__label">{file.label}</span>
            <span className="task-file-chip__name">{file.name}</span>
          </button>
        );
        if (!onOpenInApp) return chip;
        return (
          <span key={file.path} className="task-file-chip-item">
            {chip}
            <button
              type="button"
              className="task-file-chip__open"
              aria-label={`${OPEN_IN_APP[language]}: ${file.name}`}
              data-tooltip={OPEN_IN_APP[language]}
              onClick={() => onOpenInApp(file)}
            >
              <SquareArrowOutUpRight size={14} strokeWidth={1.8} aria-hidden />
            </button>
          </span>
        );
      })}
    </div>
  );
}
