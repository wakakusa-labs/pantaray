import { useState } from 'react';

import type { OverlayOpenTaskRequest } from '../../../electron/src/ipc/schemas/overlay';
import { FileChips } from '@/components/task/FileChips';
import { taskFileAt, type TaskFile } from '@/components/task/taskFiles';
import { useI18n } from '@/context/useI18n';

const OPEN_FAILED = { ja: 'ファイルを開けませんでした。', en: "Couldn't open the file." } as const;

type SuggestionArtifactChipsProps = {
  suggestionId: string;
  artifacts: readonly string[];
  onOpenInMainWindow: (work: OverlayOpenTaskRequest) => void;
};

/**
 * The files a suggestion made, under its notice in a panel. A chip opens the file's preview in
 * the main window, which takes the suggestion over; the button beside it opens the file in its
 * app and leaves the panel as it is.
 */
export function SuggestionArtifactChips({
  suggestionId,
  artifacts,
  onOpenInMainWindow,
}: SuggestionArtifactChipsProps) {
  const { language } = useI18n();
  const [openFailed, setOpenFailed] = useState(false);
  const openInApp = async (file: TaskFile) => {
    setOpenFailed(false);
    try {
      const result = await window.electron?.actionFiles?.openInApp({
        target: { kind: 'suggestion', id: suggestionId },
        path: file.path,
      });
      if (result === undefined || result.kind === 'unavailable') setOpenFailed(true);
    } catch (error) {
      console.error('Failed to open a suggestion file', error);
      setOpenFailed(true);
    }
  };
  return (
    <>
      <FileChips
        owner="suggestion"
        files={artifacts.map(taskFileAt)}
        onOpen={(file) =>
          onOpenInMainWindow({ kind: 'suggestion', id: suggestionId, file: file.path })
        }
        onOpenInApp={(file) => void openInApp(file)}
      />
      {openFailed ? <p role="alert">{OPEN_FAILED[language]}</p> : null}
    </>
  );
}
