import { spawn, execFile, type ChildProcess } from 'node:child_process';
import fs from 'node:fs';
import { setTimeout as delay } from 'node:timers/promises';
import { zaneiSubjectPaths, type ZaneiManifest } from './zaneiConfig';
import type { RecorderBinding } from './sourceControl';

export type ZaneiStatus = {
  instance: string | null;
  state: string;
  running: boolean;
  paused: boolean | null;
  permissions_ok: boolean;
  heartbeat_freshness: string | null;
  store_write_state: string | null;
  last_event_ts: string | null;
  degraded: { [component: string]: string };
};
export type CapturePermission =
  | 'read_accessibility_tree'
  | 'observe_input'
  | 'automate_browser'
  | 'automate_safari'
  | 'capture_screen';
export class ZaneiPermissionRequired extends Error {
  constructor(readonly missing: CapturePermission[]) {
    super(
      'Grant Pantaray Accessibility, Input Monitoring, Screen Recording and required browser Automation permissions, then start recording again.'
    );
  }
}
const START_TIMEOUT_MS = 15_000;
const COMMAND_TIMEOUT_MS = 10_000;

export function createZaneiProcess(params: {
  manifest: ZaneiManifest;
  userId: string;
  onExit: () => void;
  requestPermissions: (missing: CapturePermission[]) => Promise<void>;
  /**
   * Screen Recording, which recording requires although the recorder treats it as
   * optional. The recorder reports it only once it runs, too late to keep a recorder
   * without it from starting, so this process reads it instead.
   */
  holdsScreenRecording: () => boolean;
  spawnProcess?: typeof spawn;
  runCommand?: typeof execFile;
}) {
  const paths = zaneiSubjectPaths(params.manifest, params.userId);
  const env: NodeJS.ProcessEnv = {
    ...process.env,
    ZANEI_KEYCHAIN_SERVICE: paths.service,
    ZANEI_KEYCHAIN_LABEL: paths.label,
    ZANEI_KEYCHAIN_NO_PROMPT: '1',
  };
  delete env.ZANEI_STORE_KEY_FILE;
  const prefix = ['--config', paths.config, '--store', paths.store];
  let child: ChildProcess | null = null;
  let exited: Promise<void> = Promise.resolve();
  let expectedExit = false;
  let startupConfirmed = false;
  let processError: Error | null = null;

  function command(args: string[], input?: string): Promise<string> {
    return new Promise((resolve, reject) => {
      const process = (params.runCommand ?? execFile)(
        params.manifest.executable_path,
        [...prefix, ...args],
        { env, timeout: COMMAND_TIMEOUT_MS, maxBuffer: 512 * 1024, encoding: 'utf8' },
        (error, stdout) => {
          // Status reports include failure state even when the command exits nonzero.
          const expectedExit =
            (args.includes('status') && [1, 4].includes(Number(error?.code))) ||
            (args.includes('doctor') && error?.code === 3);
          if (error && !(expectedExit && stdout.trim().startsWith('{'))) {
            reject(new Error('Zanei command failed.'));
          } else resolve(stdout);
        }
      );
      process.stdin?.on('error', () => {
        reject(new Error('Zanei command input failed.'));
        process.kill();
      });
      process.stdin?.end(input);
    });
  }
  /** `permissions_ok` also covers Screen Recording, which the recorder leaves out. */
  async function status(): Promise<ZaneiStatus> {
    const report = JSON.parse(await command(['status', '--json'])) as ZaneiStatus;
    if (typeof report.running !== 'boolean' || typeof report.permissions_ok !== 'boolean') {
      throw new Error('Invalid Zanei status response.');
    }
    return { ...report, permissions_ok: report.permissions_ok && params.holdsScreenRecording() };
  }
  /**
   * Recording turned off: capture stops while the recorder keeps running, so what
   * it already stored stays readable. The recorder polls the request every second.
   */
  async function pause(): Promise<void> {
    await command(['pause']);
  }
  async function resume(): Promise<void> {
    await command(['resume']);
  }
  async function stop(): Promise<void> {
    const current = child;
    if (!current) return;
    expectedExit = true;
    current.stdin?.end();
    const timeout = setTimeout(() => current.kill('SIGTERM'), COMMAND_TIMEOUT_MS);
    const hardTimeout = setTimeout(() => current.kill('SIGKILL'), COMMAND_TIMEOUT_MS * 2);
    try {
      await exited;
    } finally {
      clearTimeout(timeout);
      clearTimeout(hardTimeout);
    }
  }
  /**
   * `paused` starts a recorder that must not capture: the app is restoring one
   * for a user who has recording turned off. Only the pause the store already
   * carries can promise that. `zanei` 0.6.0 has no paused start, `pause` needs a
   * running daemon, and the daemon captures from the moment it runs, so a pause
   * sent after the spawn always arrives too late. The daemon does read the store's
   * pause before it starts a collector, so the caller may ask for a paused start
   * only for a store its own earlier session left paused, which the preference
   * records. A store that carries no pause is not paused after the fact: the
   * recorder is taken down instead, because nothing can unrecord what it captured.
   *
   * A pause outlives the process in the store, and the app owns the preference, so
   * any start that is meant to capture clears a stale one.
   */
  async function start(
    config: string,
    manual = false,
    paused = false
  ): Promise<{ binding: RecorderBinding; permissionsReady: boolean }> {
    fs.mkdirSync(paths.directory, { recursive: true, mode: 0o700 });
    fs.writeFileSync(`${paths.config}.tmp`, config, { mode: 0o600 });
    fs.renameSync(`${paths.config}.tmp`, paths.config);
    const doctor = JSON.parse(await command(['doctor', '--json'])) as {
      capabilities: Partial<Record<CapturePermission, { required: boolean; state: string }>>;
    };
    const missing: CapturePermission[] = (
      ['read_accessibility_tree', 'observe_input', 'automate_browser', 'automate_safari'] as const
    ).filter(
      (key) =>
        doctor.capabilities[key]?.required &&
        doctor.capabilities[key]?.state !== 'available' &&
        !(
          (key === 'automate_browser' || key === 'automate_safari') &&
          doctor.capabilities[key]?.state === 'deferred'
        )
    );
    if (!params.holdsScreenRecording()) missing.push('capture_screen');
    if (missing.length) {
      if (!manual) throw new ZaneiPermissionRequired([...missing]);
      await params.requestPermissions([...missing]);
    }
    expectedExit = false;
    processError = null;
    const current = (params.spawnProcess ?? spawn)(
      params.manifest.executable_path,
      [...prefix, 'start', '--foreground', '--exit-on-stdin-eof'],
      { env, stdio: ['pipe', 'ignore', 'ignore'] }
    );
    child = current;
    current.stdin?.on('error', (error) => {
      processError = error;
      current.kill('SIGTERM');
    });
    exited = new Promise<void>((resolve) => {
      current.once('error', (error) => {
        processError = error;
      });
      current.once('close', () => {
        child = null;
        resolve();
        if (!expectedExit && startupConfirmed) params.onExit();
      });
    });
    try {
      const deadline = Date.now() + START_TIMEOUT_MS;
      // Bounded startup observation; no background restart or capture polling loop.
      while (Date.now() < deadline) {
        if (!child) throw processError ?? new Error('Zanei exited during startup.');
        const report = await status();
        if (report.state === 'store_locked') throw new Error('Zanei Keychain key unavailable.');
        const owned = report.running && report.instance?.startsWith(`${current.pid}@`);
        // The daemon captures from the moment it runs, so a store that lost its
        // pause fails here rather than after the heartbeat and store health that
        // reads wait for. The failure stops the recorder.
        if (owned && paused && !report.paused) {
          throw new Error('Zanei started capturing without the pause its store held.');
        }
        if (
          owned &&
          report.heartbeat_freshness === 'fresh' &&
          report.store_write_state === 'healthy'
        ) {
          if (!paused && report.paused) await resume();
          const page = JSON.parse(
            await command(
              ['context-read'],
              JSON.stringify({
                kind: 'page',
                protocol_version: 1,
                cursor: null,
                upper_bound: null,
                limit: 1,
              }) + '\n'
            )
          ) as { kind: string; protocol_version: number; store_identity: string };
          if (
            !['page', 'gap'].includes(page.kind) ||
            page.protocol_version !== 1 ||
            !page.store_identity
          ) {
            throw new Error('Zanei context protocol is incompatible.');
          }
          if (!child) throw new Error('Zanei exited during startup.');
          startupConfirmed = true;
          return {
            binding: { store_id: page.store_identity, protocol_version: 1 },
            permissionsReady: report.permissions_ok,
          };
        }
        await delay(150);
      }
      throw new Error('Zanei startup timed out.');
    } catch (error) {
      await stop();
      throw error;
    }
  }
  return { start, stop, status, pause, resume };
}
