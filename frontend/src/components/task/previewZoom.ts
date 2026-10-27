import { useCallback, useEffect, useRef, type SyntheticEvent } from 'react';

/*
 * The document is untrusted content. The sandbox runs no script, and this policy stops it loading
 * anything over the network: only inline styles and data: images render. A policy can only be
 * tightened by a later one, so the document's own <meta> tags cannot loosen it.
 *
 * The sandbox keeps the app's origin (allow-same-origin, never with allow-scripts) so the pane can
 * measure the laid-out page and hear a pinch over it. Without script the document cannot use that
 * origin: it reaches no storage, cookies or API, its requests are refused by the policy, and the
 * sandbox still blocks forms, popups, plugins and navigating the app.
 */
const HTML_PREVIEW_POLICY =
  "default-src 'none'; style-src 'unsafe-inline'; img-src data:; font-src data:";
const POLICY_META = `<meta http-equiv="Content-Security-Policy" content="${HTML_PREVIEW_POLICY}">`;
const LEADING_DOCTYPE = /^\s*<!doctype[^>]*>/i;

/** The policy goes first so it applies before any element; after a doctype, to keep standards mode. */
export function htmlPreviewDocument(html: string): string {
  const doctype = LEADING_DOCTYPE.exec(html)?.[0] ?? '';
  return `${doctype}${POLICY_META}${html.slice(doctype.length)}`;
}

type Size = Readonly<{ width: number; height: number }>;
type Point = Readonly<{ x: number; y: number }>;
/** The content's scale and the pane's scroll offset. */
type ZoomView = Readonly<{ scale: number; left: number; top: number }>;
/** The size the content is laid out at, and the scale that fits its width to the pane. */
type Layout = Readonly<{ natural: Size; fit: number }>;

const MAX_SCALE = 8;

/**
 * The width content is laid out at: the pane's, or the content's narrowest width when that is
 * wider (a slide fixed at 1280px). Laid out any narrower, centered content spills past the left
 * edge, where no scrolling reaches.
 */
export function layoutWidth(paneWidth: number, minContentWidth: number): number {
  return Math.max(paneWidth, Math.ceil(minContentWidth));
}

/**
 * Scales by `factor` while the content under `pointer` (pane coordinates) stays put. Zooming out
 * stops at the fit; the pane clamps an offset past the content's edges.
 */
export function zoomAt(view: ZoomView, factor: number, pointer: Point, fit: number): ZoomView {
  const scale = Math.min(MAX_SCALE, Math.max(fit, view.scale * factor));
  const ratio = scale / view.scale;
  return {
    scale,
    left: (view.left + pointer.x) * ratio - pointer.x,
    top: (view.top + pointer.y) * ratio - pointer.y,
  };
}

function withFit(pane: Size, natural: Size): Layout {
  return { natural, fit: pane.width / natural.width };
}

/** Sizes `box`, which the frame fills, while measuring the page. */
function pageLayout(frame: HTMLIFrameElement, box: HTMLElement, pane: Size): Layout | null {
  const root = frame.contentDocument?.documentElement;
  if (!root) return null;
  const inline = root.style.width;
  root.style.width = 'min-content';
  const width = layoutWidth(pane.width, root.getBoundingClientRect().width);
  root.style.width = inline;
  const paneHeight = (pane.height * width) / pane.width;
  box.style.width = `${width}px`;
  box.style.height = `${paneHeight}px`;
  const height = root.scrollHeight;
  box.style.height = `${height}px`;
  // A page sized by the viewport's height (a 100vh section) grows with the frame, so it keeps
  // the pane's height and scrolls inside, as a browser shows it.
  const sizedByViewport = root.scrollHeight > height;
  return withFit(pane, { width, height: sizedByViewport ? paneHeight : height });
}

function imageLayout(image: HTMLImageElement, pane: Size): Layout {
  return withFit(pane, {
    width: layoutWidth(pane.width, image.naturalWidth),
    height: image.naturalHeight,
  });
}

/**
 * Shows a page or an image laid out at its own size, scaled to fit the pane's width. A pinch
 * (Chromium's ctrl+wheel) zooms at the pointer, and the viewport scrolls the scaled content, so
 * every side stays reachable. New content, and a resize while not zoomed in, fit it again.
 *
 * The viewport holds the extent (the scaled size), which holds the box (the content's own size,
 * scaled); the frame or image fills the box.
 */
export function usePreviewZoom() {
  const viewportRef = useRef<HTMLDivElement>(null);
  const extentRef = useRef<HTMLDivElement>(null);
  const boxRef = useRef<HTMLDivElement>(null);
  const measureRef = useRef<((pane: Size) => Layout | null) | null>(null);
  const zoomRef = useRef<Readonly<{ layout: Layout; scale: number }> | null>(null);

  // Sizes are set here, not rendered, so each pinch step lands before the next one reads them.
  const show = useCallback((layout: Layout, scale: number) => {
    const extent = extentRef.current;
    const box = boxRef.current;
    if (!extent || !box) return;
    const { width, height } = layout.natural;
    box.style.width = `${width}px`;
    box.style.height = `${height}px`;
    box.style.transform = `scale(${scale})`;
    extent.style.width = `${width * scale}px`;
    extent.style.height = `${height * scale}px`;
  }, []);

  const fit = useCallback(() => {
    const viewport = viewportRef.current;
    const layout = viewport
      ? measureRef.current?.({ width: viewport.clientWidth, height: viewport.clientHeight })
      : null;
    if (!viewport || !layout) return;
    const previous = zoomRef.current;
    zoomRef.current = { layout, scale: layout.fit };
    show(layout, layout.fit);
    viewport.scrollLeft = 0;
    // Same place, at the new scale, when a new revision or a resize fits it again.
    viewport.scrollTop = previous ? (viewport.scrollTop * layout.fit) / previous.scale : 0;
  }, [show]);

  const zoomBy = useCallback(
    (event: WheelEvent, pointer: Point) => {
      const viewport = viewportRef.current;
      const zoom = zoomRef.current;
      if (!event.ctrlKey || !viewport || !zoom) return;
      event.preventDefault();
      // Chromium sends a trackpad pinch by `scale` as deltaY = -100·ln(scale).
      const next = zoomAt(
        { scale: zoom.scale, left: viewport.scrollLeft, top: viewport.scrollTop },
        Math.exp(-event.deltaY / 100),
        pointer,
        zoom.layout.fit
      );
      zoomRef.current = { layout: zoom.layout, scale: next.scale };
      show(zoom.layout, next.scale);
      viewport.scrollLeft = next.left;
      viewport.scrollTop = next.top;
    },
    [show]
  );

  // Inside the frame the pointer is in the page's own, unscaled pixels from its top-left corner.
  const onFrameWheel = useCallback(
    (event: WheelEvent) => {
      const viewport = viewportRef.current;
      const scale = zoomRef.current?.scale;
      if (!viewport || scale === undefined) return;
      zoomBy(event, {
        x: event.clientX * scale - viewport.scrollLeft,
        y: event.clientY * scale - viewport.scrollTop,
      });
    },
    [zoomBy]
  );

  useEffect(() => {
    const viewport = viewportRef.current;
    if (!viewport) return;
    const onWheel = (event: WheelEvent) => {
      const rect = viewport.getBoundingClientRect();
      zoomBy(event, { x: event.clientX - rect.left, y: event.clientY - rect.top });
    };
    viewport.addEventListener('wheel', onWheel, { passive: false });
    const observer = new ResizeObserver(() => {
      const zoom = zoomRef.current;
      if (zoom && zoom.scale === zoom.layout.fit) fit();
    });
    observer.observe(viewport);
    return () => {
      viewport.removeEventListener('wheel', onWheel);
      observer.disconnect();
    };
  }, [fit, zoomBy]);

  const onFrameLoad = useCallback(
    (event: SyntheticEvent<HTMLIFrameElement>) => {
      const frame = event.currentTarget;
      const box = boxRef.current;
      if (!box) return;
      measureRef.current = (pane) => pageLayout(frame, box, pane);
      frame.contentWindow?.addEventListener('wheel', onFrameWheel, { passive: false });
      fit();
    },
    [fit, onFrameWheel]
  );

  const onImageLoad = useCallback(
    (event: SyntheticEvent<HTMLImageElement>) => {
      const image = event.currentTarget;
      measureRef.current = (pane) => imageLayout(image, pane);
      fit();
    },
    [fit]
  );

  return { viewportRef, extentRef, boxRef, onFrameLoad, onImageLoad };
}
