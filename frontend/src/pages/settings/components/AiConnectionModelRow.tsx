import { useEffect, useRef, useState } from 'react';
import type { Translate } from '../types';
import { AiConnectionRow } from './AiConnectionRow';

/**
 * A model is a per-request setting, not an account boundary: changing it stops nothing that is
 * running, so a choice is saved as soon as it is made. A blank name is never saved, because
 * clearing the model would disconnect the route and stop running work.
 */
export function AiConnectionModelRow({
  model,
  candidates,
  selectionOnly = false,
  onSave,
  t,
}: {
  model: string;
  candidates: readonly string[];
  selectionOnly?: boolean;
  onSave: (model: string) => Promise<void>;
  t: Translate;
}) {
  const selectedModel = selectionOnly && !candidates.includes(model) ? '' : model;
  const [draft, setDraft] = useState(selectedModel);
  const [saving, setSaving] = useState(false);
  const [result, setResult] = useState<'saved' | 'saveFailed' | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const selectRef = useRef<HTMLSelectElement>(null);
  // Saving locks the control, which drops its focus. Return focus where the user left it.
  const focusAfter = useRef<Element | null>(null);
  useEffect(() => {
    setDraft(selectedModel);
  }, [selectedModel]);
  useEffect(() => {
    if (saving) return;
    const control = selectionOnly ? selectRef.current : inputRef.current;
    const target = result === 'saveFailed' ? control : focusAfter.current;
    focusAfter.current = null;
    if (result === 'saveFailed' || document.activeElement === document.body) {
      if (target instanceof HTMLElement) target.focus();
    }
  }, [result, saving, selectionOnly]);

  const save = async (value: string, returnFocusTo: Element | null) => {
    focusAfter.current = returnFocusTo;
    setSaving(true);
    setResult(null);
    try {
      await onSave(value);
      setResult('saved');
    } catch {
      // Main may persist the model before runtime apply fails; the same value still needs a retry.
      setResult('saveFailed');
    } finally {
      setSaving(false);
    }
  };

  const commit = (value: string, returnFocusTo: Element | null) => {
    if (saving) return;
    const next = value.trim();
    if (!next) {
      setDraft(selectedModel);
      return;
    }
    if (next !== model) void save(next, returnFocusTo);
  };

  return (
    <AiConnectionRow label={t('settings.aiConnection.modelLabel')} htmlFor="ai-model">
      <form
        className="ai-inline"
        onSubmit={(event) => {
          event.preventDefault();
          commit(draft, inputRef.current);
        }}
      >
        {selectionOnly ? (
          <select
            id="ai-model"
            ref={selectRef}
            className="history-filter-select"
            value={draft}
            disabled={saving}
            onChange={(event) => {
              setDraft(event.target.value);
              commit(event.target.value, event.target);
            }}
          >
            <option value="" disabled>
              {t('settings.aiConnection.modelSelectPlaceholder')}
            </option>
            {candidates.map((candidate) => (
              <option key={candidate} value={candidate}>
                {candidate}
              </option>
            ))}
          </select>
        ) : (
          <input
            id="ai-model"
            ref={inputRef}
            className="history-filter-input"
            type="text"
            spellCheck={false}
            list={candidates.length > 0 ? 'ai-model-candidates' : undefined}
            placeholder={t('settings.aiConnection.modelPlaceholder')}
            value={draft}
            disabled={saving}
            onChange={(event) => setDraft(event.target.value)}
            onBlur={(event) => commit(draft, event.relatedTarget)}
          />
        )}
        {result === 'saveFailed' ? (
          <button
            type="button"
            className="settings-text-button"
            disabled={saving}
            onClick={() =>
              void save(draft.trim(), selectionOnly ? selectRef.current : inputRef.current)
            }
          >
            {t('settings.aiConnection.model.retry')}
          </button>
        ) : null}
        <span
          className={result === 'saveFailed' ? 'ai-check ai-check--failed' : 'ai-note'}
          role="status"
        >
          {result ? t(`settings.aiConnection.model.${result}`) : ''}
        </span>
      </form>
      {!selectionOnly && candidates.length > 0 ? (
        <datalist id="ai-model-candidates">
          {candidates.map((candidate) => (
            <option key={candidate} value={candidate} />
          ))}
        </datalist>
      ) : null}
      <p className="ai-note ai-note--control">
        {t(
          selectionOnly
            ? 'settings.aiConnection.chatgptModelHint'
            : 'settings.aiConnection.modelHint'
        )}
      </p>
    </AiConnectionRow>
  );
}
