import { useEffect, useId, useRef, useState, type FormEvent } from 'react';

import { useI18n } from '@/context/useI18n';
import type { MessageKey } from '@/i18n/types';

import type { GoalView } from '../../../../electron/src/todos/todoContracts';
import type { GoalInput } from '../../../todos/useTodoProgressMutations';

import './progress.css';

/** `GOAL_TEXT_MAX_LENGTH`, `GOAL_UNIT_MAX_LENGTH` (`schema/todo_progress.py`), in UTF-16 units. */
const GOAL_TEXT_MAX_LENGTH = 200;
const GOAL_UNIT_MAX_LENGTH = 20;

type Field = 'text' | 'target' | 'unit' | 'deadline';
type Draft = Readonly<Record<Field, string>>;
type FieldErrors = Partial<Record<Field, MessageKey>>;

const FIELDS: readonly Field[] = ['text', 'target', 'unit', 'deadline'];

/** The goal the draft names, or what to fix in it. Whether the deadline is too early is the backend's. */
function readGoalDraft(
  draft: Draft
): { kind: 'ok'; goal: GoalInput } | { kind: 'invalid'; errors: FieldErrors } {
  const text = draft.text.trim();
  const unit = draft.unit.trim();
  const target = Number(draft.target);
  const errors: FieldErrors = {
    ...(text ? {} : { text: 'todoProgress.form.error.text' }),
    ...(draft.target.trim() && Number.isFinite(target) && target > 0
      ? {}
      : { target: 'todoProgress.form.error.target' }),
    ...(unit ? {} : { unit: 'todoProgress.form.error.unit' }),
    ...(draft.deadline ? {} : { deadline: 'todoProgress.form.error.deadline' }),
  };
  if (Object.keys(errors).length > 0) return { kind: 'invalid', errors };
  return { kind: 'ok', goal: { text, target, unit, deadline: draft.deadline } };
}

const draftOf = (goal: GoalView | null): Draft => ({
  text: goal?.text ?? '',
  target: goal ? String(goal.target) : '',
  unit: goal?.unit ?? '',
  deadline: goal?.deadline ?? '',
});

/**
 * Sets or edits a project's goal, or removes it. Mounted only while open; the caller moves
 * focus back once it is gone. A write the backend refused is told in `failure`.
 */
export function GoalForm({
  projectName,
  goal,
  failure,
  onSave,
  onClear,
  onClose,
}: {
  projectName: string;
  /** The goal being edited; null to set a new one. */
  goal: GoalView | null;
  failure: string | null;
  /** Resolves true once the goal was saved and read again. */
  onSave: (goal: GoalInput) => Promise<boolean>;
  onClear: () => Promise<boolean>;
  onClose: () => void;
}) {
  const { t } = useI18n();
  const dialogRef = useRef<HTMLDialogElement>(null);
  const fieldRefs = useRef<Partial<Record<Field, HTMLInputElement | null>>>({});
  const titleId = useId();
  const idPrefix = useId();
  const [draft, setDraft] = useState<Draft>(() => draftOf(goal));
  const [errors, setErrors] = useState<FieldErrors>({});
  const [busy, setBusy] = useState(false);
  // A form closed while its write was pending is gone: the write's answer must not close the
  // form opened after it, which the same `onClose` would.
  const openRef = useRef(false);
  useEffect(() => {
    openRef.current = true;
    const dialog = dialogRef.current;
    if (dialog && !dialog.open) dialog.showModal();
    return () => {
      openRef.current = false;
    };
  }, []);

  const run = async (write: () => Promise<boolean>) => {
    setBusy(true);
    const done = await write();
    if (!openRef.current) return;
    setBusy(false);
    if (done) onClose();
  };

  const submit = (event: FormEvent) => {
    event.preventDefault();
    const read = readGoalDraft(draft);
    if (read.kind === 'invalid') {
      setErrors(read.errors);
      const first = FIELDS.find((field) => read.errors[field]);
      if (first) fieldRefs.current[first]?.focus();
      return;
    }
    setErrors({});
    void run(() => onSave(read.goal));
  };

  const field = (
    name: Field,
    label: MessageKey,
    input: { type: 'text' | 'number' | 'date'; maxLength?: number; placeholder?: MessageKey }
  ) => {
    const id = `${idPrefix}-${name}`;
    const error = errors[name];
    return (
      <div className="todo-goal-form__field">
        <label htmlFor={id}>{t(label)}</label>
        <input
          ref={(element) => {
            fieldRefs.current[name] = element;
          }}
          id={id}
          type={input.type}
          className="history-filter-input"
          value={draft[name]}
          maxLength={input.maxLength}
          placeholder={input.placeholder ? t(input.placeholder) : undefined}
          {...(input.type === 'number' ? { min: 0, step: 'any', inputMode: 'decimal' } : {})}
          aria-invalid={error ? true : undefined}
          aria-describedby={error ? `${id}-error` : undefined}
          onChange={(event) => setDraft((current) => ({ ...current, [name]: event.target.value }))}
        />
        {error ? (
          <span id={`${id}-error`} className="todo-goal-form__error">
            {t(error)}
          </span>
        ) : null}
      </div>
    );
  };

  return (
    <dialog
      ref={dialogRef}
      className="todo-goal-form"
      aria-labelledby={titleId}
      onCancel={(event) => {
        event.preventDefault();
        onClose();
      }}
    >
      <form noValidate onSubmit={submit}>
        <h2 id={titleId}>
          {t(goal ? 'todoProgress.form.titleEdit' : 'todoProgress.form.titleSet', {
            project: projectName,
          })}
        </h2>
        {field('text', 'todoProgress.form.text', {
          type: 'text',
          maxLength: GOAL_TEXT_MAX_LENGTH,
          placeholder: 'todoProgress.form.textHint',
        })}
        <div className="todo-goal-form__pair">
          {field('target', 'todoProgress.form.target', { type: 'number' })}
          {field('unit', 'todoProgress.form.unit', {
            type: 'text',
            maxLength: GOAL_UNIT_MAX_LENGTH,
            placeholder: 'todoProgress.form.unitHint',
          })}
        </div>
        {field('deadline', 'todoProgress.form.deadline', { type: 'date' })}
        {failure ? (
          <p className="todo-goal-form__failure" role="alert">
            {failure}
          </p>
        ) : null}
        <div className="todo-goal-form__actions">
          {goal ? (
            <button
              type="button"
              className="history-filter-button todo-goal-form__clear"
              disabled={busy}
              onClick={() => void run(onClear)}
            >
              {t('todoProgress.form.clear')}
            </button>
          ) : null}
          <button type="button" className="history-filter-button" onClick={onClose}>
            {t('common.cancel')}
          </button>
          <button type="submit" className="history-filter-button" disabled={busy}>
            {t('common.save')}
          </button>
        </div>
      </form>
    </dialog>
  );
}
