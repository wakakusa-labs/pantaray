import { cleanup, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { ReactNode } from 'react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';

import { UiLanguageProvider } from '@/context/UiLanguageContext';

import { OrganizationDialog } from './OrganizationDialog';
import { OrganizationRail } from './OrganizationRail';

const ja = (ui: ReactNode) => <UiLanguageProvider initialLanguage="ja">{ui}</UiLanguageProvider>;
const ICON = 'data:image/webp;base64,UklGRg==';

/** What the canvas encodes the picked image to: a WebP data URL holding this many bytes. */
let encodedBytes = 0;
const decodable = { width: 300, height: 200, close: vi.fn() };

beforeEach(() => {
  encodedBytes = 1024;
  HTMLDialogElement.prototype.showModal = function (this: HTMLDialogElement) {
    this.setAttribute('open', '');
  };
  vi.stubGlobal(
    'createImageBitmap',
    vi.fn(async (file: Blob) => {
      if (file.type !== 'image/png') throw new DOMException('undecodable', 'InvalidStateError');
      return decodable;
    })
  );
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue({
    drawImage: vi.fn(),
  } as unknown as CanvasRenderingContext2D);
  vi.spyOn(HTMLCanvasElement.prototype, 'toDataURL').mockImplementation(
    () => `data:image/webp;base64,${'A'.repeat(Math.ceil((encodedBytes * 4) / 3))}`
  );
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

it('shows an organization by its icon when it has one, and by its initial otherwise; ＋ adds one', async () => {
  const onAdd = vi.fn();
  render(
    ja(
      <OrganizationRail
        organizations={[
          { organization_id: 'org-1', display_name: 'wakakusa', icon_data_url: ICON },
          { organization_id: 'org-2', display_name: 'llm-jp', icon_data_url: null },
        ]}
        selected={{ scope: 'all' }}
        onSelect={vi.fn()}
        onAdd={onAdd}
      />
    )
  );
  const withIcon = screen.getByRole('button', { name: 'wakakusa' });
  expect(withIcon.querySelector('img')).toHaveAttribute('src', ICON);
  expect(withIcon).not.toHaveTextContent('W');
  expect(screen.getByRole('button', { name: 'llm-jp' })).toHaveTextContent(/^L$/u);
  // ＋ is not one of the scopes to choose from.
  const add = screen.getByRole('button', { name: '組織を追加' });
  expect(
    within(screen.getByRole('group', { name: '組織' })).queryByRole('button', {
      name: '組織を追加',
    })
  ).toBeNull();
  await userEvent.click(add);
  expect(onAdd).toHaveBeenCalledOnce();
});

function renderDialog(onSave = vi.fn(async () => true)) {
  render(
    ja(
      <OrganizationDialog
        organization={{ organization_id: 'org-1', display_name: 'Xer', icon_data_url: ICON }}
        organizations={[{ organization_id: 'org-1', display_name: 'Xer', icon_data_url: ICON }]}
        anchorId="anchor"
        errorMessage={null}
        onSave={onSave}
        onClose={vi.fn()}
      />
    )
  );
  const dialog = screen.getByRole('dialog', { name: 'Xer を編集' });
  const pick = (file: File) =>
    userEvent.upload(within(dialog).getByLabelText('画像を選ぶ', { selector: 'input' }), file);
  return { dialog, pick, onSave };
}

const png = new File(['png'], 'logo.png', { type: 'image/png' });

it('takes a picked image as the icon only when it decodes and fits the 64 KiB cap once shrunk', async () => {
  const { dialog, pick, onSave } = renderDialog();

  // Bytes that only claim to be an image.
  await pick(new File(['not a jpeg'], 'broken.jpg', { type: 'image/jpeg' }));
  expect(within(dialog).getByRole('alert')).toHaveTextContent('この画像は読み込めませんでした。');

  encodedBytes = 64 * 1024 + 1;
  await pick(png);
  expect(within(dialog).getByRole('alert')).toHaveTextContent('縮めても大きすぎる画像です。');

  // The cap is on the bytes, not the base64 text, which is a third longer.
  encodedBytes = 60 * 1024;
  await pick(png);
  expect(within(dialog).queryByRole('alert')).toBeNull();
  await userEvent.click(within(dialog).getByRole('button', { name: '保存' }));
  const [, icon] = onSave.mock.calls[0] as unknown as [string, string];
  expect(icon).toMatch(/^data:image\/webp;base64,/u);
  expect(
    within(dialog).getByRole('button', { name: '画像を選ぶ' }).querySelector('img')
  ).toHaveAttribute('src', icon);
});

it('removes the icon, and says why a save failed without closing', async () => {
  const onSave = vi.fn(async () => false);
  renderDialog(onSave);
  const dialog = screen.getByRole('dialog');

  await userEvent.click(within(dialog).getByRole('button', { name: '画像を外す' }));
  expect(within(dialog).queryByRole('button', { name: '画像を外す' })).toBeNull();
  await userEvent.click(within(dialog).getByRole('button', { name: '保存' }));
  expect(onSave).toHaveBeenCalledWith('Xer', null);
  expect(within(dialog).getByRole('alert')).toHaveTextContent('変更を保存できませんでした。');
  expect(screen.getByRole('dialog')).toBeInTheDocument();
});
