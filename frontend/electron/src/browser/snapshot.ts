/**
 * The page's accessibility snapshot and the refs it names.
 *
 * A snapshot is the main frame's accessibility tree as indented text, one node per line:
 * `- role "name" [property] [ref=eN]: value`. Each element line carries a ref, which names that
 * DOM node (`backendDOMNodeId`) for the operations that take one. Each snapshot, of the page or
 * of the subtree a `ref` names, replaces the session's refs with the ones it shows; a node keeps
 * its ref across the snapshots of one document, a ref never names another node within a
 * session, and every ref is dropped once the main frame holds a new document.
 */

import type { Debugger, WebContents } from 'electron';

import { BrowserOperationError, type BrowserPage } from './browserSession';

/**
 * Design limit: a snapshot is cut at a line boundary past this many characters, reported as
 * `snapshot_too_long`. Raise it when that is reported for pages whose cut part the agent needed
 * and a `ref` snapshot of the part could not reach it.
 */
export const MAX_SNAPSHOT_CHARS = 30_000;

/** The fields of a CDP `Accessibility.AXNode` the snapshot reads. */
type AXValue = { value?: unknown };
type AXNode = {
  nodeId: string;
  ignored: boolean;
  role?: AXValue;
  name?: AXValue;
  value?: AXValue;
  properties?: { name: string; value: AXValue }[];
  childIds?: string[];
  backendDOMNodeId?: number;
};

/** Roles that only group their children; without a name they get no line of their own. */
const GROUPING_ROLES = new Set(['generic', 'none', 'presentation', 'LabelText', 'RootWebArea']);
const SHOWN_PROPERTIES = ['level', 'checked', 'pressed', 'selected', 'expanded', 'disabled'];

type RefMap = {
  /** The main frame document the refs belong to. */
  loaderId: string;
  byRef: Map<string, number>;
  byNode: Map<number, string>;
};

const refMaps = new WeakMap<WebContents, RefMap>();
/** The next ref number per session; refs keep counting across documents. */
const nextRef = new WeakMap<WebContents, number>();

/**
 * The world this engine's page functions run in, one per frame and document. The page's own
 * world could replace what they call (`Element.prototype.closest`, a getter), so none runs there.
 */
const ENGINE_WORLD = 'pantaray-browser';
/** The objects one page call resolves; it releases them as it ends. */
const CALL_GROUP = 'pantaray-browser-call';

async function mainFrame(cdp: Debugger): Promise<{ id: string; loaderId: string }> {
  const { frameTree } = (await cdp.sendCommand('Page.getFrameTree')) as {
    frameTree: { frame: { id: string; loaderId: string } };
  };
  return frameTree.frame;
}

export async function mainDocumentId(cdp: Debugger): Promise<string> {
  return (await mainFrame(cdp)).loaderId;
}

/** The engine's world in a frame's current document; the main frame's when `frameId` is absent. */
export async function engineWorld(cdp: Debugger, frameId?: string): Promise<number> {
  const { executionContextId } = (await cdp.sendCommand('Page.createIsolatedWorld', {
    frameId: frameId ?? (await mainFrame(cdp)).id,
    worldName: ENGINE_WORLD,
  })) as { executionContextId: number };
  return executionContextId;
}

const oneLine = (text: unknown): string =>
  typeof text === 'string' || typeof text === 'number'
    ? String(text).replace(/\s+/g, ' ').trim()
    : '';

/** The properties shown for a node: `[name]` when true, `[name=value]` otherwise, none when false. */
function propertiesOf(node: AXNode): string {
  const shown: string[] = [];
  for (const name of SHOWN_PROPERTIES) {
    const value = node.properties?.find((property) => property.name === name)?.value.value;
    if (value === undefined || value === false || value === 'false') continue;
    shown.push(value === true || value === 'true' ? `[${name}]` : `[${name}=${oneLine(value)}]`);
  }
  return shown.map((property) => ` ${property}`).join('');
}

/**
 * Renders the tree under `rootId` and names its elements through `refOf`, which commits a ref
 * only for a line that is kept. Pure apart from `refOf`.
 */
export function renderSnapshot(
  nodes: readonly AXNode[],
  rootId: string,
  refOf: (backendNodeId: number) => { ref: string; commit: () => void },
  maxChars = MAX_SNAPSHOT_CHARS
): { text: string; cut: boolean } {
  const byId = new Map(nodes.map((node) => [node.nodeId, node]));
  const lines: string[] = [];
  let length = 0;
  let cut = false;
  const emit = (line: string): boolean => {
    if (length + line.length + 1 > maxChars) {
      cut = true;
      return false;
    }
    lines.push(line);
    length += line.length + 1;
    return true;
  };
  /** `shown` is the text the parent's line already says, which its text children repeat. */
  const walk = (id: string, depth: number, shown: readonly string[], isRoot: boolean): void => {
    const node = byId.get(id);
    if (node === undefined || cut) return;
    const role = oneLine(node.role?.value);
    const name = oneLine(node.name?.value);
    if (role === 'InlineTextBox' || role === 'LineBreak') return;
    if (role === 'StaticText') {
      if (name !== '' && !shown.includes(name)) emit(`${'  '.repeat(depth)}- text: ${name}`);
      return;
    }
    if (!isRoot && (node.ignored || (GROUPING_ROLES.has(role) && name === ''))) {
      for (const child of node.childIds ?? []) walk(child, depth, shown, false);
      return;
    }
    let line = `${'  '.repeat(depth)}- ${role}${name === '' ? '' : ` ${JSON.stringify(name)}`}`;
    line += propertiesOf(node);
    const named = node.backendDOMNodeId === undefined ? null : refOf(node.backendDOMNodeId);
    if (named !== null) line += ` [ref=${named.ref}]`;
    const value = oneLine(node.value?.value);
    if (value !== '' && value !== name) line += `: ${value}`;
    if (!emit(line)) return;
    named?.commit();
    for (const child of node.childIds ?? []) walk(child, depth + 1, [name, value], false);
  };
  // The page's own root gets no line: the answer already carries its title.
  const root = byId.get(rootId);
  if (root !== undefined && oneLine(root.role?.value) === 'RootWebArea') {
    for (const child of root.childIds ?? []) walk(child, 0, [], false);
  } else {
    walk(rootId, 0, [], true);
  }
  return { text: lines.join('\n'), cut };
}

/** The node a ref names in the current document, after checking it is still in the page. */
export async function resolveRef(page: BrowserPage, ref: string): Promise<number> {
  const refs = refMaps.get(page.contents);
  const node = refs?.byRef.get(ref);
  if (refs !== undefined && refs.loaderId !== (await mainDocumentId(page.cdp))) {
    refMaps.delete(page.contents);
  } else if (node !== undefined) {
    // CDP refuses to resolve a node that no longer exists.
    const connected = await callOn(
      page.cdp,
      node,
      'function () { return this.isConnected; }'
    ).catch(() => false);
    if (connected !== true) {
      throw new BrowserOperationError(
        'BROWSER_REF_STALE',
        `The element ${ref} named is no longer in the page.`
      );
    }
    return node;
  }
  throw new BrowserOperationError(
    'BROWSER_REF_UNKNOWN',
    `No snapshot of the current page named ${ref}.`
  );
}

/** An argument of a page function: a JSON value, or a node of the same frame. */
export type PageArgument = { value: unknown } | { node: number };

/**
 * Runs a fixed function of this engine on a node, in the engine's world of the node's frame (the
 * main frame by default); `this` is the node.
 */
export async function callOn(
  cdp: Debugger,
  backendNodeId: number,
  functionDeclaration: string,
  args: PageArgument[] = [],
  frameId?: string
): Promise<unknown> {
  const executionContextId = await engineWorld(cdp, frameId);
  const resolve = async (node: number) => {
    const { object } = (await cdp.sendCommand('DOM.resolveNode', {
      backendNodeId: node,
      executionContextId,
      objectGroup: CALL_GROUP,
    })) as { object: { objectId: string } };
    return object.objectId;
  };
  try {
    const objectId = await resolve(backendNodeId);
    const callArguments = [];
    for (const arg of args) {
      callArguments.push('node' in arg ? { objectId: await resolve(arg.node) } : arg);
    }
    const { result, exceptionDetails } = (await cdp.sendCommand('Runtime.callFunctionOn', {
      objectId,
      functionDeclaration,
      arguments: callArguments,
      returnByValue: true,
    })) as { result: { value?: unknown }; exceptionDetails?: unknown };
    if (exceptionDetails !== undefined) {
      throw new BrowserOperationError('BROWSER_ACTION_FAILED', 'The page refused the operation.');
    }
    return result.value;
  } finally {
    await cdp.sendCommand('Runtime.releaseObjectGroup', { objectGroup: CALL_GROUP });
  }
}

/** The snapshot of the page, or of the subtree `ref` names. */
export async function takeSnapshot(
  page: BrowserPage,
  ref: string | null | undefined
): Promise<{ text: string; cut: boolean }> {
  const subtreeOf = ref == null ? null : await resolveRef(page, ref);
  const loaderId = await mainDocumentId(page.cdp);
  const { nodes } = (await page.cdp.sendCommand('Accessibility.getFullAXTree')) as {
    nodes: AXNode[];
  };
  const root =
    subtreeOf === null ? nodes[0] : nodes.find((node) => node.backendDOMNodeId === subtreeOf);
  if (root === undefined) {
    throw new BrowserOperationError(
      'BROWSER_ACTION_FAILED',
      ref == null ? 'The page has no accessibility tree yet.' : `${ref} is not shown in the page.`
    );
  }
  const previous = refMaps.get(page.contents);
  const kept = previous?.loaderId === loaderId ? previous.byNode : new Map<number, string>();
  const refs: RefMap = { loaderId, byRef: new Map(), byNode: new Map() };
  const refOf = (backendNodeId: number) => {
    const number = nextRef.get(page.contents) ?? 1;
    const ref = kept.get(backendNodeId) ?? `e${number}`;
    const commit = () => {
      if (!kept.has(backendNodeId)) nextRef.set(page.contents, number + 1);
      refs.byRef.set(ref, backendNodeId);
      refs.byNode.set(backendNodeId, ref);
    };
    return { ref, commit };
  };
  const rendered = renderSnapshot(nodes, root.nodeId, refOf);
  refMaps.set(page.contents, refs);
  return rendered;
}
