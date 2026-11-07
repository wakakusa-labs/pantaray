import { useRef, useState } from 'react';

/** The keyCode of a key the IME processed, sent for the Enter that ends a composition. */
const IME_PROCESS_KEY_CODE = 229;

/**
 * A one-line text field. Enter submits the text and Escape cancels when the field can be canceled;
 * Enter and Escape while an IME is composing belong to the IME. One submit runs at a time, and a
 * submit that took the text empties the field for the next one unless the text changed meanwhile.
 */
export function LineTextField({
  label,
  className,
  initialText = '',
  autoFocus = false,
  submitOnBlur = false,
  onSubmit,
  onCancel,
}: {
  /** The field's accessible name, also shown as its placeholder. */
  label: string;
  className?: string;
  initialText?: string;
  /** Opened on purpose to edit: take focus, and select the text on focus so typing replaces it. */
  autoFocus?: boolean;
  /** Leaving the field submits, as a file name field does; otherwise leaving it empty cancels. */
  submitOnBlur?: boolean;
  /** Resolves true when the text was taken, which empties the field. */
  onSubmit: (text: string) => Promise<boolean>;
  onCancel?: () => void;
}) {
  const [text, setText] = useState(initialText);
  // Set while a submit is deciding and once Escape has, so the blur that follows does not act again.
  const settledRef = useRef(false);
  const submit = async () => {
    if (settledRef.current) return;
    settledRef.current = true;
    const sent = text;
    // Text typed while the submit was deciding is the next line, so only the sent text is cleared.
    if (await onSubmit(sent)) setText((current) => (current === sent ? '' : current));
    settledRef.current = false;
  };
  return (
    <input
      type="text"
      className={className ? `history-filter-input ${className}` : 'history-filter-input'}
      aria-label={label}
      placeholder={label}
      value={text}
      autoFocus={autoFocus}
      onFocus={autoFocus ? (event) => event.currentTarget.select() : undefined}
      onChange={(event) => setText(event.target.value)}
      // Selecting text with the pointer must not pick up a draggable row around the field.
      onPointerDown={(event) => event.stopPropagation()}
      onBlur={() => {
        if (submitOnBlur) void submit();
        else if (!text.trim() && !settledRef.current) onCancel?.();
      }}
      onKeyDown={(event) => {
        if (event.nativeEvent.isComposing || event.keyCode === IME_PROCESS_KEY_CODE) return;
        if (event.key === 'Enter') {
          event.preventDefault();
          void submit();
        } else if (event.key === 'Escape' && onCancel) {
          event.preventDefault();
          settledRef.current = true;
          onCancel();
        }
      }}
    />
  );
}
