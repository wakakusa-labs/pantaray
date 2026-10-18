import { useEffect, useState } from 'react';

import type {
  ApiDenial,
  IntegrationAccess,
  IntegrationAccountStatus,
  IntegrationToolkitStatus,
  IntegrationToolSetting,
} from '../../../../electron/src/settings/integrationSettingsFetch';

export type IntegrationAccountNotice =
  | { kind: 'save_failed' }
  | { kind: 'not_allowed'; reason: ApiDenial };

type LoadedTools = {
  state: 'loaded';
  tools: IntegrationToolSetting[];
  /** The tool whose block is being saved; it shows the new value until the backend answers. */
  savingSlug: string | null;
  saveFailed: boolean;
};

export type IntegrationToolsState = { state: 'loading' } | { state: 'failed' } | LoadedTools;

export type IntegrationAccountRowState = {
  /** The option being saved; it shows as chosen until the backend answers. */
  pending: IntegrationAccess | null;
  notice: IntegrationAccountNotice | null;
  tools: IntegrationToolsState | null;
};

export type IntegrationSettingsActions = {
  setAccess: (toolkit: string, account: string, access: IntegrationAccess) => void;
  openTools: (toolkit: string, account: string) => void;
  setToolBlocked: (toolkit: string, account: string, slug: string, blocked: boolean) => void;
};

/** Rows are keyed by account: the connection method's account ID is unique across toolkits. */
type Loaded = {
  ownerId: string;
  toolkits: IntegrationToolkitStatus[];
  rows: Record<string, IntegrationAccountRowState>;
};

const EMPTY_ROW: IntegrationAccountRowState = { pending: null, notice: null, tools: null };

export const integrationRow = (rows: Record<string, IntegrationAccountRowState>, account: string) =>
  rows[account] ?? EMPTY_ROW;

/**
 * Loads the owner's connected accounts. `toolkits` stays null, and the section absent, until
 * at least one account is connected: users without one see no change.
 */
export function useIntegrationSettingsController(ownerId: string | null): {
  toolkits: IntegrationToolkitStatus[] | null;
  rows: Record<string, IntegrationAccountRowState>;
  actions: IntegrationSettingsActions;
} {
  const [loaded, setLoaded] = useState<Loaded | null>(null);

  useEffect(() => {
    const api = window.electron?.integrations;
    if (ownerId === null || !api) return;
    let cancelled = false;
    api.getStatus().then(
      (status) => {
        if (cancelled || status.state !== 'configured') return;
        const toolkits = status.toolkits.filter((toolkit) => toolkit.accounts.length > 0);
        if (toolkits.length > 0) setLoaded({ ownerId, toolkits, rows: {} });
      },
      (error: unknown) => {
        // The section is optional: without it the page is today's page.
        console.warn('Failed to load integration settings', error);
      }
    );
    return () => {
      cancelled = true;
    };
  }, [ownerId]);

  // A result that lands after the owner changed belongs to the previous owner's screen.
  const current = loaded !== null && loaded.ownerId === ownerId ? loaded : null;
  const api = window.electron?.integrations;

  const patchRow = (
    forOwner: string,
    account: string,
    change: (row: IntegrationAccountRowState) => Partial<IntegrationAccountRowState>,
    saved?: IntegrationAccountStatus
  ) =>
    setLoaded((prev) => {
      if (prev === null || prev.ownerId !== forOwner) return prev;
      const row = integrationRow(prev.rows, account);
      return {
        ...prev,
        toolkits: saved
          ? prev.toolkits.map((toolkit) => ({
              ...toolkit,
              accounts: toolkit.accounts.map((status) =>
                status.account === account ? saved : status
              ),
            }))
          : prev.toolkits,
        rows: { ...prev.rows, [account]: { ...row, ...change(row) } },
      };
    });

  const setAccess: IntegrationSettingsActions['setAccess'] = (toolkit, account, access) => {
    if (!current || !api || integrationRow(current.rows, account).pending !== null) return;
    const forOwner = current.ownerId;
    const settle = (notice: IntegrationAccountNotice | null, saved?: IntegrationAccountStatus) =>
      patchRow(forOwner, account, () => ({ pending: null, notice }), saved);
    patchRow(forOwner, account, () => ({ pending: access, notice: null }));
    api.setAccountAccess({ toolkit, account, access }).then(
      (result) =>
        result.kind === 'saved'
          ? settle(null, result.account)
          : settle({ kind: 'not_allowed', reason: result.reason }),
      () => settle({ kind: 'save_failed' })
    );
  };

  const openTools: IntegrationSettingsActions['openTools'] = (toolkit, account) => {
    if (!current || !api) return;
    const tools = integrationRow(current.rows, account).tools;
    if (tools !== null && tools.state !== 'failed') return;
    const forOwner = current.ownerId;
    const setTools = (next: IntegrationToolsState) =>
      patchRow(forOwner, account, () => ({ tools: next }));
    setTools({ state: 'loading' });
    api.listAccountTools({ toolkit, account }).then(
      (list) => setTools({ state: 'loaded', tools: list, savingSlug: null, saveFailed: false }),
      () => setTools({ state: 'failed' })
    );
  };

  const setToolBlocked: IntegrationSettingsActions['setToolBlocked'] = (
    toolkit,
    account,
    slug,
    blocked
  ) => {
    if (!current || !api) return;
    const tools = integrationRow(current.rows, account).tools;
    if (tools?.state !== 'loaded' || tools.savingSlug !== null) return;
    const forOwner = current.ownerId;
    const changeTools = (change: (loadedTools: LoadedTools) => LoadedTools) =>
      patchRow(forOwner, account, (row) =>
        row.tools?.state === 'loaded' ? { tools: change(row.tools) } : {}
      );
    changeTools((loadedTools) => ({ ...loadedTools, savingSlug: slug, saveFailed: false }));
    api.setToolBlocked({ toolkit, account, slug, blocked }).then(
      () =>
        changeTools((loadedTools) => ({
          ...loadedTools,
          savingSlug: null,
          tools: loadedTools.tools.map((tool) =>
            tool.slug === slug ? { ...tool, blocked } : tool
          ),
        })),
      () => changeTools((loadedTools) => ({ ...loadedTools, savingSlug: null, saveFailed: true }))
    );
  };

  return {
    toolkits: current?.toolkits ?? null,
    rows: current?.rows ?? {},
    actions: { setAccess, openTools, setToolBlocked },
  };
}
