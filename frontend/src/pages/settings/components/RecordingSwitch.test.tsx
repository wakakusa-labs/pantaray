import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';

import { UiLanguageProvider } from '@/context/UiLanguageContext';
import { RecordingSwitch } from './RecordingSwitch';

function renderSwitch(props: { isCapturing: boolean; canStart: boolean; isProcessing: boolean }) {
  const onChange = vi.fn();
  render(
    <UiLanguageProvider initialLanguage="en">
      <RecordingSwitch {...props} onChange={onChange} />
    </UiLanguageProvider>
  );
  return { onChange, toggle: screen.getByRole('switch') };
}

afterEach(cleanup);

it('can always turn recording off, but on only when recording can start', () => {
  const on = renderSwitch({ isCapturing: true, canStart: false, isProcessing: false });
  expect(on.toggle).toHaveAttribute('aria-checked', 'true');
  fireEvent.click(on.toggle);
  expect(on.onChange).toHaveBeenCalledWith(false);

  cleanup();
  const blocked = renderSwitch({ isCapturing: false, canStart: false, isProcessing: false });
  expect(blocked.toggle).toBeDisabled();

  cleanup();
  const off = renderSwitch({ isCapturing: false, canStart: true, isProcessing: false });
  fireEvent.click(off.toggle);
  expect(off.onChange).toHaveBeenCalledWith(true);
});

it('takes no second change while one is being applied', () => {
  const { toggle } = renderSwitch({ isCapturing: true, canStart: true, isProcessing: true });
  expect(toggle).toBeDisabled();
});
