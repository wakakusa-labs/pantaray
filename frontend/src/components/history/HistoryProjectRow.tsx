import { useRef, useState } from 'react';

import type { useI18n } from '@/context/useI18n';
import type {
  WorkspaceFolder,
  WorkspaceProject,
} from '@/pages/settings/components/workspaceSettingsModel';

import { HistoryProjectMenu, type HistoryProjectMenuItem } from './HistoryProjectMenu';

/** One project in the sidebar, with its 「…」 menu and, while renaming, its name field. */
export function HistoryProjectRow({
  project,
  folders,
  menuItems,
  menuButtonId,
  renaming,
  t,
  onRename,
  onRenameEnd,
}: {
  project: WorkspaceProject;
  folders: WorkspaceFolder[];
  menuItems: HistoryProjectMenuItem[];
  menuButtonId: string;
  renaming: boolean;
  t: ReturnType<typeof useI18n>['t'];
  onRename: (displayName: string) => Promise<boolean>;
  /** False when another row's rename has begun since, which keeps its field and its focus. */
  onRenameEnd: () => boolean;
}) {
  const endRename = () => {
    if (onRenameEnd()) document.getElementById(menuButtonId)?.focus();
  };

  return (
    <li className="history-item history-project">
      {renaming ? (
        <RenameField
          initialName={project.display_name}
          label={t('history.projects.nameLabel')}
          onSave={async (name) => {
            if (await onRename(name)) endRename();
          }}
          onCancel={endRename}
        />
      ) : (
        <span
          className="history-project__name"
          title={folders.map((folder) => folder.real_path).join('\n')}
        >
          {project.display_name}
        </span>
      )}
      <HistoryProjectMenu
        label={t('history.projects.menu', { name: project.display_name })}
        buttonId={menuButtonId}
        items={menuItems}
      />
    </li>
  );
}

/** The keyCode of a key the IME processed, sent for the Enter that ends a composition. */
const IME_PROCESS_KEY_CODE = 229;

/** Enter saves and Escape cancels; leaving the field saves, as a file name field does. */
function RenameField({
  initialName,
  label,
  onSave,
  onCancel,
}: {
  initialName: string;
  label: string;
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
      value={name}
      // Opened from the menu on purpose, so taking focus here is what the user asked for.
      autoFocus
      onFocus={(event) => event.currentTarget.select()}
      onChange={(event) => setName(event.target.value)}
      onBlur={() => void save()}
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
