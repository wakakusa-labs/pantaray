import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';

import { MenuButton } from './MenuButton';

afterEach(cleanup);

function renderChoices(onSelect: (value: string) => void) {
  const item = (key: string, checked: boolean, unavailableReason: string | null = null) => ({
    key,
    title: key,
    checked,
    unavailableReason,
    onSelect: () => onSelect(key),
  });
  render(
    <MenuButton
      label="Access: Read"
      items={[item('Off', false), item('Read', true), item('Write', false, 'Not allowed')]}
    >
      Read
    </MenuButton>
  );
  return screen.getByRole('button', { name: 'Access: Read' });
}

it('opens on the checked item, moves with the arrows and closes back to the button', () => {
  const onSelect = vi.fn();
  const trigger = renderChoices(onSelect);

  fireEvent.keyDown(trigger, { key: 'ArrowDown' });
  expect(screen.getByRole('menuitemradio', { name: 'Read' })).toHaveFocus();

  const menu = screen.getByRole('menu');
  fireEvent.keyDown(menu, { key: 'ArrowDown' });
  expect(screen.getByRole('menuitemradio', { name: 'Write' })).toHaveFocus();
  fireEvent.keyDown(menu, { key: 'ArrowDown' });
  expect(screen.getByRole('menuitemradio', { name: 'Off' })).toHaveFocus();

  fireEvent.keyDown(menu, { key: 'Escape' });
  expect(screen.queryByRole('menu')).not.toBeInTheDocument();
  expect(trigger).toHaveFocus();
  expect(onSelect).not.toHaveBeenCalled();
});

it('never chooses an unavailable item, and chooses an available one', () => {
  const onSelect = vi.fn();
  const trigger = renderChoices(onSelect);
  fireEvent.click(trigger);

  const write = screen.getByRole('menuitemradio', { name: 'Write' });
  expect(write).toHaveAttribute('aria-disabled', 'true');
  expect(write).toHaveAccessibleDescription('Not allowed');
  fireEvent.click(write);
  expect(onSelect).not.toHaveBeenCalled();
  expect(screen.getByRole('menu')).toBeInTheDocument();

  fireEvent.click(screen.getByRole('menuitemradio', { name: 'Off' }));
  expect(onSelect).toHaveBeenCalledWith('Off');
  expect(screen.queryByRole('menu')).not.toBeInTheDocument();
  expect(trigger).toHaveFocus();
});
