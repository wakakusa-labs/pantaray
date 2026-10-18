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
  | { kind: 'not_allowed'; reason: ApiDenial }
  /** Neither the save's answer nor a re-read arrived: the stored access is not known. */
  | { kind: 'unconfirmed' };

type LoadedTools = {
  state: 'loaded';
  tools: IntegrationToolSetting[];
  /** The tool whose block is being saved; it shows the new value until the backend answers. */
  savingSlug: string | null;
  saveFailed: boolean;
};

export type IntegrationToolsState =
  | { state: 'loading' }
  /** `unconfirmed`: a block's save and the re-read after it both failed. */
  | { state: 'failed'; unconfirmed: boolean }
  | LoadedTools;

export type IntegrationAccountRowState = {
  /** A save or a re-read is in flight; the access control takes no new choice until it ends. */
  busy: boolean;
  /** The option being saved; it shows as chosen until the backend answers. */
  pending: IntegrationAccess | null;
  notice: IntegrationAccountNotice | null;
  tools: IntegrationToolsState | null;
};

export type IntegrationSettingsActions = {
  setAccess: (toolkit: string, account: string, access: IntegrationAccess) => void;
  /** Re-reads an account's stored access after it could not be confirmed. */
  recheckAccess: (toolkit: string, account: string) => void;
  /** Loads an account's tools once; after a failed load it tries again. */
  loadTools: (toolkit: string, account: string) => void;
  setToolBlocked: (toolkit: string, account: string, slug: string, blocked: boolean) => void;
};

/** Rows are keyed by account: the connection method's account ID is unique across toolkits. */
type Loaded = {
  ownerId: string;
  toolkits: IntegrationToolkitStatus[];
  rows: Record<string, IntegrationAccountRowState>;
};

const EMPTY_ROW: IntegrationAccountRowState = {
  busy: false,
  pending: null,
  notice: null,
  tools: null,
};

export const integrationRow = (rows: Record<string, IntegrationAccountRowState>, account: string) =>
  rows[account] ?? EMPTY_ROW;

/** Loads the owner's toolkits and accounts; `toolkits` is null until the gateway is configured. */
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
        if (!cancelled && status.state === 'configured') {
          setLoaded({ ownerId, toolkits: status.toolkits, rows: {} });
        }
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

  // A failed answer does not prove a failed save: the backend writes before it answers, so
  // the stored access is read back. `requested` is null for a plain re-read.
  const confirmAccess = (
    forOwner: string,
    toolkit: string,
    account: string,
    requested: IntegrationAccess | null
  ) => {
    if (!api) return;
    const unconfirmed = () =>
      patchRow(forOwner, account, () => ({
        busy: false,
        pending: null,
        notice: { kind: 'unconfirmed' },
      }));
    api.getStatus().then((status) => {
      const stored =
        status.state === 'configured'
          ? status.toolkits
              .find((entry) => entry.toolkit === toolkit)
              ?.accounts.find((entry) => entry.account === account)
          : undefined;
      if (!stored) return unconfirmed();
      const saved = requested === null || stored.access === requested;
      patchRow(
        forOwner,
        account,
        () => ({ busy: false, pending: null, notice: saved ? null : { kind: 'save_failed' } }),
        stored
      );
    }, unconfirmed);
  };

  const setAccess: IntegrationSettingsActions['setAccess'] = (toolkit, account, access) => {
    if (!current || !api || integrationRow(current.rows, account).busy) return;
    const forOwner = current.ownerId;
    patchRow(forOwner, account, () => ({ busy: true, pending: access, notice: null }));
    api.setAccountAccess({ toolkit, account, access }).then(
      (result) =>
        result.kind === 'saved'
          ? patchRow(
              forOwner,
              account,
              () => ({ busy: false, pending: null, notice: null }),
              result.account
            )
          : patchRow(forOwner, account, () => ({
              busy: false,
              pending: null,
              notice: { kind: 'not_allowed', reason: result.reason },
            })),
      () => confirmAccess(forOwner, toolkit, account, access)
    );
  };

  const recheckAccess: IntegrationSettingsActions['recheckAccess'] = (toolkit, account) => {
    if (!current || integrationRow(current.rows, account).busy) return;
    patchRow(current.ownerId, account, () => ({ busy: true }));
    confirmAccess(current.ownerId, toolkit, account, null);
  };

  const loadTools: IntegrationSettingsActions['loadTools'] = (toolkit, account) => {
    if (!current || !api) return;
    const tools = integrationRow(current.rows, account).tools;
    if (tools !== null && tools.state !== 'failed') return;
    const forOwner = current.ownerId;
    const setTools = (next: IntegrationToolsState) =>
      patchRow(forOwner, account, () => ({ tools: next }));
    setTools({ state: 'loading' });
    api.listAccountTools({ toolkit, account }).then(
      (list) => setTools({ state: 'loaded', tools: list, savingSlug: null, saveFailed: false }),
      () => setTools({ state: 'failed', unconfirmed: false })
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
      // As with access: the block may be stored although its answer was lost, so read it back.
      () =>
        api.listAccountTools({ toolkit, account }).then(
          (list) =>
            patchRow(forOwner, account, () => ({
              tools: {
                state: 'loaded',
                tools: list,
                savingSlug: null,
                saveFailed: list.find((tool) => tool.slug === slug)?.blocked !== blocked,
              },
            })),
          () =>
            patchRow(forOwner, account, () => ({ tools: { state: 'failed', unconfirmed: true } }))
        )
    );
  };

  return {
    toolkits: current?.toolkits ?? null,
    rows: current?.rows ?? {},
    actions: { setAccess, recheckAccess, loadTools, setToolBlocked },
  };
}
