import type { RefObject } from 'react';

export function InlineTextForm(props: {
  buttonLabel: string;
  busy: boolean;
  disabled: boolean;
  onSubmit: () => void;
  placeholder: string;
  value: string;
  onChange: (value: string) => void;
  inputRef?: RefObject<HTMLInputElement>;
  inputId?: string;
}) {
  const { busy, buttonLabel, disabled, inputId, inputRef, onChange, onSubmit, placeholder, value } =
    props;
  return (
    <div className="workspace-inline-form">
      <input
        ref={inputRef}
        id={inputId}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        onKeyDown={(event) => {
          // The Enter that commits a kana-kanji conversion belongs to the IME.
          if (event.key === 'Enter' && !event.nativeEvent.isComposing) onSubmit();
        }}
        placeholder={placeholder}
        aria-label={placeholder}
        className="workspace-input"
      />
      <button
        type="button"
        onClick={onSubmit}
        aria-busy={busy}
        disabled={disabled}
        className="workspace-button"
      >
        {buttonLabel}
      </button>
    </div>
  );
}
