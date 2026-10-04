import { describe, expect, it } from 'vitest';

import { shortcutAcceleratorFromKeyEvent, type ShortcutKeyEvent } from './shortcutAccelerator';

function keyEvent(
  key: string,
  modifiers: Partial<Omit<ShortcutKeyEvent, 'key'>> = {}
): ShortcutKeyEvent {
  return {
    altKey: false,
    code: '',
    ctrlKey: false,
    key,
    metaKey: false,
    shiftKey: false,
    ...modifiers,
  };
}

describe('shortcutAcceleratorFromKeyEvent', () => {
  it.each([
    [
      keyEvent('k', { altKey: true, metaKey: true, shiftKey: true }),
      true,
      'Command+Option+Shift+K',
    ],
    [keyEvent('ArrowUp', { altKey: true, ctrlKey: true }), false, 'Control+Alt+Up'],
    [keyEvent('!', { code: 'Digit1', ctrlKey: true, shiftKey: true }), false, 'Control+!'],
    [keyEvent('<', { code: 'Comma', metaKey: true, shiftKey: true }), true, 'Command+<'],
    [keyEvent(':', { code: 'Quote', metaKey: true }), true, 'Command+:'],
    [keyEvent("'", { code: 'Digit7', metaKey: true, shiftKey: true }), true, "Command+'"],
    [keyEvent('5', { code: 'Numpad5', ctrlKey: true }), false, 'Control+num5'],
    [keyEvent('+', { code: 'NumpadAdd', ctrlKey: true }), false, 'Control+numadd'],
    [keyEvent('Insert', { ctrlKey: true }), false, 'Control+Insert'],
    [keyEvent('a', { code: 'KeyQ', metaKey: true }), true, 'Command+A'],
    [keyEvent('k', { code: 'KeyK', metaKey: true }), false, 'Super+K'],
    [keyEvent('\u00a0', { code: 'Space', altKey: true }), true, 'Option+Space'],
    [keyEvent(' ', { code: 'Space', ctrlKey: true }), true, 'Control+Space'],
    [keyEvent('@', { code: 'KeyL', altKey: true }), true, 'Option+L'],
    [keyEvent('F1'), false, 'F1'],
    [keyEvent('F24', { shiftKey: true }), false, 'Shift+F24'],
  ])('converts a supported chord to an Electron accelerator', (event, isMac, expected) => {
    expect(shortcutAcceleratorFromKeyEvent(event, isMac)).toBe(expected);
  });

  it.each([
    keyEvent('k'),
    keyEvent('k', { shiftKey: true }),
    keyEvent('Alt', { altKey: true }),
    keyEvent('Escape'),
    keyEvent('F12'),
    keyEvent('F25'),
  ])('keeps recording when the key cannot form an accelerator', (event) => {
    expect(shortcutAcceleratorFromKeyEvent(event, true)).toBeNull();
  });
});
