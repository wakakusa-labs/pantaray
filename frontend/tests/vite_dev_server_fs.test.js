const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const test = require('node:test');

test(
  'import analysis does not authorize an outside file with a colliding browser path',
  { skip: process.platform === 'win32' },
  async () => {
    const { createServer } = await import('vite');
    const fixtureDir = fs.realpathSync.native(
      fs.mkdtempSync(path.join(os.tmpdir(), 'pantaray-vite-fs-'))
    );
    const root = path.join(fixtureDir, 'project');
    const outsideFile = path.join(fixtureDir, 'outside.js');
    const outsideMarker = 'PANTARAY_OUTSIDE_FS_MARKER';
    const decoyMarker = 'PANTARAY_ALLOWED_DECOY';
    let server;

    try {
      // GHSA-rq7h-c2jc-7f22: the decoy's browser URL is the outside absolute path.
      const decoyFile = path.join(root, outsideFile.slice(1));
      fs.mkdirSync(path.dirname(decoyFile), { recursive: true });
      fs.writeFileSync(outsideFile, `export default "${outsideMarker}";\n`);
      fs.writeFileSync(decoyFile, `export default "${decoyMarker}";\n`);
      fs.writeFileSync(
        path.join(root, 'main.js'),
        `import value from ${JSON.stringify(`.${outsideFile}`)}; export default value;\n`
      );

      server = await createServer({
        configFile: false,
        root,
        logLevel: 'silent',
        server: {
          host: '127.0.0.1',
          port: 0,
          strictPort: true,
          fs: { strict: true, allow: [root] },
        },
      });
      await server.listen();
      const address = server.httpServer.address();
      assert.ok(address && typeof address === 'object');
      const origin = `http://127.0.0.1:${address.port}`;
      const outsideUrl = `${origin}/@fs${outsideFile}`;

      const beforeImport = await fetch(outsideUrl);
      assert.equal(beforeImport.status, 403, 'outside file must start unauthorized');
      assert.ok(!(await beforeImport.text()).includes(outsideMarker));

      const main = await fetch(`${origin}/main.js`);
      assert.equal(main.status, 200, 'the in-root import must be analyzed successfully');
      assert.ok(
        decodeURI(await main.text()).includes(`from "${outsideFile}"`),
        'the import must rewrite to the colliding root-relative browser URL'
      );
      const decoy = await fetch(`${origin}${outsideFile}`);
      assert.equal(decoy.status, 200, 'the in-root decoy must remain accessible');
      assert.ok((await decoy.text()).includes(decoyMarker));

      const afterImport = await fetch(outsideUrl);
      const body = await afterImport.text();
      assert.equal(
        afterImport.status,
        403,
        'import analysis must not authorize the colliding outside absolute path'
      );
      assert.ok(!body.includes(outsideMarker), 'outside file content must not leak');
    } finally {
      try {
        await server?.close();
      } finally {
        fs.rmSync(fixtureDir, { recursive: true, force: true });
      }
    }
  }
);
