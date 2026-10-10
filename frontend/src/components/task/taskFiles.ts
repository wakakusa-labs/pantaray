import type { ActionConversationView } from '../../../electron/src/actions/actionConversationModel';
import { pantarayFilePaths } from '../../../electron/src/actions/pantarayFileLinks';

/**
 * How the preview shows a file: rendered from its text (Markdown, a web page, or plain text),
 * as a web page converted from a word-processor document, drawn as an image or by the PDF
 * viewer, or left to Quick Look and its app.
 */
export type TaskFileKind = 'markdown' | 'html' | 'text' | 'document' | 'image' | 'pdf' | 'app_only';

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
  docx: 'document',
  doc: 'document',
  rtf: 'document',
  odt: 'document',
  xlsx: 'app_only',
  xls: 'app_only',
  pptx: 'app_only',
  ppt: 'app_only',
  key: 'app_only',
  pages: 'app_only',
  numbers: 'app_only',
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

function extensionOf(name: string): string {
  const dot = name.lastIndexOf('.');
  return dot > 0 ? name.slice(dot + 1).toLowerCase() : '';
}

/** How the preview shows the file at a path an answer links. */
export function taskFileAt(path: string): TaskFile {
  const name = path.split('/').pop() ?? '';
  const extension = extensionOf(name);
  return {
    path,
    name,
    // Any other file is tried as text: a log, JSON or CSV reads as it is.
    kind: KIND_BY_EXTENSION[extension] ?? 'text',
    label: LABEL_BY_EXTENSION[extension] ?? extension.toUpperCase(),
  };
}

/** Whether the path gets a chip: not code, and not a name without an extension. */
function isDocumentPath(path: string): boolean {
  const extension = extensionOf(path.split('/').pop() ?? '');
  return extension !== '' && !CODE_EXTENSIONS.has(extension);
}

/**
 * The documents an Action produced, in the order its final answers link them. Only those links
 * name a file main will serve: a step's subject is display text, not a path.
 */
export function deriveTaskFiles(view: ActionConversationView): TaskFile[] {
  const files = new Map<string, TaskFile>();
  for (const item of view.items) {
    if (item.kind !== 'run') continue;
    for (const line of item.lines) {
      if (line.kind !== 'final_output') continue;
      for (const path of pantarayFilePaths(line.text)) {
        if (!files.has(path) && isDocumentPath(path)) files.set(path, taskFileAt(path));
      }
    }
  }
  return [...files.values()];
}

/**
 * Which finished run the conversation has reached. A later run may rewrite a file the preview
 * shows, so the preview reads it again whenever this changes.
 */
export function latestCompletion(view: ActionConversationView): string | null {
  for (let index = view.items.length - 1; index >= 0; index -= 1) {
    const item = view.items[index];
    if (item.kind === 'run' && item.completedAt !== null)
      return `${item.runId}@${item.completedAt}`;
  }
  return null;
}
