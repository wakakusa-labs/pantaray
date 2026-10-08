import { createRef, useState } from 'react';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';

import { UiLanguageProvider } from '@/context/UiLanguageContext';
import { ComposerMessageField } from './ComposerMessageField';
import type { ComposerMention } from './composerMentions';

afterEach(() => {
  cleanup();
  delete (window as { electron?: unknown }).electron;
});

function renderField(get: () => Promise<unknown>) {
  (window as { electron?: unknown }).electron = { workspaceSettings: { get } };
  const onKeyDown = vi.fn();
  function Harness() {
    const [value, setValue] = useState('');
    const [mentions, setMentions] = useState<ComposerMention[]>([]);
    // The list is placed relative to the field's form, as in the composer.
    return (
      <form>
        <ComposerMessageField
          id="field"
          textareaRef={createRef()}
          value={value}
          mentions={mentions}
          readOnly={false}
          placeholder=""
          invalid={false}
          describedBy={undefined}
          onChange={(draft, next) => {
            setValue(draft);
            setMentions(next);
          }}
          onKeyDown={onKeyDown}
          onPaste={() => {}}
          onAddProject={() => {}}
        />
      </form>
    );
  }
  render(
    <UiLanguageProvider initialLanguage="en">
      <Harness />
    </UiLanguageProvider>
  );
  return { input: screen.getByRole('textbox') as HTMLTextAreaElement, onKeyDown };
}

// prettier-ignore
const settings = { read_access_scope: 'workspace', organizations: [], folders: [], projects: [{ project_id: 'p1', display_name: 'Aurora Web', sort_order: 0, organization_ids: [] }] };

it('leaves Enter to the IME while composing: no pick, no send', async () => {
  const { input, onKeyDown } = renderField(async () => settings);
  fireEvent.change(input, { target: { value: '＠', selectionStart: 1 } });
  expect(await screen.findByRole('option', { name: 'Aurora Web' })).toBeTruthy();
  fireEvent.keyDown(input, { key: 'Enter', isComposing: true });
  expect(input.value).toBe('＠');
  expect(onKeyDown).not.toHaveBeenCalled();
  fireEvent.keyDown(input, { key: 'Enter' });
  expect(input.value).toBe('Aurora Web ');
  expect(screen.queryByRole('listbox')).toBeNull();
  expect(onKeyDown).not.toHaveBeenCalled();
});

it('tells a failed workspace read apart from an empty workspace', async () => {
  const { input } = renderField(() => Promise.reject(new Error('ipc down')));
  fireEvent.change(input, { target: { value: '@', selectionStart: 1 } });
  expect((await screen.findByRole('alert')).textContent).toBe('Projects could not be loaded.');
  expect(screen.getAllByRole('option').map((option) => option.textContent)).toEqual([
    'Add project',
  ]);
});
