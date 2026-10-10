import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';

import { useShowHistoryItem } from './useShowHistoryItem';

let showItem: ((payload: unknown) => void) | null;

beforeEach(() => {
  showItem = null;
  window.electron = {
    history: {
      onShowItem: (callback: (payload: unknown) => void) => {
        showItem = callback;
        return () => {
          showItem = null;
        };
      },
    },
  } as unknown as Window['electron'];
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

function Location() {
  const location = useLocation();
  return <output aria-label="location">{`${location.pathname}${location.search}`}</output>;
}

// The main window's shape: the drafts are held above the pages, as ChatSessionProvider holds them.
function MainWindow() {
  useShowHistoryItem();
  return (
    <>
      <textarea aria-label="draft" defaultValue="" />
      <Routes>
        <Route path="/history" element={<p>history</p>} />
        <Route path="/workspace" element={<p>workspace</p>} />
      </Routes>
      <Location />
    </>
  );
}

it('selects the work main names on History, in place, keeping what the window holds', () => {
  render(
    <MemoryRouter initialEntries={['/workspace']}>
      <MainWindow />
    </MemoryRouter>
  );
  const draft = screen.getByRole('textbox', { name: 'draft' });
  fireEvent.change(draft, { target: { value: 'half-written message' } });

  act(() => showItem?.({ item: 'action:act 1' }));

  expect(screen.getByRole('status', { name: 'location' })).toHaveTextContent(
    '/history?item=action:act%201'
  );
  expect(screen.getByText('history')).toBeInTheDocument();
  expect(screen.getByRole('textbox', { name: 'draft' })).toBe(draft);
  expect(draft).toHaveValue('half-written message');
});

it('ignores a request that does not name a work', () => {
  vi.spyOn(console, 'error').mockImplementation(() => undefined);
  render(
    <MemoryRouter initialEntries={['/workspace']}>
      <MainWindow />
    </MemoryRouter>
  );

  for (const payload of [
    { item: 'chat' },
    { item: 'action:' },
    { item: 'action: act-1' },
    { item: 'conversation:act-1' },
    { actionId: 'act-1' },
    'action:act-1',
    null,
  ]) {
    act(() => showItem?.(payload));
  }

  expect(screen.getByRole('status', { name: 'location' })).toHaveTextContent(/^\/workspace$/);
});
