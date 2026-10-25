import { z } from 'zod';

import type { CapturePermission } from '../../context/zaneiProcess';

export const ONBOARDING_STEPS = ['ai', 'recording', 'web_search', 'apps', 'done'] as const;

/** Where first-run setup stands on this device; `completed` shows the app as usual. */
export const OnboardingStateSchema = z.discriminatedUnion('status', [
  z.object({ status: z.literal('in_progress'), step: z.enum(ONBOARDING_STEPS) }).strict(),
  z.object({ status: z.literal('completed') }).strict(),
]);

export type OnboardingState = z.infer<typeof OnboardingStateSchema>;

/** A macOS permission a recording start can wait on, which the renderer can ask for. */
export const CapturePermissionSchema = z.enum([
  'read_accessibility_tree',
  'observe_input',
  'capture_screen',
  'automate_browser',
  'automate_safari',
] as const satisfies readonly CapturePermission[]);
