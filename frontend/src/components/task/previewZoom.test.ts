import { describe, expect, it } from 'vitest';

import { layoutWidth, refitScale, zoomAt } from './previewZoom';

describe('layoutWidth', () => {
  it('lays a deck of fixed 1280px slides out at 1280px in a narrower pane, so its left edge is not cut', () => {
    // Centered in an 800px viewport the slides would spill 240px past the left edge.
    expect(layoutWidth(800, 1280)).toBe(1280);
  });

  it('keeps the pane width for content that fits it', () => {
    expect(layoutWidth(1500, 1280)).toBe(1500);
    expect(layoutWidth(800, 320)).toBe(800);
  });

  it('rounds a fractional narrowest width up, so the content never overflows by a fraction', () => {
    expect(layoutWidth(800, 1279.4)).toBe(1280);
  });
});

describe('zoomAt', () => {
  const contentPoint = (
    view: { scale: number; left: number; top: number },
    x: number,
    y: number
  ) => [(view.left + x) / view.scale, (view.top + y) / view.scale];

  it('keeps the content under the pointer in place', () => {
    const view = { scale: 0.625, left: 0, top: 300 };
    const next = zoomAt(view, 2, { x: 200, y: 120 }, 0.625);

    expect(next.scale).toBe(1.25);
    expect(contentPoint(next, 200, 120)).toEqual(contentPoint(view, 200, 120));
  });

  it('stops zooming out at the fit and zooming in at 8x', () => {
    expect(zoomAt({ scale: 0.7, left: 40, top: 0 }, 0.5, { x: 0, y: 0 }, 0.625).scale).toBe(0.625);
    expect(zoomAt({ scale: 6, left: 0, top: 0 }, 2, { x: 0, y: 0 }, 0.625).scale).toBe(8);
  });
});

describe('refitScale', () => {
  it('lets a view zoomed before the pane narrowed pinch back out to the new fit', () => {
    const zoomed = zoomAt({ scale: 0.625, left: 0, top: 0 }, 2, { x: 0, y: 0 }, 0.625);
    // The pane narrows from 800px to 600px; the 1280px deck now fits at 600 / 1280.
    const fit = 600 / 1280;
    const scale = refitScale({ scale: zoomed.scale, fit: 0.625 }, fit);
    expect(scale).toBe(zoomed.scale);

    expect(zoomAt({ ...zoomed, scale }, 0.1, { x: 0, y: 0 }, fit).scale).toBe(fit);
  });

  it('keeps a fitted view fitted, and lifts a zoom the new fit overtakes', () => {
    expect(refitScale({ scale: 0.625, fit: 0.625 }, 0.5)).toBe(0.5);
    expect(refitScale({ scale: 0.7, fit: 0.625 }, 0.9)).toBe(0.9);
  });
});
