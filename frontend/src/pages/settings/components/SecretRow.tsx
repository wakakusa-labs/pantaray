import { CircleAlert } from 'lucide-react';
import React, { useEffect, useRef, useState } from 'react';

import type { Translate } from '../types';
import { AiConnectionRow as Row } from './AiConnectionRow';
import './aiConnection.css';

/** A saved credential's row: the renderer only knows whether a key is saved, never its value. */
export function SecretRow({
  label,
  hasSavedKey,
  placeholder,
  inputId,
  disabled,
  onSave,
  onClear,
  problem,
  draftProblem,
  hint,
  t,
}: {
  label: string;
  hasSavedKey: boolean;
  placeholder: string;
  inputId: string;
  disabled: boolean;
  onSave: (value: string) => Promise<void> | void;
  onClear: () => Promise<void> | void;
  /** Why the saved key does not work; shown in place of "saved". */
  problem?: string;
  /** Why the typed key cannot be the right one; shown while typing and blocks saving. */
  draftProblem?: (draft: string) => string | undefined;
  hint?: React.ReactNode;
  t: Translate;
}): React.JSX.Element {
  const [draft, setDraft] = useState('');
  const [editing, setEditing] = useState(false);
  const [saving, setSaving] = useState(false);
  const [removing, setRemoving] = useState(false);
  const [result, setResult] = useState<
    'saveFailed' | 'removeFailed' | 'saveSuccess' | 'removed' | null
  >(null);
  const replaceRef = useRef<HTMLButtonElement>(null);
  const removeRef = useRef<HTMLButtonElement>(null);
  const focusAfter = useRef<'input' | 'replace' | 'remove' | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  // 「変更」で消えたボタンからフォーカスが落ちないよう、入力欄へ移す。
  useEffect(() => {
    if (editing) inputRef.current?.focus();
  }, [editing]);

  useEffect(() => {
    if (saving || removing) return;
    const target =
      focusAfter.current === 'replace'
        ? replaceRef.current
        : focusAfter.current === 'remove'
          ? hasSavedKey
            ? removeRef.current
            : inputRef.current
          : focusAfter.current === 'input'
            ? inputRef.current
            : null;
    if (target && !target.matches(':disabled')) {
      target.focus();
      focusAfter.current = null;
    }
  }, [editing, hasSavedKey, saving, removing, disabled]);

  const save = async (): Promise<void> => {
    const value = draft.trim();
    if (!value) return;
    setSaving(true);
    setEditing(true);
    setResult(null);
    try {
      await onSave(value);
    } catch {
      // 保存後の反映で失敗する場合もあるため、再試行できるよう入力を残す。
      setResult('saveFailed');
      focusAfter.current = 'input';
      return;
    } finally {
      setSaving(false);
    }
    setDraft('');
    setEditing(false);
    setResult('saveSuccess');
    focusAfter.current = 'replace';
  };

  const clear = async (): Promise<void> => {
    setRemoving(true);
    setResult(null);
    try {
      await onClear();
      setResult('removed');
      focusAfter.current = 'input';
    } catch {
      setResult('removeFailed');
      focusAfter.current = 'remove';
    } finally {
      setRemoving(false);
    }
  };

  const typedProblem = draftProblem?.(draft.trim());

  const feedback = (
    <span
      className={result?.endsWith('Failed') ? 'ai-check ai-check--failed' : 'ai-note'}
      role="status"
    >
      {saving
        ? t('settings.aiConnection.key.saving')
        : removing
          ? t('settings.aiConnection.key.removing')
          : result
            ? t(`settings.aiConnection.key.${result}`)
            : ''}
    </span>
  );

  if (hasSavedKey && !editing) {
    return (
      <Row label={label}>
        <div className="ai-inline">
          <span className="ai-secret-mask">••••••••••••</span>
          {problem ? (
            <ProblemNote text={problem} />
          ) : (
            <span className="ai-note">{t('settings.aiConnection.key.saved')}</span>
          )}
          <button
            type="button"
            className="settings-text-button"
            aria-label={`${label}: ${t('settings.aiConnection.key.replace')}`}
            ref={replaceRef}
            disabled={disabled || saving || removing}
            onClick={() => {
              setResult(null);
              setEditing(true);
            }}
          >
            {t('settings.aiConnection.key.replace')}
          </button>
          <button
            type="button"
            className="settings-text-button"
            aria-label={`${label}: ${t('settings.aiConnection.key.remove')}`}
            ref={removeRef}
            disabled={saving || removing}
            onClick={() => void clear()}
          >
            {t('settings.aiConnection.key.remove')}
          </button>
          {feedback}
        </div>
        {hint}
      </Row>
    );
  }

  return (
    <Row label={label} htmlFor={inputId}>
      <div className="ai-inline">
        <input
          id={inputId}
          ref={inputRef}
          className="history-filter-input"
          type="password"
          autoComplete="off"
          spellCheck={false}
          disabled={disabled || saving || removing}
          placeholder={placeholder}
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
        />
        <button
          type="button"
          className="settings-action-button"
          aria-label={`${label}: ${t('settings.aiConnection.key.save')}`}
          disabled={
            disabled || saving || removing || draft.trim().length === 0 || Boolean(typedProblem)
          }
          onClick={() => void save()}
        >
          {t('settings.aiConnection.key.save')}
        </button>
        {hasSavedKey ? (
          <button
            type="button"
            className="settings-text-button"
            aria-label={`${label}: ${t('settings.aiConnection.key.cancel')}`}
            disabled={saving}
            onClick={() => {
              setDraft('');
              setResult(null);
              setEditing(false);
              focusAfter.current = 'replace';
            }}
          >
            {t('settings.aiConnection.key.cancel')}
          </button>
        ) : null}
        {feedback}
      </div>
      {typedProblem ? <ProblemNote text={typedProblem} /> : null}
      {hint}
    </Row>
  );
}

function ProblemNote({ text }: { text: string }): React.JSX.Element {
  return (
    <span role="alert" className="ai-check ai-check--failed ai-secret-problem">
      <CircleAlert size={15} strokeWidth={1.8} aria-hidden />
      {text}
    </span>
  );
}
