import { useEffect, useId, useState } from 'react';
import { X } from 'lucide-react';

import { buildActionFileUrl } from '../../../electron/src/protocol/actionFileUrl';
import { MarkdownBlock } from '@/components/agent-overlay/MarkdownRenderer';
import { useI18n } from '@/context/useI18n';

import type { TaskFile } from './taskFiles';
import './taskFiles.css';

const COPY = {
  en: {
    openInApp: 'Open in default app',
    quickLook: 'Quick Look',
    close: 'Close preview',
    loading: 'Loading…',
    truncated: 'Showing the beginning only.',
    appOnly: 'Look at this file in Quick Look or open it in its app.',
    unavailable: "This file can't be shown here.",
    openFailed: "Couldn't open the file.",
  },
  ja: {
    openInApp: 'いつものアプリで開く',
    quickLook: 'Quick Look で見る',
    close: 'プレビューを閉じる',
    loading: '読み込んでいます…',
    truncated: '途中まで表示しています。',
    appOnly: 'このファイルは Quick Look かいつものアプリで見られます。',
    unavailable: 'このファイルはここでは表示できません。',
    openFailed: 'ファイルを開けませんでした。',
  },
} as const;

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

  const openInApp = async () => {
    setOpenFailed(false);
    try {
      const result = await window.electron?.actionFiles?.openInApp({ actionId, path: file.path });
      if (result?.kind !== 'opened') setOpenFailed(true);
    } catch (error) {
      console.error('Failed to open an Action file in its app', error);
      setOpenFailed(true);
    }
  };
  const quickLook = async () => {
    setOpenFailed(false);
    try {
      const result = await window.electron?.actionFiles?.quickLook({ actionId, path: file.path });
      if (result?.kind !== 'shown') setOpenFailed(true);
    } catch (error) {
      console.error('Failed to show an Action file in Quick Look', error);
      setOpenFailed(true);
    }
  };
  const openButtons = (
    <>
      <button type="button" className="task-file-preview__open" onClick={() => void quickLook()}>
        {copy.quickLook}
      </button>
      <button type="button" className="task-file-preview__open" onClick={() => void openInApp()}>
        {copy.openInApp}
      </button>
    </>
  );
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
        {openButtons}
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
          <div className="task-file-preview__notice">
            <p>{file.kind === 'app_only' ? copy.appOnly : copy.unavailable}</p>
            {openButtons}
          </div>
        ) : content.kind === 'page' ? (
          page(content.html)
        ) : content.kind === 'image' ? (
          <img className="task-file-preview__image" src={content.url} alt={file.name} />
        ) : (
          <>
            {content.truncated ? (
              <div className="task-file-preview__notice">
                <p>{copy.truncated}</p>
                {openButtons}
              </div>
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
