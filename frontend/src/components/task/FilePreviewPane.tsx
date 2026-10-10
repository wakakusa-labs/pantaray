import { useEffect, useId, useState } from 'react';
import { X } from 'lucide-react';

import { buildActionFileUrl } from '../../../electron/src/protocol/actionFileUrl';
import { MarkdownBlock } from '@/components/agent-overlay/MarkdownRenderer';
import { useClipboardCopy } from '@/components/agent-overlay/useClipboardCopy';
import { useI18n } from '@/context/useI18n';

import { FileOpenMenu, type FileOpenMenuItem } from './FileOpenMenu';
import type { TaskFile } from './taskFiles';
import './taskFiles.css';

const COPY = {
  en: {
    open: 'Open',
    reveal: 'Show in Finder',
    quickLook: 'Quick Look',
    openInApp: 'Open in default app',
    openWithApp: 'Open with…',
    copyPath: 'Copy path',
    pathCopied: 'Copied',
    copyFailed: "Couldn't copy",
    close: 'Close preview',
    loading: 'Loading…',
    truncated: 'Showing the beginning only.',
    appOnly: 'Open this file from the Open menu.',
    unavailable: "This file can't be shown here.",
    openFailed: "Couldn't open the file.",
  },
  ja: {
    open: '開く',
    reveal: 'Finder で表示',
    quickLook: 'Quick Look で見る',
    openInApp: '既定のアプリで開く',
    openWithApp: 'アプリを選んで開く…',
    copyPath: 'パスをコピー',
    pathCopied: 'コピーしました',
    copyFailed: 'コピーできませんでした',
    close: 'プレビューを閉じる',
    loading: '読み込んでいます…',
    truncated: '途中まで表示しています。',
    appOnly: 'このファイルは「開く」から見られます。',
    unavailable: 'このファイルはここでは表示できません。',
    openFailed: 'ファイルを開けませんでした。',
  },
} as const;

/** Quick Look draws these; a text file already reads in the preview itself. */
const QUICK_LOOK_KINDS = new Set<TaskFile['kind']>(['document', 'image', 'pdf', 'app_only']);

type OpenWay = 'reveal' | 'quickLook' | 'openInApp' | 'openWithApp';

/*
 * The document is untrusted content. The empty sandbox runs no script and gives the frame an
 * opaque origin, and this policy stops it loading anything over the network: only inline
 * styles and data: images render. A policy can only be tightened by a later one, so the
 * document's own <meta> tags cannot loosen it.
 */
const HTML_PREVIEW_POLICY =
  "default-src 'none'; style-src 'unsafe-inline'; img-src data:; font-src data:";
const POLICY_META = `<meta http-equiv="Content-Security-Policy" content="${HTML_PREVIEW_POLICY}">`;
const LEADING_DOCTYPE = /^\s*<!doctype[^>]*>/i;

/** The policy goes first so it applies before any element; after a doctype, to keep standards mode. */
function htmlPreviewDocument(html: string): string {
  const doctype = LEADING_DOCTYPE.exec(html)?.[0] ?? '';
  return `${doctype}${POLICY_META}${html.slice(doctype.length)}`;
}

/** A new revision changes the URL, so the frame loads the PDF again instead of keeping it. */
function pdfUrl(actionId: string, filePath: string, revision: string | null): string {
  const url = new URL(buildActionFileUrl(actionId, filePath));
  if (revision !== null) url.searchParams.set('revision', revision);
  return url.toString();
}

type Content =
  | Readonly<{ kind: 'loading' }>
  | Readonly<{ kind: 'text'; text: string; truncated: boolean }>
  | Readonly<{ kind: 'page'; html: string }>
  | Readonly<{ kind: 'image'; url: string }>
  | Readonly<{ kind: 'unavailable' }>;

const READ_KINDS = new Set<TaskFile['kind']>(['markdown', 'html', 'text', 'document', 'image']);

/**
 * Reads the file, and again for each new revision; the old content stays until the new one
 * arrives. The workspace keys this pane by path, so another file remounts it.
 */
function useFileContent(actionId: string, file: TaskFile, revision: string | null): Content {
  const read = READ_KINDS.has(file.kind) ? window.electron?.actionFiles?.read : undefined;
  const [content, setContent] = useState<Content>(() =>
    read ? { kind: 'loading' } : { kind: 'unavailable' }
  );
  useEffect(() => {
    if (!read) return;
    let active = true;
    let imageUrl: string | null = null;
    read({ actionId, path: file.path }).then(
      (result) => {
        if (!active) return;
        if (result.kind === 'text') {
          setContent({ kind: 'text', text: result.text, truncated: result.truncated });
        } else if (result.kind === 'html') {
          setContent({ kind: 'page', html: result.html });
        } else if (result.kind === 'image') {
          imageUrl = URL.createObjectURL(new Blob([result.bytes], { type: result.mime }));
          setContent({ kind: 'image', url: imageUrl });
        } else {
          setContent({ kind: 'unavailable' });
        }
      },
      (error: unknown) => {
        console.error('Failed to read an Action file for preview', error);
        if (active) setContent({ kind: 'unavailable' });
      }
    );
    return () => {
      active = false;
      if (imageUrl) URL.revokeObjectURL(imageUrl);
    };
    // revision is not read inside: it only says the file may have changed since.
  }, [actionId, file.path, read, revision]);
  return content;
}

type FilePreviewPaneProps = {
  actionId: string;
  file: TaskFile;
  /** The conversation's latest finished run; a new one may have rewritten the file. */
  revision: string | null;
  onClose: () => void;
};

/** One file an Action produced, shown read-only beside its conversation. */
export function FilePreviewPane({ actionId, file, revision, onClose }: FilePreviewPaneProps) {
  const { language } = useI18n();
  const copy = COPY[language];
  const nameId = useId();
  const content = useFileContent(actionId, file, revision);
  const [openFailed, setOpenFailed] = useState(false);
  const { status: copyStatus, copy: copyToClipboard } = useClipboardCopy();

  const run = async (way: OpenWay) => {
    setOpenFailed(false);
    try {
      const result = await window.electron?.actionFiles?.[way]({ actionId, path: file.path });
      // A cancelled app picker is no failure.
      if (result === undefined || result.kind === 'unavailable') setOpenFailed(true);
    } catch (error) {
      console.error('Failed to open an Action file', error);
      setOpenFailed(true);
    }
  };
  const item = (label: string, way: OpenWay): FileOpenMenuItem => ({
    label,
    onSelect: () => void run(way),
  });
  const openItems = [
    item(copy.reveal, 'reveal'),
    ...(QUICK_LOOK_KINDS.has(file.kind) ? [item(copy.quickLook, 'quickLook')] : []),
    item(copy.openInApp, 'openInApp'),
    item(copy.openWithApp, 'openWithApp'),
  ];
  const page = (html: string) => (
    <iframe
      className="task-file-preview__frame"
      title={file.name}
      sandbox=""
      srcDoc={htmlPreviewDocument(html)}
    />
  );

  return (
    <section className="task-file-preview" aria-labelledby={nameId}>
      <header className="task-file-preview__header">
        <h2 id={nameId} title={file.path}>
          {file.name}
        </h2>
        <button
          type="button"
          className="task-file-preview__open"
          title={file.path}
          onClick={() => void copyToClipboard(() => file.path)}
        >
          <span aria-live="polite">
            {copyStatus === 'copied'
              ? copy.pathCopied
              : copyStatus === 'failed'
                ? copy.copyFailed
                : copy.copyPath}
          </span>
        </button>
        <FileOpenMenu label={copy.open} items={openItems} />
        <button
          type="button"
          className="task-file-preview__close"
          aria-label={copy.close}
          title={copy.close}
          onClick={onClose}
        >
          <X size={16} strokeWidth={1.8} aria-hidden />
        </button>
      </header>
      {openFailed ? (
        <p className="task-file-preview__alert" role="alert">
          {copy.openFailed}
        </p>
      ) : null}
      <div className="task-file-preview__body">
        {file.kind === 'pdf' ? (
          // Not sandboxed: a sandboxed frame turns plugins off, and the PDF viewer is one. The
          // scheme serves only a PDF the Action names, from an origin of its own.
          <iframe
            className="task-file-preview__frame"
            title={file.name}
            src={pdfUrl(actionId, file.path, revision)}
          />
        ) : content.kind === 'loading' ? (
          <p className="task-file-preview__status" role="status">
            {copy.loading}
          </p>
        ) : content.kind === 'unavailable' ? (
          <p className="task-file-preview__status">
            {file.kind === 'app_only' ? copy.appOnly : copy.unavailable}
          </p>
        ) : content.kind === 'page' ? (
          page(content.html)
        ) : content.kind === 'image' ? (
          <img className="task-file-preview__image" src={content.url} alt={file.name} />
        ) : (
          <>
            {content.truncated ? (
              <p className="task-file-preview__status">{copy.truncated}</p>
            ) : null}
            {file.kind === 'html' ? (
              page(content.text)
            ) : file.kind === 'markdown' ? (
              <div className="task-file-preview__document">
                <MarkdownBlock text={content.text} isStreamFinished />
              </div>
            ) : (
              <pre className="task-file-preview__text">{content.text}</pre>
            )}
          </>
        )}
      </div>
    </section>
  );
}
