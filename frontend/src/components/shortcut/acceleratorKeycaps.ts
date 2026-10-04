/** One rendered key: `symbol` is drawn, `label` is spoken by assistive technology. */
export type ShortcutKeycap = Readonly<{ symbol: string; label: string }>;

type ModifierDisplay = Readonly<{ order: number; mac: ShortcutKeycap; other: ShortcutKeycap }>;

const CONTROL: ModifierDisplay = {
  order: 0,
  mac: { symbol: '⌃', label: 'Control' },
  other: { symbol: 'Ctrl', label: 'Ctrl' },
};
const OPTION: ModifierDisplay = {
  order: 1,
  mac: { symbol: '⌥', label: 'Option' },
  other: { symbol: 'Alt', label: 'Alt' },
};
const SHIFT: ModifierDisplay = {
  order: 2,
  mac: { symbol: '⇧', label: 'Shift' },
  other: { symbol: 'Shift', label: 'Shift' },
};
const COMMAND: ModifierDisplay = {
  order: 3,
  mac: { symbol: '⌘', label: 'Command' },
  other: { symbol: 'Super', label: 'Super' },
};
const COMMAND_OR_CONTROL: ModifierDisplay = {
  order: 3,
  mac: { symbol: '⌘', label: 'Command' },
  other: { symbol: 'Ctrl', label: 'Ctrl' },
};

/** macOS draws modifiers as ⌃⌥⇧⌘ in that order; other platforms spell them out. */
const MODIFIERS: Readonly<Record<string, ModifierDisplay>> = {
  Control: CONTROL,
  Ctrl: CONTROL,
  Option: OPTION,
  Alt: OPTION,
  Shift: SHIFT,
  Command: COMMAND,
  Cmd: COMMAND,
  Super: COMMAND,
  CommandOrControl: COMMAND_OR_CONTROL,
  CmdOrCtrl: COMMAND_OR_CONTROL,
};

/** Tokens outside the accelerator vocabulary keep their input position, after known modifiers. */
const UNKNOWN_MODIFIER_ORDER = 4;

/**
 * Turns an Electron accelerator (`Option+Space`) into ordered keycaps for the current platform.
 * Unknown tokens are shown verbatim so nothing is silently dropped.
 */
export function acceleratorKeycaps(accelerator: string, isMac: boolean): ShortcutKeycap[] {
  const tokens = accelerator.split('+');
  const key = tokens.pop() ?? '';
  const modifiers = tokens.map((token, index) => {
    const modifier = MODIFIERS[token];
    return {
      order: modifier ? modifier.order : UNKNOWN_MODIFIER_ORDER,
      index,
      keycap: modifier ? (isMac ? modifier.mac : modifier.other) : { symbol: token, label: token },
    };
  });
  modifiers.sort((left, right) => left.order - right.order || left.index - right.index);
  return [...modifiers.map((modifier) => modifier.keycap), { symbol: key, label: key }];
}

/** Spoken form of a shortcut, e.g. `Option Space`. */
export function keycapsLabel(keycaps: readonly ShortcutKeycap[]): string {
  return keycaps.map((keycap) => keycap.label).join(' ');
}

/** ARIA names the modifiers by their KeyboardEvent key values. */
const ARIA_MODIFIER_NAMES: Readonly<Record<string, string>> = {
  Control: 'Control',
  Ctrl: 'Control',
  Option: 'Alt',
  Alt: 'Alt',
  Shift: 'Shift',
  Command: 'Meta',
  Super: 'Meta',
};

/** `aria-keyshortcuts` form of a shortcut, e.g. `Alt+Space`. */
export function ariaKeyShortcuts(keycaps: readonly ShortcutKeycap[]): string {
  return keycaps
    .map((keycap, index) =>
      index < keycaps.length - 1
        ? (ARIA_MODIFIER_NAMES[keycap.label] ?? keycap.label)
        : keycap.label
    )
    .join('+');
}
