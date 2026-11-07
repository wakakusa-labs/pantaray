import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, it, vi } from 'vitest';

import { LineTextField } from './LineTextField';

afterEach(cleanup);

/** A submit the test answers, to look at the field while it is still deciding. */
function deferredSubmit() {
  let answer!: (taken: boolean) => void;
  const onSubmit = vi.fn(
    () =>
      new Promise<boolean>((resolve) => {
        answer = resolve;
      })
  );
  return { onSubmit, answer: (taken: boolean) => act(async () => answer(taken)) };
}

it('leaves Enter to the IME while it composes and submits on the Enter after', async () => {
  const onSubmit = vi.fn(async () => true);
  render(<LineTextField label="Add a TODO" onSubmit={onSubmit} />);
  const field = screen.getByRole('textbox', { name: 'Add a TODO' });

  await userEvent.type(field, 'かいぎ');
  fireEvent.keyDown(field, { key: 'Enter', isComposing: true });
  fireEvent.keyDown(field, { key: 'Enter', keyCode: 229 });
  expect(onSubmit).not.toHaveBeenCalled();

  await act(async () => fireEvent.keyDown(field, { key: 'Enter' }));
  expect(onSubmit).toHaveBeenCalledExactlyOnceWith('かいぎ');
});

it('empties an add field only once the submit took the text', async () => {
  const { onSubmit, answer } = deferredSubmit();
  render(<LineTextField label="Add a TODO" onSubmit={onSubmit} />);
  const field = screen.getByRole('textbox', { name: 'Add a TODO' });

  await userEvent.type(field, 'Call Aoba{Enter}');
  expect(field).toHaveValue('Call Aoba');
  await answer(false);
  expect(field).toHaveValue('Call Aoba');

  await userEvent.keyboard('{Enter}');
  await answer(true);
  expect(field).toHaveValue('');
});

it('runs one submit at a time, from Enter or from leaving the field', async () => {
  const { onSubmit, answer } = deferredSubmit();
  render(<LineTextField label="Name" initialText="Aoba" submitOnBlur onSubmit={onSubmit} />);
  const field = screen.getByRole('textbox', { name: 'Name' });

  field.focus();
  await userEvent.keyboard('{Enter}{Enter}');
  await userEvent.tab();
  expect(onSubmit).toHaveBeenCalledOnce();
  await answer(false);

  field.focus();
  await userEvent.tab();
  expect(onSubmit).toHaveBeenCalledTimes(2);
});

it('cancels on Escape and does not cancel again or submit when focus then leaves', async () => {
  const onSubmit = vi.fn(async () => true);
  const onCancel = vi.fn();
  render(<LineTextField label="Name" onSubmit={onSubmit} onCancel={onCancel} />);
  const field = screen.getByRole('textbox', { name: 'Name' });

  field.focus();
  fireEvent.keyDown(field, { key: 'Escape', isComposing: true });
  expect(onCancel).not.toHaveBeenCalled();
  await userEvent.keyboard('{Escape}');
  await userEvent.tab();
  expect(onCancel).toHaveBeenCalledOnce();
  expect(onSubmit).not.toHaveBeenCalled();
});

it('keeps a field without cancel usable after Escape', async () => {
  const onSubmit = vi.fn(async () => true);
  render(<LineTextField label="Add a TODO" onSubmit={onSubmit} />);

  await userEvent.type(screen.getByRole('textbox', { name: 'Add a TODO' }), 'x{Escape}{Enter}');
  expect(onSubmit).toHaveBeenCalledExactlyOnceWith('x');
});
