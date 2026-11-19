/**
 * The operations that act on a session's page by ref, as CDP input at the element's box or one
 * of the fixed page functions below, never page or model text. A click that would open a list or
 * picker the OS draws is refused; `select` sets the options without opening the list.
 */

import type { Debugger } from 'electron';

import { BROWSER_MAX_WAIT_SECONDS, type BrowserOperation } from './browserContract';
import { BrowserOperationError, type BrowserPage } from './browserSession';
import { decodePng, MAX_FULL_PAGE_HEIGHT_PX, type CapturedArtifact } from './capture';
import { callOn, resolveRef } from './snapshot';

const WAIT_POLL_MS = 100;
const SCROLL_DIRECTION = { up: [0, -1], down: [0, 1], left: [-1, 0], right: [1, 0] } as const;

/** True when a click on this node would open a list or picker the OS draws. */
const OPENS_NATIVE_UI = `function () {
  const element = this.nodeType === 1 ? this : this.parentElement;
  if (element === null) return false;
  const label = element.closest('label');
  const control = label !== null && label.control !== null ? label.control : element;
  if (control.closest('select, datalist') !== null) return true;
  return control.localName === 'input'
    && ['color', 'date', 'datetime-local', 'month', 'week', 'time'].includes(control.type);
}`;
/** Selects the focused field's text, so typing replaces it; false when it takes no text. */
const SELECT_FIELD_TEXT = `function () {
  const active = document.activeElement;
  if (active === null || !(this === active || this.contains(active))) return false;
  if (active.isContentEditable) {
    const range = document.createRange();
    range.selectNodeContents(active);
    getSelection().removeAllRanges();
    getSelection().addRange(range);
    return true;
  }
  const takesText = active.localName === 'textarea' || (active.localName === 'input' && ![
    'button', 'checkbox', 'color', 'file', 'hidden', 'image', 'radio', 'range', 'reset', 'submit',
  ].includes(active.type));
  if (!takesText || active.readOnly || active.disabled) return false;
  active.select();
  return true;
}`;
/** Chooses options by value, else by label; null, or what kept it from doing so. */
const SELECT_OPTIONS = `function (values) {
  if (this.localName !== 'select') return 'This element is not a list box; click it instead.';
  if (this.disabled) return 'This list box is disabled.';
  const options = Array.from(this.options);
  const chosen = values.map((value) =>
    options.find((option) => option.value === value)
    || options.find((option) => option.label === value || option.text.trim() === value));
  if (chosen.some((option) => option === undefined || option.disabled)) {
    return 'A value is not an option of this list box; take a snapshot to see its options.';
  }
  if (!this.multiple && chosen.length > 1) return 'This list box takes one value.';
  for (const option of options) option.selected = chosen.includes(option);
  this.dispatchEvent(new Event('input', { bubbles: true }));
  this.dispatchEvent(new Event('change', { bubbles: true }));
  return null;
}`;
/** Scrolls the element, or with no element the page, by 80% of what it shows; true if it moved. */
const SCROLL_BY = `function (dx, dy) {
  const box = this instanceof Element ? this : document.scrollingElement;
  const [left, top] = [box.scrollLeft, box.scrollTop];
  box.scrollBy({ left: dx * box.clientWidth * 0.8, top: dy * box.clientHeight * 0.8, behavior: 'instant' });
  return box.scrollLeft !== left || box.scrollTop !== top;
}`;
const PAGE_HAS_TEXT = `function (text) {
  return document.body !== null && document.body.innerText.includes(text);
}`;

const failed = (message: string) => new BrowserOperationError('BROWSER_ACTION_FAILED', message);
const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

/** Evaluates an expression built only from this module's fixed functions and JSON values. */
async function evaluate(cdp: Debugger, expression: string): Promise<unknown> {
  const { result } = (await cdp.sendCommand('Runtime.evaluate', {
    expression,
    returnByValue: true,
  })) as { result: { value?: unknown } };
  return result.value;
}

/** Scrolls the node into view and returns its box in viewport CSS pixels. */
async function boxOf(
  cdp: Debugger,
  backendNodeId: number
): Promise<{ x: number; y: number; width: number; height: number }> {
  await cdp.sendCommand('DOM.scrollIntoViewIfNeeded', { backendNodeId });
  const { quads } = (await cdp.sendCommand('DOM.getContentQuads', { backendNodeId })) as {
    quads: number[][];
  };
  for (const quad of quads) {
    const xs = [quad[0], quad[2], quad[4], quad[6]];
    const ys = [quad[1], quad[3], quad[5], quad[7]];
    const box = { x: Math.min(...xs), y: Math.min(...ys) };
    const width = Math.max(...xs) - box.x;
    const height = Math.max(...ys) - box.y;
    if (width >= 1 && height >= 1) return { ...box, width, height };
  }
  throw failed('The element has no visible box.');
}

/** The hidden window is never focused; this lets the page behave as if it were. */
async function emulateFocus(cdp: Debugger): Promise<void> {
  await cdp.sendCommand('Emulation.setFocusEmulationEnabled', { enabled: true });
}

async function click(page: BrowserPage, ref: string, hoverOnly: boolean): Promise<void> {
  const { cdp } = page;
  const box = await boxOf(cdp, await resolveRef(page, ref));
  const point = { x: box.x + box.width / 2, y: box.y + box.height / 2 };
  const move = { type: 'mouseMoved', ...point };
  if (hoverOnly) {
    await cdp.sendCommand('Input.dispatchMouseEvent', move);
    return;
  }
  const { backendNodeId: hit } = (await cdp.sendCommand('DOM.getNodeForLocation', {
    x: Math.round(point.x),
    y: Math.round(point.y),
  })) as { backendNodeId: number };
  if ((await callOn(cdp, hit, OPENS_NATIVE_UI)) === true) {
    throw failed(
      'Clicking here would open a list or picker outside the page; use select for a list box.'
    );
  }
  await emulateFocus(cdp);
  await cdp.sendCommand('Input.dispatchMouseEvent', move);
  const button = { ...point, button: 'left', clickCount: 1 };
  await cdp.sendCommand('Input.dispatchMouseEvent', { ...button, type: 'mousePressed' });
  await cdp.sendCommand('Input.dispatchMouseEvent', { ...button, type: 'mouseReleased' });
}

type KeyDefinition = { key: string; code: string; keyCode: number; text?: string };

const MODIFIERS: Record<string, KeyDefinition & { bit: number }> = {
  Alt: { key: 'Alt', code: 'AltLeft', keyCode: 18, bit: 1 },
  Control: { key: 'Control', code: 'ControlLeft', keyCode: 17, bit: 2 },
  Meta: { key: 'Meta', code: 'MetaLeft', keyCode: 91, bit: 4 },
  Shift: { key: 'Shift', code: 'ShiftLeft', keyCode: 16, bit: 8 },
};
/** Each named key's Windows key code, which Chromium acts on; its DOM `code` is its name. */
const NAMED_KEYS: Record<string, number> = {
  Enter: 13,
  Tab: 9,
  Escape: 27,
  Backspace: 8,
  Delete: 46,
  ArrowUp: 38,
  ArrowDown: 40,
  ArrowLeft: 37,
  ArrowRight: 39,
  Home: 36,
  End: 35,
  PageUp: 33,
  PageDown: 34,
};

function keyDefinition(name: string): KeyDefinition {
  if (name === 'Enter') return { key: name, code: name, keyCode: 13, text: '\r' };
  if (name === 'Space' || name === ' ') return { key: ' ', code: 'Space', keyCode: 32, text: ' ' };
  if (NAMED_KEYS[name] !== undefined) return { key: name, code: name, keyCode: NAMED_KEYS[name] };
  if ([...name].length !== 1) throw failed(`${name} is not a key this browser can press.`);
  const upper = name.toUpperCase();
  const code = /^[A-Z]$/.test(upper) ? `Key${upper}` : /^[0-9]$/.test(name) ? `Digit${name}` : '';
  return { key: name, code, keyCode: code === '' ? 0 : upper.charCodeAt(0), text: name };
}

/** Presses one key or a chord such as `Shift+Tab`: modifiers, then the key, which is last. */
async function press(cdp: Debugger, chord: string): Promise<void> {
  const parts = chord.length === 1 ? [chord] : chord.split('+');
  const key = keyDefinition(parts.pop() as string);
  const held = parts.map((part) => {
    if (MODIFIERS[part] === undefined) throw failed(`${part} is not a modifier key.`);
    return MODIFIERS[part];
  });
  let modifiers = 0;
  const send = (type: string, definition: KeyDefinition, text?: string) =>
    cdp.sendCommand('Input.dispatchKeyEvent', {
      type,
      modifiers,
      key: definition.key,
      code: definition.code,
      windowsVirtualKeyCode: definition.keyCode,
      ...(text === undefined ? {} : { text, unmodifiedText: text }),
    });
  await emulateFocus(cdp);
  for (const modifier of held) {
    modifiers |= modifier.bit;
    await send('rawKeyDown', modifier);
  }
  // A chord with Control, Alt or Meta is a shortcut, not text.
  const text = (modifiers & ~MODIFIERS.Shift.bit) === 0 ? key.text : undefined;
  await send(text === undefined ? 'rawKeyDown' : 'keyDown', key, text);
  await send('keyUp', key);
  for (const modifier of held.reverse()) {
    modifiers &= ~modifier.bit;
    await send('keyUp', modifier);
  }
}

/** Replaces the field's text, as a person selecting it and typing would. */
async function typeText(
  page: BrowserPage,
  operation: Extract<BrowserOperation, { action: 'type' }>
): Promise<void> {
  const { cdp } = page;
  const node = await resolveRef(page, operation.ref);
  await emulateFocus(cdp);
  await cdp.sendCommand('DOM.focus', { backendNodeId: node });
  if ((await callOn(cdp, node, SELECT_FIELD_TEXT)) !== true) {
    throw failed('This element does not take text.');
  }
  if (operation.text === '') await press(cdp, 'Backspace');
  else await cdp.sendCommand('Input.insertText', { text: operation.text });
  if (operation.submit === true) await press(cdp, 'Enter');
}

async function selectOptions(
  page: BrowserPage,
  operation: Extract<BrowserOperation, { action: 'select' }>
): Promise<void> {
  const node = await resolveRef(page, operation.ref);
  const refusal = await callOn(page.cdp, node, SELECT_OPTIONS, [operation.values]);
  if (refusal !== null) throw failed(String(refusal));
}

/** Scrolls the page, or the element, by most of its visible size; refused when nothing moved. */
async function scroll(
  page: BrowserPage,
  operation: Extract<BrowserOperation, { action: 'scroll' }>
): Promise<void> {
  const [dx, dy] = SCROLL_DIRECTION[operation.direction];
  const moved =
    operation.ref == null
      ? await evaluate(page.cdp, `(${SCROLL_BY}).call(null, ${dx}, ${dy})`)
      : await callOn(page.cdp, await resolveRef(page, operation.ref), SCROLL_BY, [dx, dy]);
  if (moved !== true) throw failed('Nothing scrolled: it is at its end, or it does not scroll.');
}

/** Waits the seconds, or until the page shows the text, for at most `BROWSER_MAX_WAIT_SECONDS`. */
async function wait(
  cdp: Debugger,
  operation: Extract<BrowserOperation, { action: 'wait' }>
): Promise<void> {
  if (operation.seconds != null) {
    await sleep(operation.seconds * 1000);
    return;
  }
  const expression = `(${PAGE_HAS_TEXT})(${JSON.stringify(operation.text)})`;
  const deadline = Date.now() + BROWSER_MAX_WAIT_SECONDS * 1000;
  for (;;) {
    // Fails while the document is being replaced; the next one is checked on the next round.
    if ((await evaluate(cdp, expression).catch(() => false)) === true) return;
    if (Date.now() >= deadline) {
      throw failed(`The text did not appear within ${BROWSER_MAX_WAIT_SECONDS} s.`);
    }
    await sleep(WAIT_POLL_MS);
  }
}

/** The element's box, cut at the full page's height cap. */
export async function captureElement(
  page: BrowserPage,
  ref: string
): Promise<{ artifact: CapturedArtifact; cut: boolean }> {
  const box = await boxOf(page.cdp, await resolveRef(page, ref));
  // The box is in viewport pixels; the clip is in page pixels.
  const { cssLayoutViewport: viewport } = (await page.cdp.sendCommand('Page.getLayoutMetrics')) as {
    cssLayoutViewport: { pageX: number; pageY: number };
  };
  const { data } = (await page.cdp.sendCommand('Page.captureScreenshot', {
    format: 'png',
    captureBeyondViewport: true,
    clip: {
      x: box.x + viewport.pageX,
      y: box.y + viewport.pageY,
      width: box.width,
      height: Math.min(box.height, MAX_FULL_PAGE_HEIGHT_PX),
      scale: 1,
    },
  })) as { data: string };
  return { artifact: decodePng(data), cut: box.height > MAX_FULL_PAGE_HEIGHT_PX };
}

type Interaction = Extract<
  BrowserOperation,
  { action: 'click' | 'hover' | 'type' | 'press' | 'select' | 'scroll' | 'wait' }
>;

/** Runs an operation that acts on the page and answers with nothing of its own. */
export async function interact(page: BrowserPage, operation: Interaction): Promise<void> {
  switch (operation.action) {
    case 'click':
    case 'hover':
      return click(page, operation.ref, operation.action === 'hover');
    case 'type':
      return typeText(page, operation);
    case 'press':
      return press(page.cdp, operation.key);
    case 'select':
      return selectOptions(page, operation);
    case 'scroll':
      return scroll(page, operation);
    case 'wait':
      return wait(page.cdp, operation);
  }
}
