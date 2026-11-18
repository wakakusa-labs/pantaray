import { useEffect, useId, useState } from 'react';

import { AGENTS_MD_MAX_BYTES, utf8ByteLength } from '../../../../electron/src/ipc/schemas/agentsMd';
import type { Translate } from '../types';

type SaveStatus = 'saving' | 'saved' | 'saveFailed' | null;

/** Edits `~/.pantaray/AGENTS.md` in place; the file is the only copy. */
export function AgentsMdSection({ t }: { t: Translate }) {
  const titleId = useId();
  const descriptionId = useId();
  const [content, setContent] = useState<string | null>(null);
  const [loadFailed, setLoadFailed] = useState(false);
  const [status, setStatus] = useState<SaveStatus>(null);

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const api = window.electron?.agentsMd;
        if (!api) throw new Error('AGENTS.md bridge is unavailable.');
        const text = await api.read();
        if (!cancelled) setContent(text);
      } catch {
        if (!cancelled) setLoadFailed(true);
      }
    };
    void load();
    return () => {
      cancelled = true;
    };
  }, []);

  // Main refuses the same limit; checking here says why before the user presses save.
  const tooLong = content !== null && utf8ByteLength(content) > AGENTS_MD_MAX_BYTES;

  const save = async () => {
    if (content === null || status === 'saving' || tooLong) return;
    setStatus('saving');
    try {
      const api = window.electron?.agentsMd;
      if (!api) throw new Error('AGENTS.md bridge is unavailable.');
      await api.write(content);
      setStatus('saved');
    } catch {
      setStatus('saveFailed');
    }
  };

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
      {loadFailed ? (
        <p className="settings-section-status settings-section-error" role="alert">
          {t('settings.agentsMd.loadFailed')}
        </p>
      ) : content === null ? (
        <p className="settings-section-status">{t('settings.agentsMd.loading')}</p>
      ) : (
        <>
          <textarea
            className="history-filter-input settings-agents-md-input"
            aria-labelledby={titleId}
            aria-describedby={descriptionId}
            aria-invalid={tooLong || undefined}
            placeholder={t('settings.agentsMd.placeholder')}
            spellCheck={false}
            // Held while saving, so "saved" always describes the text on screen.
            readOnly={status === 'saving'}
            value={content}
            onChange={(event) => {
              setContent(event.target.value);
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
                tooLong || status === 'saveFailed'
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
          </div>
        </>
      )}
    </div>
  );
}
