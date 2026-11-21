/**
 * The operations that act on a session's page by ref, as CDP input at the element's box or one
 * of the fixed page functions below, never page or model text. The functions run in the engine's
 * own world (`callOn`), so page script cannot change what they find.
 *
 * Input that would open a list or picker the OS draws is refused: a click or typing on one, a key
 * other than Tab or Escape while a list box has focus, or a key that opens a picker field's picker
 * while one has focus (`keyReaches`), wherever it sits (in a shadow root, closed ones included, or
 * a frame of the page's process). `pageGuardPreload` cancels what opens them in every frame as
 * well; this check also sees into closed shadow roots and names the refusal. `select` sets the
 * options without opening the list. A click or hover is refused, too, when another element covers
 * the ref's element at the point the input would reach.
 */

import { BROWSER_MAX_WAIT_SECONDS, type BrowserOperation } from './browserContract';
import { BrowserOperationError } from './browserSession';
import type { CdpPage, CdpSession } from './cdpSession';
import { decodePng, MAX_FULL_PAGE_HEIGHT_PX, type CapturedArtifact } from './capture';
import { callOn, engineWorld, resolveRef } from './snapshot';

const WAIT_POLL_MS = 100;
const SCROLL_DIRECTION = { up: [0, -1], down: [0, 1], left: [-1, 0], right: [1, 0] } as const;

/** A field whose list or picker the OS draws. */
type NativeUiField = 'list box' | 'picker';
/**
 * The kind of field whose list or picker input to this node would open: the node is, or is inside,
 * a list box or a picker field, or the label of one; null for any other node. A datalist's
 * suggestions are not among them: Electron shows none for a hidden window
 * (`AutofillPopupView::Show`).
 */
const NATIVE_UI_FIELD = `function () {
  const kind = (element) => element.localName === 'select' ? 'list box'
    : element.localName === 'input'
      && ['color', 'date', 'datetime-local', 'month', 'week', 'time'].includes(element.type)
      ? 'picker' : null;
  for (let at = this; at !== null; at = at.parentNode ?? (at instanceof ShadowRoot ? at.host : null)) {
    if (at.nodeType !== Node.ELEMENT_NODE) continue;
    const field = kind(at) ?? (at.localName === 'label' && at.control !== null ? kind(at.control) : null);
    if (field !== null) return field;
  }
  return null;
}`;
/** True when `node` is this element or inside it, across shadow roots. */
const HOLDS = `function (node) {
  for (let at = node; at !== null; at = at.parentNode ?? (at instanceof ShadowRoot ? at.host : null)) {
    if (at === this) return true;
  }
  return false;
}`;
const ACTIVE_ELEMENT = 'function () { return this.activeElement; }';
/** The objects a focus lookup resolves; it releases them as it ends. */
const FOCUS_GROUP = 'pantaray-browser-focus';
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

export const failed = (message: string) =>
  new BrowserOperationError('BROWSER_ACTION_FAILED', message);
const opensNativeUi = (where: string) =>
  failed(`${where} would open a list or picker outside the page; use select for a list box.`);
const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

/** Evaluates an expression built only from this module's fixed functions and JSON values. */
async function evaluate(cdp: CdpSession, expression: string): Promise<unknown> {
  const { result } = (await cdp.sendCommand('Runtime.evaluate', {
    expression,
    returnByValue: true,
  })) as { result: { value?: unknown } };
  return result.value;
}

type Box = { x: number; y: number; width: number; height: number };

/** Scrolls the node into view and returns its box in viewport CSS pixels. */
export async function boxOf(cdp: CdpSession, backendNodeId: number): Promise<Box> {
  await cdp.sendCommand('DOM.scrollIntoViewIfNeeded', { backendNodeId });
  return currentBox(cdp, backendNodeId);
}

/** The node's box in viewport CSS pixels where it is now, scrolled into view or not. */
export async function currentBox(cdp: CdpSession, backendNodeId: number): Promise<Box> {
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
export async function emulateFocus(cdp: CdpSession): Promise<void> {
  await cdp.sendCommand('Emulation.setFocusEmulationEnabled', { enabled: true });
}

export type Point = { x: number; y: number };

export const centerOf = (box: Box): Point => ({
  x: box.x + box.width / 2,
  y: box.y + box.height / 2,
});

/** The node that holds a frame's document in the main frame, for each frame the page's process draws. */
async function inMainFrame(cdp: CdpSession, node: number, frameId: string): Promise<number> {
  type FrameTree = { frame: { id: string }; childFrames?: FrameTree[] };
  const { frameTree } = (await cdp.sendCommand('Page.getFrameTree')) as { frameTree: FrameTree };
  const parents = new Map<string, string>();
  const visit = (tree: FrameTree) =>
    tree.childFrames?.forEach((child) => {
      parents.set(child.frame.id, tree.frame.id);
      visit(child);
    });
  visit(frameTree);
  let [holder, frame] = [node, frameId];
  while (frame !== frameTree.frame.id) {
    ({ backendNodeId: holder } = (await cdp.sendCommand('DOM.getFrameOwner', {
      frameId: frame,
    })) as { backendNodeId: number });
    frame = parents.get(frame) as string;
  }
  return holder;
}

/**
 * Refuses unless input at the point reaches one of the nodes or something inside it, and with
 * `refuseNativeUi`, also when it would open a list or picker there.
 */
export async function checkPoint(
  cdp: CdpSession,
  nodes: readonly number[],
  point: Point,
  refuseNativeUi: boolean
): Promise<void> {
  // A field's own parts, such as a date field's picker button, are in its user-agent shadow root.
  const hit = (await cdp.sendCommand('DOM.getNodeForLocation', {
    x: Math.round(point.x),
    y: Math.round(point.y),
    includeUserAgentShadowDOM: true,
  })) as { backendNodeId: number; frameId: string };
  const holder = await inMainFrame(cdp, hit.backendNodeId, hit.frameId);
  let reached = false;
  for (const node of nodes) {
    reached ||= (await callOn(cdp, node, HOLDS, [{ node: holder }])) === true;
  }
  if (!reached) {
    throw failed(
      'Another element covers this one where the input would land; scroll, close what covers it, or take a snapshot.'
    );
  }
  if (
    refuseNativeUi &&
    (await callOn(cdp, hit.backendNodeId, NATIVE_UI_FIELD, [], hit.frameId)) !== null
  ) {
    throw opensNativeUi('Clicking here');
  }
}

async function click(page: CdpPage, ref: string, hoverOnly: boolean): Promise<void> {
  const { cdp } = page;
  const node = await resolveRef(page, ref);
  const point = centerOf(await boxOf(cdp, node));
  await checkPoint(cdp, [node], point, !hoverOnly);
  const move = { type: 'mouseMoved', ...point };
  if (hoverOnly) {
    await cdp.sendCommand('Input.dispatchMouseEvent', move);
    return;
  }
  await emulateFocus(cdp);
  await cdp.sendCommand('Input.dispatchMouseEvent', move);
  // The hover may have shown something over the element.
  await checkPoint(cdp, [node], point, true);
  const button = { ...point, button: 'left', clickCount: 1 };
  await cdp.sendCommand('Input.dispatchMouseEvent', { ...button, type: 'mousePressed' });
  await cdp.sendCommand('Input.dispatchMouseEvent', { ...button, type: 'mouseReleased' });
}

/** The focused element of a document or shadow root, or of the frame's document when null. */
async function activeElementOf(
  cdp: CdpSession,
  frameId: string | undefined,
  container: number | null
): Promise<number | null> {
  const executionContextId = await engineWorld(cdp, frameId);
  let result: { objectId?: string };
  if (container === null) {
    ({ result } = (await cdp.sendCommand('Runtime.evaluate', {
      expression: 'document.activeElement',
      contextId: executionContextId,
      objectGroup: FOCUS_GROUP,
    })) as { result: { objectId?: string } });
  } else {
    const { object } = (await cdp.sendCommand('DOM.resolveNode', {
      backendNodeId: container,
      executionContextId,
      objectGroup: FOCUS_GROUP,
    })) as { object: { objectId: string } };
    ({ result } = (await cdp.sendCommand('Runtime.callFunctionOn', {
      objectId: object.objectId,
      functionDeclaration: ACTIVE_ELEMENT,
      objectGroup: FOCUS_GROUP,
    })) as { result: { objectId?: string } });
  }
  // A null element comes back as a value, without an object.
  if (result.objectId === undefined) return null;
  const { node } = (await cdp.sendCommand('DOM.describeNode', { objectId: result.objectId })) as {
    node: { backendNodeId: number };
  };
  return node.backendNodeId;
}

type DescribedNode = {
  backendNodeId: number;
  frameId?: string;
  shadowRoots?: { backendNodeId: number; shadowRootType: string }[];
  contentDocument?: { backendNodeId: number };
};

/**
 * The element that has focus, through shadow roots (CDP shows closed ones too) and the frames
 * the page's process draws; in an out-of-process frame, that frame's element in the page. Null
 * when the document has no element at all.
 */
async function focusedElement(cdp: CdpSession): Promise<{ node: number; frameId?: string } | null> {
  let frameId: string | undefined;
  try {
    let focused = await activeElementOf(cdp, frameId, null);
    while (focused !== null) {
      const { node } = (await cdp.sendCommand('DOM.describeNode', {
        backendNodeId: focused,
        depth: 1,
        pierce: true,
      })) as { node: DescribedNode };
      const root = node.shadowRoots?.find((shadow) => shadow.shadowRootType !== 'user-agent');
      let inner: number | null = null;
      if (root !== undefined) {
        inner = await activeElementOf(cdp, frameId, root.backendNodeId);
      } else if (node.contentDocument !== undefined) {
        frameId = node.frameId;
        inner = await activeElementOf(cdp, frameId, node.contentDocument.backendNodeId);
      }
      if (inner === null) return { node: focused, frameId };
      focused = inner;
    }
    return null;
  } finally {
    await cdp.sendCommand('Runtime.releaseObjectGroup', { objectGroup: FOCUS_GROUP });
  }
}

/** The kind of list or picker field that has focus, or null when the focus is on none. */
async function focusedNativeUiField(cdp: CdpSession): Promise<NativeUiField | null> {
  const focused = await focusedElement(cdp);
  if (focused === null) return null;
  return (await callOn(
    cdp,
    focused.node,
    NATIVE_UI_FIELD,
    [],
    focused.frameId
  )) as NativeUiField | null;
}

/**
 * Whether a key may reach a focused field of this kind. A list box takes only Tab and Escape: on
 * macOS its arrows, Space and Enter open the list. A picker field takes all but the keys that
 * open its picker (`MultipleFieldsTemporalInputTypeView::HandleKeydownEvent`: Space, F4,
 * Alt+ArrowDown; a color field's Space and Enter click it), so digits and arrows edit it.
 */
function keyReaches(field: NativeUiField, chord: string): boolean {
  const { modifiers, key } = chordParts(chord);
  if (field === 'list box') return key === 'Tab' || key === 'Escape';
  const arrow = key === 'ArrowDown' || key === 'ArrowUp';
  return !(['Space', ' ', 'Enter', 'F4'].includes(key) || (arrow && modifiers.includes('Alt')));
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

/** A chord such as `Shift+Tab`: its modifiers, then its key, which is last. */
function chordParts(chord: string): { modifiers: string[]; key: string } {
  const parts = chord.length === 1 ? [chord] : chord.split('+');
  return { key: parts.pop() as string, modifiers: parts };
}

/** Presses one key or a chord: modifiers, then the key. */
async function press(cdp: CdpSession, chord: string): Promise<void> {
  const parts = chordParts(chord);
  const key = keyDefinition(parts.key);
  const held = parts.modifiers.map((part) => {
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
  page: CdpPage,
  operation: Extract<BrowserOperation, { action: 'type' }>
): Promise<void> {
  const { cdp } = page;
  const node = await resolveRef(page, operation.ref);
  await emulateFocus(cdp);
  await cdp.sendCommand('DOM.focus', { backendNodeId: node });
  if ((await focusedNativeUiField(cdp)) !== null) throw opensNativeUi('Typing in this field');
  if ((await callOn(cdp, node, SELECT_FIELD_TEXT)) !== true) {
    throw failed('This element does not take text.');
  }
  if (operation.text === '') await press(cdp, 'Backspace');
  else await cdp.sendCommand('Input.insertText', { text: operation.text });
  if (operation.submit === true) await press(cdp, 'Enter');
}

async function selectOptions(
  page: CdpPage,
  operation: Extract<BrowserOperation, { action: 'select' }>
): Promise<void> {
  const node = await resolveRef(page, operation.ref);
  const refusal = await callOn(page.cdp, node, SELECT_OPTIONS, [{ value: operation.values }]);
  if (refusal !== null) throw failed(String(refusal));
}

/** Scrolls the page, or the element, by most of its visible size; refused when nothing moved. */
async function scroll(
  page: CdpPage,
  operation: Extract<BrowserOperation, { action: 'scroll' }>
): Promise<void> {
  const [dx, dy] = SCROLL_DIRECTION[operation.direction];
  const moved =
    operation.ref == null
      ? await evaluate(page.cdp, `(${SCROLL_BY}).call(null, ${dx}, ${dy})`)
      : await callOn(page.cdp, await resolveRef(page, operation.ref), SCROLL_BY, [
          { value: dx },
          { value: dy },
        ]);
  if (moved !== true) throw failed('Nothing scrolled: it is at its end, or it does not scroll.');
}

/** Waits the seconds, or until the page shows the text, for at most `BROWSER_MAX_WAIT_SECONDS`. */
async function wait(
  cdp: CdpSession,
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
  page: CdpPage,
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
export async function interact(page: CdpPage, operation: Interaction): Promise<void> {
  switch (operation.action) {
    case 'click':
    case 'hover':
      return click(page, operation.ref, operation.action === 'hover');
    case 'type':
      return typeText(page, operation);
    case 'press': {
      const field = await focusedNativeUiField(page.cdp);
      if (field !== null && !keyReaches(field, operation.key)) {
        throw opensNativeUi('This key in the focused field');
      }
      return press(page.cdp, operation.key);
    }
    case 'select':
      return selectOptions(page, operation);
    case 'scroll':
      return scroll(page, operation);
    case 'wait':
      return wait(page.cdp, operation);
  }
}
