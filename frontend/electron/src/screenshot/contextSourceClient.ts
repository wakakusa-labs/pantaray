import { randomUUID } from 'node:crypto';
import type {
  SourceState,
  SourceTransition,
  SourceTransitionResult,
  StopReason,
} from '../context/sourceControl';

/** The backend's context source for an owner: the generation and read permit of its recorder. */
export function createContextSourceClient(params: {
  readSource: (userId: string) => Promise<SourceState>;
  transitionSource: (
    userId: string,
    transition: SourceTransition
  ) => Promise<SourceTransitionResult>;
}) {
  return {
    /** Ends the current generation; resolves to the epoch a next activation is issued for. */
    async suspend(ownerId: string, reason: StopReason, policyRevision: string): Promise<string> {
      const state = await params.readSource(ownerId);
      const result = await params.transitionSource(ownerId, {
        kind: 'suspend',
        request_id: randomUUID(),
        expected_epoch: state.kind === 'ready' ? state.binding.epoch : state.epoch,
        policy_revision: policyRevision,
        reason,
      });
      if (result.kind !== 'applied' || result.state.kind === 'ready') {
        throw new Error(`Context source suspend ${result.kind}.`);
      }
      return result.state.epoch;
    },
    /**
     * The epoch of a source that still holds its read permit, or null.
     *
     * The backend reports a source whose permit it no longer holds as stopped, which
     * is what a local backend restarted under a still-running recorder leaves behind.
     * Pausing capture keeps the generation and so cannot restore that permit: the
     * caller has to fall back to the full stop or the full activation instead.
     */
    async readyEpoch(ownerId: string): Promise<string | null> {
      const state = await params.readSource(ownerId);
      return state.kind === 'ready' ? state.binding.epoch : null;
    },
    /**
     * Publishes the capture state the user chose. The recorder keeps its generation
     * and its read permit, so a paused source still answers timeline and event reads.
     */
    async publishCapturePaused(ownerId: string, epoch: string, nowPaused: boolean): Promise<void> {
      const result = await params.transitionSource(ownerId, {
        kind: 'set_capture_paused',
        request_id: randomUUID(),
        expected_epoch: epoch,
        paused: nowPaused,
      });
      if (result.kind !== 'applied' || result.state.kind !== 'ready') {
        throw new Error(`Context source capture pause ${result.kind}.`);
      }
    },
  };
}
