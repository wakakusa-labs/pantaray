import type { MainContext } from '../context';
import type { IpcRegistrar } from '../registrar';
import { parseInput } from '../schemas/error';
import { OnboardingStateSchema } from '../schemas/onboarding';

export function registerOnboardingHandlers(ctx: MainContext, registrar: IpcRegistrar): void {
  registrar.handle('onboarding:getState', () => ctx.onboarding.getState());
  registrar.handle('onboarding:setState', (_event, state) =>
    ctx.onboarding.setState(parseInput(OnboardingStateSchema, 'onboarding:setState', state))
  );
}
