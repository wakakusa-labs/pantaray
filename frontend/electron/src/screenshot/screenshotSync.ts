import fs from 'node:fs';
import { randomUUID } from 'node:crypto';
import type { BrowserWindow } from 'electron';
import type { LocalOwner } from '../auth/localRuntimeState';
import type { CapturePrivacyManager } from '../privacy/capturePrivacy';
import { resolveScopedSettingsPath, SCOPED_PREFERENCE_FILES } from '../settings/scope';
import type { CaptureStatusSnapshot } from './captureStatus';
import type { CaptureEditingRequest } from './captureEditing';
import {
  createZaneiProcess,
  ZaneiPermissionRequired,
  type CapturePermission,
  type ZaneiStatus,
} from '../context/zaneiProcess';
import { readZaneiManifest, zaneiConfig, policyRevision } from '../context/zaneiConfig';
import type {
  SourceState,
  SourceTransition,
  SourceTransitionResult,
  StopReason,
} from '../context/sourceControl';
export type ScreenshotSyncManager = ReturnType<typeof createScreenshotSyncManager>;

/**
 * Outcome of a start request.
 *
 * `permission_pending` is not a grant: the recorder is up but macOS has not granted
 * the capture permissions yet, so nothing has been recorded and the first-run screen
 * must stay open until a later start actually reaches `started`.
 */
export type RecordingStartResult = 'started' | 'permission_pending' | 'failed';

export function createScreenshotSyncManager(params: {
  isMac: boolean;
  userDataDir: string;
  getMainWindow: () => BrowserWindow | null;
  isBackendRuntimeReady: () => boolean;
  capturePrivacy: CapturePrivacyManager;
  getManifestPath: () => string;
  requestPermissions: (missing: CapturePermission[]) => Promise<void>;
  readSource: (userId: string) => Promise<SourceState>;
  transitionSource: (
    userId: string,
    transition: SourceTransition
  ) => Promise<SourceTransitionResult>;
  containSource: (userId: string) => Promise<void>;
  onCaptureStatusChanged?: () => void;
  /** Capture started for this owner; called once per owner in a run, after the start succeeded. */
  onRecordingStarted?: (userId: string) => void;
  createProcess?: typeof createZaneiProcess;
}) {
  let owner: LocalOwner | null = null;
  let collector: ReturnType<typeof createZaneiProcess> | null = null;
  let active = false;
  let editingSessionId: string | null = null;
  let paused = false;
  let capturePaused = false;
  let failure: string | null = null;
  let permissionRequired = false;
  let stopPreferenceWriteFailed = false;
  let activation: {
    userId: string;
    /** Carries `capture_paused` when this restores a recorder the user turned off. */
    request: Extract<SourceTransition, { kind: 'activate' }>;
  } | null = null;
  let pending = Promise.resolve();
  /** Owners `onRecordingStarted` already heard about in this run. */
  const reportedStarts = new Set<string>();
  const reportRecordingStarted = (userId: string) => {
    if (reportedStarts.has(userId)) return;
    reportedStarts.add(userId);
    try {
      params.onRecordingStarted?.(userId);
    } catch (error) {
      // The hook is a courtesy; the start the user asked for already succeeded.
      console.error('Recording start hook failed:', error);
    }
  };
  const settingsPath = () =>
    resolveScopedSettingsPath({
      userDataDir: params.userDataDir,
      userId: owner?.kind === 'account' ? owner.id : null,
      fileName: SCOPED_PREFERENCE_FILES.recording,
    });
  /**
   * The owner as `kind:id`, the identity the renderer's owner boundary keys on. A session
   * that expires keeps the id and changes only the kind, and that is another owner with
   * another settings scope. `setOwner` does not go through the queue below, so it can land
   * in the middle of an await: an operation reads this once before its first await and
   * stops if it no longer matches, rather than re-reading the owner around every await.
   */
  const ownerKey = () => (owner ? `${owner.kind}:${owner.id}` : null);
  /**
   * What the previous run left behind: the user's choice, and whether the recorder
   * it left behind holds the capture pause. `recorder_paused` is absent in a
   * preference written by the release where turning recording off stopped the
   * recorder instead of pausing it.
   *
   * The preference file exists only once recording actually ran for this user: a
   * start still waiting for macOS permissions, and a stop before anything ever ran,
   * leave none, so turning recording off writes nothing for a user who never had it on.
   */
  type StoredPreference = { enabled: boolean; recorder_paused?: boolean };
  const storedPreference = (): StoredPreference | null =>
    owner && fs.existsSync(settingsPath())
      ? (JSON.parse(fs.readFileSync(settingsPath(), 'utf8')) as StoredPreference)
      : null;
  const enabledPreference = () => storedPreference()?.enabled === true;
  /**
   * Recording is on only while a recorder is capturing, not merely running: a
   * recorder brought up for a user who has recording turned off is running and
   * already paused, waiting for the activation that publishes that pause.
   */
  const recordingOn = () =>
    (collector !== null && !capturePaused && !activation?.request.capture_paused) ||
    stopPreferenceWriteFailed;
  const notify = () => {
    const win = params.getMainWindow();
    if (win && !win.isDestroyed()) win.webContents.send('screenshot:statusChanged', recordingOn());
    params.onCaptureStatusChanged?.();
  };
  function enqueue<T>(operation: () => Promise<T>): Promise<T> {
    const result = pending.then(operation);
    pending = result.then(
      () => undefined,
      (error) => {
        failure = error instanceof Error ? error.message : String(error);
        permissionRequired = error instanceof ZaneiPermissionRequired;
        // The tray only shows generic recording copy, so the cause must reach the log.
        console.error('Activity recording failed:', error);
        notify();
      }
    );
    return result;
  }
  async function suspend(reason: StopReason): Promise<string | null> {
    // The source and the preference this suspend is about belong to the owner it started
    // for, read before the recorder stop and the backend requests below can hand the
    // manager to another one.
    const suspended = owner;
    // The user's stored "off" outranks whatever triggered this suspend. The backend keeps
    // the reason across launches and reads only `disabled` as "the user turned recording
    // off"; a shutdown or sign-out written while recording is off would make the next
    // launch read it as never turned off, and run the queued work the toggle suppresses.
    const storedReason = storedPreference()?.enabled === false ? 'disabled' : reason;
    active = false;
    capturePaused = false;
    permissionRequired = false;
    activation = null;
    // Capture stops before the backend hears about it. Every reason to suspend is
    // a reason to record nothing more right now, and the transition below waits on
    // a read and a write that can each take ten seconds.
    await collector?.stop();
    collector = null;
    notify();
    let epoch: string | null = null;
    try {
      if (suspended) {
        const state = await params.readSource(suspended.id);
        const result = await params.transitionSource(suspended.id, {
          kind: 'suspend',
          request_id: randomUUID(),
          expected_epoch: state.kind === 'ready' ? state.binding.epoch : state.epoch,
          policy_revision: policyRevision(zaneiConfig(params.capturePrivacy.getCaptureSettings())),
          reason: storedReason,
        });
        if (result.kind !== 'applied' || result.state.kind === 'ready') {
          throw new Error(`Context source suspend ${result.kind}.`);
        }
        epoch = result.state.epoch;
      }
    } catch (error) {
      if (suspended) await params.containSource(suspended.id);
      if (reason !== 'signed_out' && reason !== 'shutdown') throw error;
    }
    failure = null;
    return epoch;
  }
  /**
   * The epoch of a source that still holds its read permit, or null.
   *
   * The backend reports a source whose permit it no longer holds as stopped, which
   * is what a local backend restarted under a still-running recorder leaves behind.
   * Pausing capture keeps the generation and so cannot restore that permit: the
   * caller has to fall back to the full stop or the full activation instead.
   */
  async function readyEpoch(ownerId: string): Promise<string | null> {
    const state = await params.readSource(ownerId);
    return state.kind === 'ready' ? state.binding.epoch : null;
  }
  /**
   * Publishes the capture state the user chose. The recorder keeps its generation
   * and its read permit, so a paused source still answers timeline and event reads.
   */
  async function publishCapturePaused(
    ownerId: string,
    epoch: string,
    nowPaused: boolean
  ): Promise<void> {
    const result = await params.transitionSource(ownerId, {
      kind: 'set_capture_paused',
      request_id: randomUUID(),
      expected_epoch: epoch,
      paused: nowPaused,
    });
    if (result.kind !== 'applied' || result.state.kind !== 'ready') {
      throw new Error(`Context source capture pause ${result.kind}.`);
    }
  }
  /**
   * Turning recording off pauses the recorder instead of stopping it, so what it
   * already recorded stays readable while nothing new is captured. Recording must
   * never outlive the user's "off", so anything that fails falls back to the stop.
   *
   * The recorder is paused before the backend is asked anything: the epoch read
   * and the pause publication each wait on a request that can take ten seconds,
   * and capture would keep running for all of it. Publishing afterwards can only
   * leave a source that still says capture is on, and that falls back to the stop,
   * which ends the source generation the readers key off.
   */
  async function applyCapturePause(): Promise<void> {
    const pausedFor = owner;
    if (!collector || !active || !pausedFor) {
      await suspend('disabled');
      return;
    }
    try {
      await collector.pause();
    } catch (error) {
      await suspend('disabled');
      throw error;
    }
    capturePaused = true;
    notify();
    try {
      const epoch = await readyEpoch(pausedFor.id);
      if (epoch === null) {
        await suspend('disabled');
        return;
      }
      await publishCapturePaused(pausedFor.id, epoch, true);
    } catch (error) {
      await suspend('disabled');
      throw error;
    }
  }
  async function resume(manual = false, restorePaused = false): Promise<RecordingStartResult> {
    if (active) return 'started';
    // The owner this start belongs to. The recorder it brings up writes into that owner's
    // store and holds that owner's source permit, so a start that outlives its owner would
    // keep recording under whoever arrived instead.
    const startedFor = owner;
    if (
      paused ||
      !params.isMac ||
      !startedFor ||
      !params.isBackendRuntimeReady() ||
      editingSessionId !== null
    )
      return 'failed';
    const startedForKey = ownerKey();
    const config = zaneiConfig(params.capturePrivacy.getCaptureSettings());
    const epoch = await suspend('policy_change');
    if (!epoch || ownerKey() !== startedForKey) return 'failed';
    collector = (params.createProcess ?? createZaneiProcess)({
      manifest: readZaneiManifest(params.getManifestPath()),
      userId: startedFor.id,
      requestPermissions: params.requestPermissions,
      onExit: () => {
        void enqueue(async () => {
          await suspend('shutdown');
          throw new Error('Zanei recorder stopped unexpectedly.');
        }).catch(() => undefined);
      },
    });
    try {
      const result = await collector.start(config, manual, restorePaused);
      if (ownerKey() !== startedForKey) {
        // The owner left while macOS was asking for the capture permissions. Nothing was
        // published for it, so taking the recorder back down is the whole rollback.
        await collector.stop();
        collector = null;
        notify();
        return 'failed';
      }
      activation = {
        userId: startedFor.id,
        request: {
          kind: 'activate',
          request_id: randomUUID(),
          issued_epoch: epoch,
          recorder_binding: result.binding,
          capture_paused: restorePaused,
        },
      };
      if (result.permissionsReady) {
        await activate();
        return 'started';
      }
      permissionRequired = true;
      failure = null;
      notify();
      return 'permission_pending';
    } catch (error) {
      if (collector) await suspend('shutdown');
      throw error;
    }
  }
  /**
   * Publishes the readiness of the recorder `resume` brought up, including the
   * pause a recorder restored for a user who has recording off already holds.
   *
   * The pause travels in the activation itself rather than in a `set_capture_paused`
   * that follows it: the local workers run concurrently, and a source published as
   * ready-and-capturing first would let a queued Suggestion job read recording as
   * on while the user's saved preference says off.
   */
  async function activate(): Promise<void> {
    if (!activation) return;
    const restorePaused = activation.request.capture_paused;
    const activatedFor = activation.userId;
    try {
      const result = await params.transitionSource(activation.userId, activation.request);
      if (result.kind !== 'applied' || result.state.kind !== 'ready') {
        throw new Error(`Context source activation ${result.kind}.`);
      }
      // Recording is running now, which is the moment the user's "on" becomes durable —
      // whether this is a manual start or the permission a pending start waited for.
      // A recorder restarted only to keep reading holds no such "on".
      if (!restorePaused) markEnabled();
      activation = null;
      active = true;
      // `resume` starts such a recorder paused and refuses to run one that is not,
      // so this states what the daemon already does; nothing has to pause it.
      capturePaused = restorePaused;
      permissionRequired = false;
      failure = null;
      notify();
    } catch (error) {
      await suspend('shutdown');
      throw error;
    }
    if (!restorePaused) reportRecordingStarted(activatedFor);
  }
  /**
   * Starts the recorder this user's stored preference calls for.
   *
   * Invariant: while the app runs and this user's preference exists, the recorder
   * process is up and holds the source permit; the toggle only switches capture
   * pause. So a preference that is off still brings the recorder back, paused at
   * once, and the activity the store still holds stays readable instead of
   * reading as "recording unavailable". A user who never turned recording on has
   * no preference and gets no recorder.
   */
  async function restoreFromPreference(): Promise<void> {
    const stored = storedPreference();
    if (!stored) return;
    // Except for a recorder whose store carries no pause: nothing can pause a
    // recorder before it runs, so that one would record until a pause reached it.
    // A preference from the release that stopped the recorder instead of pausing
    // it is exactly that, so it stays down for this run, as it did in that
    // release, until recording is turned on and off again leaves a pause behind.
    if (!stored.enabled && !stored.recorder_paused) return;
    await resume(false, !stored.enabled);
  }
  /**
   * Records what this run leaves behind. A later restore reads `recorder_paused`
   * to decide whether the recorder may come back for a user who has recording
   * off, so it states what the recorder actually did rather than what the
   * preference implies: a stop that could not pause leaves no pause behind.
   */
  function writeEnabled(enabled: boolean): void {
    try {
      fs.writeFileSync(
        settingsPath(),
        JSON.stringify({ enabled, recorder_paused: capturePaused }, null, 2),
        { mode: 0o600 }
      );
      stopPreferenceWriteFailed = false;
    } catch (error) {
      stopPreferenceWriteFailed = !enabled;
      throw error;
    }
  }
  /**
   * Records that recording is on now. A preference that already says so needs no
   * write, but it does clear a failed "off": nothing is left unsaved once the
   * user has recording on again.
   */
  function markEnabled(): void {
    stopPreferenceWriteFailed = false;
    if (!enabledPreference()) writeEnabled(true);
  }
  /** The latest report, or null when no recorder is up or it cannot answer. */
  async function readReport(): Promise<ZaneiStatus | null> {
    if (!collector) return null;
    try {
      const report = await collector.status();
      failure = null;
      return report;
    } catch (error) {
      failure = error instanceof Error ? error.message : String(error);
      console.error('Activity recording status unavailable:', error);
      return null;
    }
  }
  /**
   * The recorder is up and able to capture. The capture pause is deliberately not
   * part of it: this asks whether capture would work, not whether it runs now.
   */
  const captureReady = (report: ZaneiStatus | null) =>
    report !== null &&
    report.running &&
    report.permissions_ok &&
    report.heartbeat_freshness === 'fresh' &&
    report.store_write_state === 'healthy';
  return {
    setOwner: (next: LocalOwner) => {
      owner = next;
      editingSessionId = null;
      failure = null;
      permissionRequired = false;
      stopPreferenceWriteFailed = false;
    },
    getCaptureEditing: () => editingSessionId !== null,
    setCaptureEditing: (request: CaptureEditingRequest): Promise<boolean> =>
      enqueue(async () => {
        if (request.kind === 'begin') {
          if (owner?.id !== request.ownerId || !params.isBackendRuntimeReady()) return false;
          await suspend('policy_change');
          if (owner?.id !== request.ownerId || !params.isBackendRuntimeReady()) return false;
          editingSessionId = request.sessionId;
        } else {
          if (editingSessionId !== request.sessionId) return false;
          editingSessionId = null;
          try {
            // Ending an edit needs no backend call while the owner is unavailable;
            // normal owner restoration will restart capture from its preference.
            await restoreFromPreference();
          } catch (error) {
            editingSessionId = request.sessionId;
            throw error;
          }
        }
        notify();
        return true;
      }),
    getStatus: recordingOn,
    start: (): Promise<RecordingStartResult> =>
      enqueue(async () => {
        const startedFor = owner;
        if (!capturePaused || !collector || !startedFor) return resume(true);
        const startedForKey = ownerKey();
        // Only a recorder that can capture may be resumed in place: macOS can have
        // revoked the permissions while capture was paused, and reporting `started`
        // for a recorder that records nothing would leave the toggle on until the
        // next status poll. Only a full activation can give the source a permit
        // again or report the permission the user still has to grant, and without
        // one the recorder would run on while nothing could read it.
        const epoch = captureReady(await readReport()) ? await readyEpoch(startedFor.id) : null;
        // The owner left while the recorder and the backend answered. Lifting the pause
        // now would let the recorder the previous owner holds capture whoever arrived,
        // into that owner's store, and the "on" would be stored in the arriving owner's
        // scope. The recorder stays paused, exactly as this start found it.
        if (ownerKey() !== startedForKey) return 'failed';
        if (epoch === null) {
          await suspend('disabled');
          return resume(true);
        }
        // The recorder and its permit are still current: only capture was paused.
        // Publishing first keeps a failure on the user's "off" instead of half on.
        await publishCapturePaused(startedFor.id, epoch, false);
        try {
          await collector.resume();
        } catch (error) {
          // The published state now says capture is on while the recorder is still
          // paused, and short Insight reads that state: the tail recorded before
          // the pause would become a Suggestion for a start that never happened.
          await applyCapturePause();
          throw error;
        }
        capturePaused = false;
        markEnabled();
        notify();
        reportRecordingStarted(startedFor.id);
        return 'started';
      }),
    stop: () =>
      enqueue(async () => {
        let pauseFailure: unknown = null;
        try {
          await applyCapturePause();
        } catch (error) {
          // The pause fell back to the stop, so no recorder is left capturing and
          // the user's "off" holds for this run. Recording it is what keeps the
          // next launch from restoring a capturing recorder from a preference
          // that still reads as on; the failure still reaches the caller.
          pauseFailure = error;
        }
        if (storedPreference()) writeEnabled(false);
        if (pauseFailure) throw pauseFailure;
        return true;
      }),
    pause: (reason: StopReason) => {
      paused = true;
      return enqueue(async () => {
        if (collector || active) await suspend(reason);
      });
    },
    /** Brings the recorder back once this user's runtime is ready to hold it. */
    restoreRecorder: () =>
      enqueue(async () => {
        paused = false;
        if (failure) return;
        await restoreFromPreference();
      }),
    updatePrivacy: <T>(update: () => T) =>
      enqueue(async () => {
        await suspend('policy_change');
        const result = update();
        // A filter change restarts the recorder under the same rule. Editing
        // filters keeps it down until the edit ends, which is the next call here.
        await restoreFromPreference();
        return result;
      }),
    getCaptureStatusSnapshot: (): Promise<CaptureStatusSnapshot> =>
      enqueue(async () => {
        const report = await readReport();
        const running = captureReady(report);
        // A recorder restored for a user who has recording turned off is paused on
        // purpose, and that pause must not keep it from activating once macOS
        // grants the permission: activation is what its reads need.
        if (activation && running && (activation.request.capture_paused || !report?.paused))
          await activate();
        const healthy = running && !report?.paused;
        const kind = permissionRequired
          ? 'permission_required'
          : // An "off" that could not be saved must not read as a plain pause: the
            // toggle still shows on, because the next launch resumes from the
            // preference that stayed on, and only this says the save failed.
            failure || stopPreferenceWriteFailed
            ? 'unavailable'
            : editingSessionId !== null
              ? 'editing_paused'
              : capturePaused || !active
                ? 'paused'
                : report && !report.permissions_ok
                  ? 'permission_required'
                  : healthy
                    ? Object.keys(report?.degraded ?? {}).length
                      ? 'degraded'
                      : 'capturing'
                    : 'unavailable';
        return {
          kind,
          screenshotsEnabled: recordingOn(),
          activeWindow: { appName: null, title: null },
          browserUrl: null,
          lastCaptureAt: report?.last_event_ts ?? null,
          lastCaptureResult: null,
          reasonLabel:
            (stopPreferenceWriteFailed ? 'capture_preference_write_failed' : failure) ??
            (kind === 'degraded'
              ? Object.keys(report?.degraded ?? {}).join(', ')
              : `zanei_${kind}`),
        };
      }),
  };
}
