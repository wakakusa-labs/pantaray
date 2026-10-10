const assert = require('assert');
const fs = require('fs');
const os = require('os');
const path = require('path');
const { test } = require('node:test');

const { openRegisteredWorkspaceFolder } = require('../electron/dist/settings/workspaceFolderOpen.js');

function settingsWith(folders) {
  return async () => ({
    read_access_scope: 'workspace',
    organizations: [],
    projects: [],
    folders: folders.map(([folderId, realPath]) => ({
      folder_id: folderId,
      display_name: path.basename(realPath),
      real_path: realPath,
      canonical_real_path: realPath,
      organization_ids: [],
      project_ids: ['project-1'],
    })),
  });
}

test('openRegisteredWorkspaceFolder opens the registered path of the folder it is named by', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'workspace-open-'));
  const opened = [];
  await openRegisteredWorkspaceFolder('folder-1', {
    getSettings: settingsWith([['folder-1', root]]),
    openPath: async (realPath) => {
      opened.push(realPath);
      return '';
    },
  });
  assert.deepStrictEqual(opened, [root]);
});

test('openRegisteredWorkspaceFolder opens nothing for a folder that is not registered', async () => {
  const opened = [];
  await assert.rejects(
    openRegisteredWorkspaceFolder('/etc', {
      getSettings: settingsWith([['folder-1', os.tmpdir()]]),
      openPath: async (realPath) => {
        opened.push(realPath);
        return '';
      },
    }),
    /not registered/
  );
  assert.deepStrictEqual(opened, []);
});

test('openRegisteredWorkspaceFolder refuses an app bundle, which opening would run', async () => {
  const bundle = path.join(fs.mkdtempSync(path.join(os.tmpdir(), 'workspace-open-')), 'Tool.app');
  fs.mkdirSync(path.join(bundle, 'Contents'), { recursive: true });
  fs.writeFileSync(path.join(bundle, 'Contents', 'Info.plist'), '');
  const opened = [];
  await assert.rejects(
    openRegisteredWorkspaceFolder('folder-1', {
      getSettings: settingsWith([['folder-1', bundle]]),
      openPath: async (realPath) => {
        opened.push(realPath);
        return '';
      },
    }),
    /executable/
  );
  assert.deepStrictEqual(opened, []);
});
