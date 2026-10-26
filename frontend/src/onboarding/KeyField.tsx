import { useState } from 'react';

import type { Translate } from '../pages/settings/types';

/**
 * A key typed into its checklist row. `onSave` rejects when the key is not saved; the draft
 * stays so it can be corrected, and the row's problem note says why. A saved key shows only
 * that it is saved, never its value.
 */
export function KeyField({
  label,
  saved,
  placeholder,
  disabled = false,
  onSave,
  t,
}: {
  label: string;
  saved: boolean;
  placeholder: string;
  /** Another connection change is being applied. */
  disabled?: boolean;
  onSave: (value: string) => Promise<void>;
  t: Translate;
}) {
  const [draft, setDraft] = useState('');
  const [editing, setEditing] = useState(false);
  const [saving, setSaving] = useState(false);
  const replace = t('settings.aiConnection.key.replace');

  if (saved && !editing) {
    return (
      <>
        <span className="check-item-state">{t('onboarding.keySaved')}</span>
        <button
          type="button"
          className="settings-text-button"
          aria-label={`${label}: ${replace}`}
          disabled={disabled}
          onClick={() => setEditing(true)}
        >
          {replace}
        </button>
      </>
    );
  }

  const save = async () => {
    setSaving(true);
    try {
      await onSave(draft.trim());
      setDraft('');
      setEditing(false);
    } catch {
      // The row's problem note names the reason; keep the draft so it can be corrected.
    } finally {
      setSaving(false);
    }
  };

  return (
    <form
      className="onb-key-form"
      onSubmit={(event) => {
        event.preventDefault();
        void save();
      }}
    >
      <input
        className="history-filter-input onb-key-input"
        type="password"
        autoComplete="off"
        spellCheck={false}
        aria-label={label}
        placeholder={placeholder}
        value={draft}
        disabled={disabled || saving}
        // Pressing Change removed the button that had focus; the field takes it.
        autoFocus={editing}
        onChange={(event) => setDraft(event.target.value)}
      />
      <button
        type="submit"
        className="settings-action-button"
        disabled={disabled || saving || draft.trim().length === 0}
      >
        {t(saving ? 'settings.aiConnection.key.saving' : 'settings.aiConnection.key.save')}
      </button>
      {saved ? (
        <button
          type="button"
          className="settings-text-button"
          onClick={() => {
            setDraft('');
            setEditing(false);
          }}
        >
          {t('common.cancel')}
        </button>
      ) : null}
    </form>
  );
}
