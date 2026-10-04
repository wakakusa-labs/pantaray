export type ShortcutKeyEvent = Pick<
  KeyboardEvent,
  'altKey' | 'code' | 'ctrlKey' | 'key' | 'metaKey' | 'shiftKey'
>;

const FUNCTION_KEY_PATTERN = /^F(?:[1-9]|1\d|2[0-4])$/;
const RELEASE_DEVTOOLS_KEY = 'F12';
const PRINTABLE_PUNCTUATION_KEYS = new Set(')!@#$%^&*(:;+=<,_->.?/~`{][|\\}"\'');

const CODE_KEYS: Readonly<Record<string, string>> = {
  Backquote: '`',
  Backslash: '\\',
  BracketLeft: '[',
  BracketRight: ']',
  Comma: ',',
  Equal: '=',
  Minus: '-',
  NumpadAdd: 'numadd',
  NumpadDecimal: 'numdec',
  NumpadDivide: 'numdiv',
  NumpadMultiply: 'nummult',
  NumpadSubtract: 'numsub',
  Period: '.',
  Quote: "'",
  Semicolon: ';',
  Slash: '/',
  // Option+Space reports a no-break space as its key on macOS.
  Space: 'Space',
};

const NAMED_KEYS: Readonly<Record<string, string>> = {
  ' ': 'Space',
  ArrowDown: 'Down',
  ArrowLeft: 'Left',
  ArrowRight: 'Right',
  ArrowUp: 'Up',
  Backspace: 'Backspace',
  Delete: 'Delete',
  End: 'End',
  Enter: 'Enter',
  Escape: 'Esc',
  Home: 'Home',
  Insert: 'Insert',
  PageDown: 'PageDown',
  PageUp: 'PageUp',
  Spacebar: 'Space',
  Tab: 'Tab',
};

function isAvailableFunctionKey(key: string): boolean {
  return key !== RELEASE_DEVTOOLS_KEY && FUNCTION_KEY_PATTERN.test(key);
}

function physicalKeyToken(code: string): string | null {
  if (/^Key[A-Z]$/.test(code)) return code.slice(3);
  if (/^Digit[0-9]$/.test(code)) return code.slice(5);
  return CODE_KEYS[code] ?? null;
}

function keyToken(event: ShortcutKeyEvent): string | null {
  const { key } = event;
  if (/^Numpad[0-9]$/.test(event.code)) return `num${event.code.slice(6)}`;
  if (event.code.startsWith('Numpad') && CODE_KEYS[event.code]) return CODE_KEYS[event.code];
  // Option turns a key into another character (Option+L is "@" on some layouts),
  // so the key that was pressed is its code.
  if (event.altKey) {
    const physical = physicalKeyToken(event.code);
    if (physical) return physical;
  }
  if (/^[a-z]$/i.test(key)) return key.toUpperCase();
  if (/^[0-9]$/.test(key)) return key;
  if (PRINTABLE_PUNCTUATION_KEYS.has(key)) return key === '+' ? 'Plus' : key;

  const physical = physicalKeyToken(event.code);
  if (physical) return physical;

  if (isAvailableFunctionKey(key)) return key;
  return NAMED_KEYS[key] ?? null;
}

export function shortcutAcceleratorFromKeyEvent(
  event: ShortcutKeyEvent,
  isMac: boolean
): string | null {
  const key = keyToken(event);
  if (!key) return null;

  const hasPrimaryModifier = event.metaKey || event.ctrlKey || event.altKey;
  if (!hasPrimaryModifier && !isAvailableFunctionKey(key)) return null;

  const modifiers = [
    event.metaKey ? (isMac ? 'Command' : 'Super') : null,
    event.ctrlKey ? 'Control' : null,
    event.altKey ? (isMac ? 'Option' : 'Alt') : null,
    event.shiftKey && !PRINTABLE_PUNCTUATION_KEYS.has(event.key) ? 'Shift' : null,
  ].filter((modifier): modifier is string => modifier !== null);

  return [...modifiers, key].join('+');
}
