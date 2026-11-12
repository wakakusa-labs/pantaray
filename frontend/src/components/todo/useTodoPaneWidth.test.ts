import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  boundaryRange,
  fixedWidthAt,
  keyedBoundary,
  readSavedWidth,
  saveWidth,
  shownFixedWidth,
} from './useTodoPaneWidth';

afterEach(() => {
  vi.restoreAllMocks();
  localStorage.clear();
});

describe('the rule between the TODO list and its pane', () => {
  it('beside a conversation sets the pane, keeping both sides their least', () => {
    // 1,201px: the list, the 1px rule and the pane.
    expect(fixedWidthAt('conversation', 1201, 700)).toBe(500);
    expect(fixedWidthAt('conversation', 1201, 100)).toBe(1201 - 1 - 320);
    expect(fixedWidthAt('conversation', 1201, 1150)).toBe(360);
  });

  it('beside a file sets the list, leaving the file and the conversation their room', () => {
    expect(fixedWidthAt('split', 1201, 500)).toBe(500);
    expect(fixedWidthAt('split', 1201, 100)).toBe(320);
    expect(fixedWidthAt('split', 1201, 1000)).toBe(1201 - 1 - 560);
  });

  it('keeps a remembered width inside a window narrowed since, and the list its least', () => {
    expect(shownFixedWidth('conversation', 1201, 600)).toBe(600);
    expect(shownFixedWidth('conversation', 900, 800)).toBe(900 - 1 - 320);
    // Too narrow for both: the list keeps its least.
    expect(shownFixedWidth('split', 700, 500)).toBe(320);
    expect(boundaryRange('split', 700)).toEqual({ min: 320, max: 320 });
  });

  it('steps with the arrow keys and goes to either end with Home and End', () => {
    expect(keyedBoundary('ArrowRight', 'conversation', 1201, 600)).toBe(624);
    expect(keyedBoundary('ArrowLeft', 'conversation', 1201, 600)).toBe(576);
    expect(keyedBoundary('ArrowLeft', 'conversation', 1201, 330)).toBe(320);
    expect(keyedBoundary('Home', 'conversation', 1201, 600)).toBe(320);
    expect(keyedBoundary('End', 'conversation', 1201, 600)).toBe(1201 - 1 - 360);
    expect(keyedBoundary('Enter', 'conversation', 1201, 600)).toBeNull();
  });
});

describe('the remembered width', () => {
  it('is kept per layout and goes back to the default when cleared', () => {
    saveWidth('conversation', 512.4);
    saveWidth('split', 480);
    expect(readSavedWidth('conversation')).toBe(512);
    expect(readSavedWidth('split')).toBe(480);
    saveWidth('conversation', null);
    expect(readSavedWidth('conversation')).toBeNull();
    expect(readSavedWidth('split')).toBe(480);
  });

  it('is the default when what is stored is not a width', () => {
    localStorage.setItem('pantaray.todo-pane-width:conversation', 'wide');
    expect(readSavedWidth('conversation')).toBeNull();
    localStorage.setItem('pantaray.todo-pane-width:conversation', '-40');
    expect(readSavedWidth('conversation')).toBeNull();
  });

  it('is the default, and moving the rule still works, when the storage refuses', () => {
    vi.spyOn(console, 'warn').mockImplementation(() => {});
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new DOMException('denied', 'SecurityError');
    });
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new DOMException('full', 'QuotaExceededError');
    });
    expect(readSavedWidth('conversation')).toBeNull();
    expect(() => saveWidth('conversation', 500)).not.toThrow();
  });
});
