import { useEffect, useId, useRef, useState } from 'react';

import {
  AGENTS_MD_MAX_BYTES,
  utf8ByteLength,
  type AgentsMdRevision,
} from '../../../../electron/src/ipc/schemas/agentsMd';
import type { Translate } from '../types';

type View =
  | { kind: 'loading' }
  | { kind: 'load_failed' }
  | { kind: 'too_large' }
  | { kind: 'editing'; content: string; revision: AgentsMdRevision };
type SaveStatus = 'saving' | 'saved' | 'saveFailed' | 'conflict' | null;

async function readView(): Promise<View> {
  try {
    const api = window.electron?.agentsMd;
    if (!api) throw new Error('AGENTS.md bridge is unavailable.');
    const result = await api.read();
    return result.kind === 'too_large'
      ? result
      : { kind: 'editing', content: result.content, revision: result.revision };
  } catch {
    return { kind: 'load_failed' };
  }
}

/** Edits `~/.pantaray/AGENTS.md` in place; the file is the only copy. */
export function AgentsMdSection({ t }: { t: Translate }) {
  const titleId = useId();
  const descriptionId = useId();
  const editorRef = useRef<HTMLTextAreaElement>(null);
  const focusEditorAfterLoad = useRef(false);
  const [view, setView] = useState<View>({ kind: 'loading' });
  const [status, setStatus] = useState<SaveStatus>(null);

  useEffect(() => {
    let cancelled = false;
    void readView().then((next) => {
      if (!cancelled) setView(next);
    });
    return () => {
      cancelled = true;
    };
  }, []);

  // The reload button goes away with the conflict, so focus moves to the text it loaded.
  useEffect(() => {
    if (view.kind === 'editing' && focusEditorAfterLoad.current) {
      focusEditorAfterLoad.current = false;
      editorRef.current?.focus();
    }
  }, [view]);

  const content = view.kind === 'editing' ? view.content : null;
  // Main refuses the same limit; checking here says why before the user presses save.
  const tooLong = content !== null && utf8ByteLength(content) > AGENTS_MD_MAX_BYTES;

  const save = async () => {
    if (view.kind !== 'editing' || status === 'saving' || tooLong) return;
    setStatus('saving');
    try {
      const api = window.electron?.agentsMd;
      if (!api) throw new Error('AGENTS.md bridge is unavailable.');
      const result = await api.write({ content: view.content, expectedRevision: view.revision });
      if (result.kind === 'conflict') {
        // The edits stay on screen; only the user's reload replaces them.
        setStatus('conflict');
        return;
      }
      setView({ kind: 'editing', content: view.content, revision: result.revision });
      setStatus('saved');
    } catch {
      setStatus('saveFailed');
    }
  };

  const reload = async () => {
    setStatus(null);
    focusEditorAfterLoad.current = true;
    setView(await readView());
  };

  const failed = tooLong || status === 'saveFailed' || status === 'conflict';
  return (
    <div className="dashboard-section execution-settings-section">
      <div>
        <h3 id={titleId} className="dashboard-section-title">
          {t('settings.agentsMd.title')}
        </h3>
        <p id={descriptionId} className="dashboard-section-description settings-agents-md-hint">
          {t('settings.agentsMd.description')}
        </p>
      </div>
      {view.kind === 'load_failed' || view.kind === 'too_large' ? (
        <p className="settings-section-status settings-section-error" role="alert">
          {t(
            view.kind === 'too_large'
              ? 'settings.agentsMd.fileTooLarge'
              : 'settings.agentsMd.loadFailed'
          )}
        </p>
      ) : view.kind === 'loading' ? (
        <p className="settings-section-status">{t('settings.agentsMd.loading')}</p>
      ) : (
        <>
          <textarea
            ref={editorRef}
            className="history-filter-input settings-agents-md-input"
            aria-labelledby={titleId}
            aria-describedby={descriptionId}
            aria-invalid={tooLong || undefined}
            spellCheck={false}
            // Held while saving, so "saved" always describes the text on screen.
            readOnly={status === 'saving'}
            value={view.content}
            onChange={(event) => {
              setView({ ...view, content: event.target.value });
              setStatus(null);
            }}
          />
          <div className="settings-agents-md-actions">
            <button
              type="button"
              className="settings-action-button"
              disabled={tooLong}
              onClick={() => void save()}
            >
              {t('settings.agentsMd.save')}
            </button>
            <span
              className={
                failed
                  ? 'settings-section-status settings-agents-md-error'
                  : 'settings-section-status'
              }
              role="status"
            >
              {tooLong
                ? t('settings.agentsMd.tooLong')
                : status
                  ? t(`settings.agentsMd.${status}`)
                  : ''}
            </span>
            {status === 'conflict' ? (
              <button type="button" className="settings-text-button" onClick={() => void reload()}>
                {t('settings.agentsMd.reload')}
              </button>
            ) : null}
          </div>
        </>
      )}
    </div>
  );
}
