import type { ActionConversationView } from '../../../electron/src/actions/actionConversationModel';
import { pantarayFilePaths } from '../../../electron/src/actions/pantarayFileLinks';

/**
 * How the preview shows a file: rendered from its text (Markdown, a web page, or plain text),
 * drawn as an image or by the PDF viewer, or only opened in its app.
 */
export type TaskFileKind = 'markdown' | 'html' | 'text' | 'image' | 'pdf' | 'app_only';

export type TaskFile = Readonly<{
  path: string;
  name: string;
  kind: TaskFileKind;
  /** The chip's short type label, such as `HTML`. */
  label: string;
}>;

const KIND_BY_EXTENSION: Readonly<Record<string, TaskFileKind>> = {
  md: 'markdown',
  markdown: 'markdown',
  html: 'html',
  htm: 'html',
  png: 'image',
  jpg: 'image',
  jpeg: 'image',
  gif: 'image',
  webp: 'image',
  pdf: 'pdf',
  docx: 'app_only',
  xlsx: 'app_only',
  pptx: 'app_only',
  doc: 'app_only',
  xls: 'app_only',
  ppt: 'app_only',
  key: 'app_only',
  pages: 'app_only',
  numbers: 'app_only',
  rtf: 'app_only',
  zip: 'app_only',
};

// Code an Action edits stays a diff in its steps, so it never becomes a chip.
const CODE_EXTENSIONS = new Set(
  (
    'py ts tsx js jsx mjs cjs go rs java kt swift c h cc cpp hpp cs rb php sh bash zsh ' +
    'css scss sass less vue svelte sql lua pl r scala dart m mm yml yaml toml ini lock'
  ).split(' ')
);

const LABEL_BY_EXTENSION: Readonly<Record<string, string>> = {
  markdown: 'MD',
  htm: 'HTML',
  jpeg: 'JPG',
};

/** A document chip for the path, or null for code and for a name without an extension. */
function taskFile(path: string): TaskFile | null {
  const name = path.split('/').pop() ?? '';
  const dot = name.lastIndexOf('.');
  if (dot <= 0) return null;
  const extension = name.slice(dot + 1).toLowerCase();
  if (CODE_EXTENSIONS.has(extension)) return null;
  return {
    path,
    name,
    // Any other file is tried as text: a log, JSON or CSV reads as it is.
    kind: KIND_BY_EXTENSION[extension] ?? 'text',
    label: LABEL_BY_EXTENSION[extension] ?? extension.toUpperCase(),
  };
}

/**
 * The documents an Action produced, in the order the conversation names them: files its final
 * answers link, and files its patches created or edited. A patch's subject is the path as the
 * call gave it, so only an absolute one that was not cut short names a file the app can open.
 */
export function deriveTaskFiles(view: ActionConversationView): TaskFile[] {
  const paths: string[] = [];
  for (const item of view.items) {
    if (item.kind !== 'run') continue;
    for (const line of item.lines) {
      if (line.kind === 'final_output') paths.push(...pantarayFilePaths(line.text));
      if (line.kind !== 'tool') continue;
      const { file_edit: fileEdit, subject } = line.entry;
      if (
        fileEdit !== null &&
        fileEdit.operation !== 'delete' &&
        subject !== null &&
        subject.startsWith('/') &&
        !subject.endsWith('…')
      ) {
        paths.push(subject);
      }
    }
  }
  const files = new Map<string, TaskFile>();
  for (const path of paths) {
    const file = files.has(path) ? null : taskFile(path);
    if (file) files.set(path, file);
  }
  return [...files.values()];
}
