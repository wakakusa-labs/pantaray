const assert = require('assert');
const { test } = require('node:test');

const {
  createWorkspaceSettingsFetcher,
} = require('../electron/dist/settings/workspaceSettingsFetch.js');

function validSettings() {
  return {
    read_access_scope: 'workspace',
    organizations: [{ organization_id: 'org-1', display_name: 'Org' }],
    projects: [
      {
        project_id: 'project-1',
        display_name: 'Project',
        sort_order: 0,
        organization_ids: ['org-1'],
      },
    ],
    folders: [
      {
        folder_id: 'folder-1',
        display_name: 'Folder',
        real_path: '/tmp/folder',
        canonical_real_path: '/tmp/folder',
        organization_ids: [],
        project_ids: ['project-1'],
      },
    ],
  };
}

test('workspace settings fetcher sends the full project order contract', async () => {
  const calls = [];
  const fetcher = createWorkspaceSettingsFetcher({
    getUserId: () => 'user/1',
    requestJson: async (request) => {
      calls.push(request);
      return { project_ids: ['project-b', 'project-a'] };
    },
  });

  const response = await fetcher.reorderProjects({
    projectIds: ['project-b', 'project-a'],
  });

  assert.deepEqual(response, { project_ids: ['project-b', 'project-a'] });
  assert.deepEqual(calls, [
    {
      path: '/v1/agents/users/user%2F1/workspace-settings/projects/order',
      method: 'PUT',
      body: { project_ids: ['project-b', 'project-a'] },
    },
  ]);
});

test('workspace settings fetcher rejects malformed nested project data', async () => {
  const invalid = validSettings();
  delete invalid.projects[0].sort_order;
  const fetcher = createWorkspaceSettingsFetcher({
    getUserId: () => 'user-1',
    requestJson: async () => invalid,
  });

  await assert.rejects(fetcher.get(), /Workspace settings response is invalid/);
});

test('workspace settings fetcher accepts deeply valid settings', async () => {
  const settings = validSettings();
  const fetcher = createWorkspaceSettingsFetcher({
    getUserId: () => 'user-1',
    requestJson: async () => settings,
  });

  assert.deepEqual(await fetcher.get(), settings);
});

test('command network settings use the authenticated client with a finite timeout', async (t) => {
  const { createLocalBackendClient } = require('../electron/dist/localBackend/client.js');
  const calls = [];
  t.mock.method(globalThis, 'fetch', async (url, init) => {
    calls.push({ url, init });
    return new Response(JSON.stringify({ command_network_enabled: init.method === 'GET' }));
  });
  const client = createLocalBackendClient({
    getRuntimeBackendUrl: () => 'http://127.0.0.1:61131',
    getLocalApiToken: () => 'local-api-token',
    getRuntimeState: () => ({ status: 'ready', message: null }),
  });
  const fetcher = createWorkspaceSettingsFetcher({
    requestJson: async (request) => {
      assert.equal(request.timeoutMs, 10_000);
      return client.requestJson(request);
    },
    getUserId: () => 'user/1',
  });
  assert.deepEqual(await fetcher.getCommandNetwork(), { command_network_enabled: true });
  assert.deepEqual(await fetcher.updateCommandNetwork(false), { command_network_enabled: false });
  assert.equal(calls.length, 2);
  for (const { url, init } of calls) {
    assert.equal(
      url,
      'http://127.0.0.1:61131/v1/agents/users/user%2F1/workspace-settings/command-network'
    );
    assert.equal(init.headers.Authorization, 'Bearer local-api-token');
    assert.ok(init.signal instanceof AbortSignal);
    assert.equal(init.redirect, 'error');
  }
  assert.equal(calls[0].init.method, 'GET');
  assert.equal(calls[1].init.method, 'PUT');
  assert.equal(calls[1].init.body, JSON.stringify({ command_network_enabled: false }));
});

test('command network settings reject malformed responses on reads and writes', async () => {
  for (const response of [
    null,
    {},
    { command_network_enabled: 'false' },
    { command_network_enabled: 1 },
  ]) {
    const fetcher = createWorkspaceSettingsFetcher({
      getUserId: () => 'user-1',
      requestJson: async () => response,
    });
    await assert.rejects(fetcher.getCommandNetwork(), /response is invalid/);
    await assert.rejects(fetcher.updateCommandNetwork(false), /response is invalid/);
  }
});

test('command network settings propagate timeout and storage errors without assuming a value', async () => {
  const fetcher = createWorkspaceSettingsFetcher({
    getUserId: () => 'user-1',
    requestJson: async () => {
      throw new Error('request failed');
    },
  });
  await assert.rejects(fetcher.getCommandNetwork(), /request failed/);
  await assert.rejects(fetcher.updateCommandNetwork(false), /request failed/);
});

test('workspace settings fetcher answers a taken project name as data, not as the project', async () => {
  const { LocalBackendRequestError } = require('../electron/dist/localBackend/client.js');
  const answers = [
    new LocalBackendRequestError('taken', 409),
    new LocalBackendRequestError('bad', 400),
  ];
  const fetcher = createWorkspaceSettingsFetcher({
    getUserId: () => 'user-1',
    requestJson: async () => {
      throw answers.shift();
    },
  });

  assert.deepStrictEqual(
    await fetcher.createProject({ displayName: 'Core', organizationIds: [] }),
    {
      errorCode: 'PROJECT_NAME_TAKEN',
    }
  );
  await assert.rejects(fetcher.createProject({ displayName: 'Core', organizationIds: [] }), /bad/);
});
