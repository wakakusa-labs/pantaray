import type { KeyboardEvent } from 'react';

/** The keyCode of a key the IME processed, sent for the Enter that ends a composition. */
const IME_PROCESS_KEY_CODE = 229;

/** A key the IME takes while composing, such as the Enter that ends a composition. */
export const isImeKey = (event: KeyboardEvent) =>
  event.nativeEvent.isComposing || event.keyCode === IME_PROCESS_KEY_CODE;
