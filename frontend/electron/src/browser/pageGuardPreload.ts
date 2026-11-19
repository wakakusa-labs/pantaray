/**
 * The frame preload of every browser session, and the one guard it puts in a page's world.
 * Blink has no switch for any of it.
 *
 * The guard removes the page APIs in `GUARDED_APIS`:
 * - connections Electron never passes through `webRequest`: WebRTC (over TCP under any IP
 *   handling policy) and WebTransport (Electron does not intercept it);
 * - OS UI a click's user activation opens from the hidden window: the File System Access
 *   pickers (a same-origin sandbox allows them), the screen color sampler (`OpenEyeDropper`),
 *   Web Bluetooth, whose chooser picks the first device when nothing listens for
 *   `select-bluetooth-device`, and `showPicker`. Picture-in-picture is refused by the documents'
 *   permissions policy instead, which the video controls' own button honours.
 *
 * It also keeps every native picker shut. On macOS a list box's list is an NSMenu
 * (`PopupMenuHelper`) and the color, date and time pickers are windows at the menu level
 * (`PopupWindowMac`), both on the user's display whatever window the page is in. Past
 * `showPicker`, Blink opens them only as a default action: of a trusted mouse press or key on a
 * list box, a trusted key on a picker field, or a click on a picker field or its label, a page
 * script's `click()` and `dispatchEvent` included (`EventDispatcher` runs no other default for an
 * untrusted event). Listeners on the frame's window, added before the page could add one, cancel
 * those defaults, and `click()` and `dispatchEvent` drop a click aimed at a picker field, so a
 * script cannot reach one in a closed shadow root either. A trusted input into a closed shadow
 * root is out of the listeners' sight; `interactions` checks its target through CDP before it
 * sends it. A datalist's suggestions need nothing: Electron shows none for a hidden window
 * (`AutofillPopupView::Show`).
 *
 * As a preload it runs in Electron's isolated world while the page's own world is being set up,
 * before any page script, in every committed document of every frame, out-of-process frames
 * included (`nodeIntegrationInSubFrames`). `contextBridge.executeInMainWorld` compiles and runs
 * the guard in the page's world at once. It exposes nothing to the page. Electron gives no
 * preload to an `about:blank` frame, so the session also runs `removeGuardedApis` there through
 * CDP. Main imports this module for the list and the function only.
 */

import { contextBridge } from 'electron';

export const GUARDED_APIS: readonly (readonly [owner: string, name: string])[] = [
  ['globalThis', 'RTCPeerConnection'],
  ['globalThis', 'webkitRTCPeerConnection'],
  ['globalThis', 'WebTransport'],
  ['globalThis', 'showOpenFilePicker'],
  ['globalThis', 'showSaveFilePicker'],
  ['globalThis', 'showDirectoryPicker'],
  ['globalThis', 'EyeDropper'],
  ['Navigator.prototype', 'bluetooth'],
  ['HTMLInputElement.prototype', 'showPicker'],
  ['HTMLSelectElement.prototype', 'showPicker'],
];

type PageFunction = (...args: unknown[]) => unknown;

/**
 * Runs in the page's world, serialized, so it refers to nothing outside itself. What the guard
 * calls later is taken from the world now, before page script can replace it, and it iterates
 * nothing the page could patch.
 */
export function removeGuardedApis(apis: readonly (readonly [string, string])[]): void {
  const world = globalThis as unknown as Record<string, unknown>;
  for (const [owner, name] of apis) {
    const target = owner
      .split('.')
      .reduce<
        Record<string, unknown>
      >((object, key) => object[key] as Record<string, unknown>, world);
    delete target[name];
  }

  const { apply } = Reflect;
  const PageTypeError = TypeError;
  const hasInstance = Function.prototype[Symbol.hasInstance];
  const prototypeOf = (name: string) =>
    (world[name] as { prototype: Record<string, PageFunction> }).prototype;
  const getter = (name: string, property: string) =>
    Object.getOwnPropertyDescriptor(prototypeOf(name), property)?.get as PageFunction;
  /** The getter's value, or undefined when `target` is not of the getter's interface. */
  const read = (get: PageFunction, target: unknown): unknown => {
    try {
      return apply(get, target, []);
    } catch (error) {
      if (apply(hasInstance, PageTypeError, [error]) === true) return undefined;
      throw error;
    }
  };
  const nodeType = getter('Node', 'nodeType');
  const parentNode = getter('Node', 'parentNode');
  const shadowHost = getter('ShadowRoot', 'host');
  const localName = getter('Element', 'localName');
  const namespaceURI = getter('Element', 'namespaceURI');
  const inputType = getter('HTMLInputElement', 'type');
  const labelControl = getter('HTMLLabelElement', 'control');
  const eventType = getter('Event', 'type');
  const keyOf = getter('KeyboardEvent', 'key');
  const composedPath = prototypeOf('Event').composedPath;
  const preventDefault = prototypeOf('Event').preventDefault;
  const addEventListener = prototypeOf('EventTarget').addEventListener;
  const PICKER_TYPES: Record<string, true> = Object.assign(Object.create(null), {
    color: true,
    date: true,
    'datetime-local': true,
    month: true,
    week: true,
    time: true,
  });

  const elementOf = (target: unknown): object | null => {
    const type = read(nodeType, target);
    const element = type === 3 ? apply(parentNode, target, []) : target;
    return (type === 1 || type === 3) && read(nodeType, element) === 1 ? (element as object) : null;
  };
  const isHtml = (element: object, name: string) =>
    apply(localName, element, []) === name &&
    apply(namespaceURI, element, []) === 'http://www.w3.org/1999/xhtml';
  const opensPicker = (element: object) =>
    isHtml(element, 'input') && PICKER_TYPES[apply(inputType, element, []) as string] === true;
  /** Whether the element or one of its ancestors, across shadow roots, passes `test`. */
  const within = (element: object, test: (candidate: object) => boolean): boolean => {
    for (let at: unknown = element; at !== null; ) {
      if (apply(nodeType, at, []) === 1 && test(at as object)) return true;
      at = apply(parentNode, at, []) ?? read(shadowHost, at) ?? null;
    }
    return false;
  };
  const inListBox = (element: object) => within(element, (at) => isHtml(at, 'select'));
  const clickOpensPicker = (element: object) =>
    opensPicker(element) ||
    within(element, (at) => {
      if (!isHtml(at, 'label')) return false;
      const control = apply(labelControl, at, []) as object | null;
      return control !== null && opensPicker(control);
    });
  const aimsAtPicker = (target: unknown) => {
    const element = elementOf(target);
    return element !== null && clickOpensPicker(element);
  };

  /** Cancels a default action that would open a native picker; Tab and Escape still move on. */
  const cancelPickerDefault = (event: unknown): void => {
    const type = apply(eventType, event, []);
    const element = elementOf((apply(composedPath, event, []) as unknown[])[0]);
    if (element === null) return;
    let opens: boolean;
    if (type === 'click') opens = clickOpensPicker(element);
    else if (type === 'mousedown') opens = inListBox(element);
    else {
      const key = read(keyOf, event);
      opens = (inListBox(element) || opensPicker(element)) && key !== 'Tab' && key !== 'Escape';
    }
    if (opens) apply(preventDefault, event, []);
  };
  for (const type of ['mousedown', 'keydown', 'keypress', 'click']) {
    apply(addEventListener, world, [type, cancelPickerDefault, true]);
  }

  const refuse = (owner: string, name: string, refused: unknown, drops: PageFunction) => {
    const original = prototypeOf(owner)[name];
    prototypeOf(owner)[name] = {
      [name](this: unknown, ...args: unknown[]) {
        return drops(this, args[0]) === true ? refused : apply(original, this, args);
      },
    }[name];
  };
  refuse('HTMLElement', 'click', undefined, aimsAtPicker);
  refuse(
    'EventTarget',
    'dispatchEvent',
    false,
    (target, event) => read(eventType, event) === 'click' && aimsAtPicker(target)
  );
}

if (process.type === 'renderer') {
  contextBridge.executeInMainWorld({ func: removeGuardedApis, args: [GUARDED_APIS] });
}
