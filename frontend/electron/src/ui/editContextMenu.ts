import type {
  App,
  BrowserWindow,
  ContextMenuParams,
  MenuItemConstructorOptions,
  WebContents,
} from 'electron';

import type { UiLanguage } from '../ipc/context';
import { getEditMenuCopy } from './mainProcessCopy';

type MenuLike = {
  buildFromTemplate(template: MenuItemConstructorOptions[]): {
    popup(options: { window?: BrowserWindow }): void;
  };
};

type EditTarget = Pick<WebContents, 'cut' | 'copy' | 'paste' | 'selectAll'>;

/**
 * The right-click menu for what was clicked: a text field gets Cut, Copy, Paste and Select All;
 * selected text elsewhere gets Copy; anything else gets no menu. Each item acts on the page that
 * was clicked, which need not be the focused window (an Overlay is a non-activating panel).
 */
export function editContextMenuTemplate(
  params: Pick<ContextMenuParams, 'isEditable' | 'editFlags'>,
  language: UiLanguage,
  target: EditTarget
): MenuItemConstructorOptions[] {
  const copy = getEditMenuCopy(language);
  const { editFlags } = params;
  const copyItem: MenuItemConstructorOptions = {
    label: copy.copy,
    enabled: editFlags.canCopy,
    click: () => target.copy(),
  };
  if (params.isEditable) {
    return [
      { label: copy.cut, enabled: editFlags.canCut, click: () => target.cut() },
      copyItem,
      { label: copy.paste, enabled: editFlags.canPaste, click: () => target.paste() },
      { type: 'separator' },
      { label: copy.selectAll, enabled: editFlags.canSelectAll, click: () => target.selectAll() },
    ];
  }
  return editFlags.canCopy ? [copyItem] : [];
}

/** Electron shows no context menu of its own; every window's pages get this one. */
export function showEditContextMenus(params: {
  app: App;
  menu: MenuLike;
  windowFor: (contents: WebContents) => BrowserWindow | null;
  getUiLanguage: () => UiLanguage;
}): void {
  params.app.on('web-contents-created', (_event, contents) => {
    contents.on('context-menu', (_menuEvent, menuParams) => {
      const template = editContextMenuTemplate(menuParams, params.getUiLanguage(), contents);
      if (template.length === 0) return;
      params.menu
        .buildFromTemplate(template)
        .popup({ window: params.windowFor(contents) ?? undefined });
    });
  });
}
