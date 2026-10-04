import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useCallback, useRef, useState } from 'react';
import { afterEach, describe, expect, it } from 'vitest';

import { useDismissablePopover } from './useDismissablePopover';

function PopoverHarness() {
  const triggerRef = useRef<HTMLButtonElement>(null);
  const [isOpen, setIsOpen] = useState(false);
  const onDismiss = useCallback(() => setIsOpen(false), []);
  const { dismiss, popoverRef } = useDismissablePopover({ isOpen, triggerRef, onDismiss });

  return (
    <>
      <button ref={triggerRef} type="button" onClick={() => setIsOpen(true)}>
        Open
      </button>
      {isOpen ? (
        <div ref={popoverRef} role="dialog">
          <input aria-label="First field" />
          <button type="button" onClick={dismiss}>
            Confirm
          </button>
        </div>
      ) : null}
      <button type="button">Outside</button>
    </>
  );
}

/** Passes a new inline callback on every render, as most callers do. */
function InlineCallbackHarness() {
  const triggerRef = useRef<HTMLButtonElement>(null);
  const [isOpen, setIsOpen] = useState(false);
  const [renders, setRenders] = useState(0);
  const [dismissedAt, setDismissedAt] = useState<number | null>(null);
  const { popoverRef } = useDismissablePopover({
    isOpen,
    triggerRef,
    onDismiss: () => {
      setDismissedAt(renders);
      setIsOpen(false);
    },
  });

  return (
    <>
      <button ref={triggerRef} type="button" onClick={() => setIsOpen(true)}>
        Open
      </button>
      {isOpen ? (
        <div ref={popoverRef} role="dialog">
          <input aria-label="First field" />
          <button type="button" onClick={() => setRenders((count) => count + 1)}>
            Rerender
          </button>
        </div>
      ) : null}
      <output aria-label="Dismissed at">{dismissedAt ?? 'open'}</output>
    </>
  );
}

describe('useDismissablePopover', () => {
  afterEach(cleanup);

  it('focuses the first field when opened', async () => {
    const user = userEvent.setup();
    render(<PopoverHarness />);

    await user.click(screen.getByRole('button', { name: 'Open' }));

    await waitFor(() => expect(screen.getByRole('textbox', { name: 'First field' })).toHaveFocus());
  });

  it('dismisses on Escape and restores trigger focus', async () => {
    const user = userEvent.setup();
    render(<PopoverHarness />);

    const trigger = screen.getByRole('button', { name: 'Open' });
    await user.click(trigger);
    fireEvent.keyDown(document, { key: 'Escape' });

    expect(screen.queryByRole('dialog')).toBeNull();
    expect(trigger).toHaveFocus();
  });

  it('dismisses on confirmation and restores trigger focus', async () => {
    const user = userEvent.setup();
    render(<PopoverHarness />);

    const trigger = screen.getByRole('button', { name: 'Open' });
    await user.click(trigger);
    await user.click(screen.getByRole('button', { name: 'Confirm' }));

    expect(screen.queryByRole('dialog')).toBeNull();
    expect(trigger).toHaveFocus();
  });

  it('dismisses on outside pointerdown without changing focus', async () => {
    const user = userEvent.setup();
    render(<PopoverHarness />);

    await user.click(screen.getByRole('button', { name: 'Open' }));
    const outside = screen.getByRole('button', { name: 'Outside' });
    outside.focus();
    fireEvent.pointerDown(outside);

    expect(screen.queryByRole('dialog')).toBeNull();
    expect(outside).toHaveFocus();
  });

  it('keeps focus in place across re-renders and dismisses with the latest callback', async () => {
    const user = userEvent.setup();
    render(<InlineCallbackHarness />);

    await user.click(screen.getByRole('button', { name: 'Open' }));
    const rerender = screen.getByRole('button', { name: 'Rerender' });
    await user.click(rerender);
    await user.click(rerender);

    expect(rerender).toHaveFocus();
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(screen.getByRole('status', { name: 'Dismissed at' })).toHaveTextContent('2');
  });
});
