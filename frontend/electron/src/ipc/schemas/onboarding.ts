import { z } from 'zod';

export const ONBOARDING_STEPS = ['ai', 'recording', 'web_search', 'apps', 'done'] as const;

/** Where first-run setup stands on this device; `completed` shows the app as usual. */
export const OnboardingStateSchema = z.discriminatedUnion('status', [
  z.object({ status: z.literal('in_progress'), step: z.enum(ONBOARDING_STEPS) }).strict(),
  z.object({ status: z.literal('completed') }).strict(),
]);

export type OnboardingState = z.infer<typeof OnboardingStateSchema>;

/** The two macOS permissions the capture gate requires, each granted in its own pane. */
export const CapturePermissionPaneSchema = z.enum(['accessibility', 'screen_recording']);

export type CapturePermissionPane = z.infer<typeof CapturePermissionPaneSchema>;
