import type {
  ConnectionRuntimeResult,
  ConnectionSettings,
} from '../../../electron/src/ipc/schemas/aiConnection';
import { CHATGPT_MODEL_CANDIDATES } from '../../../electron/src/aiConnection/preferences';

export { CHATGPT_MODEL_CANDIDATES };

/** 設定画面「AI 接続」が扱う状態。値は Electron main が持つ接続設定の投影。 */

/** 推論の接続方法。`cloud` は Pantaray アカウントでのログインが前提。 */
export type AiConnectionMethod = 'cloud' | 'chatgpt' | 'api_key';

export type ApiKeyProvider = ConnectionSettings['preferences']['provider'];

export const API_KEY_PROVIDERS: readonly ApiKeyProvider[] = ['openai', 'anthropic', 'fireworks'];

/**
 * 宛先ごとのモデル。`candidates` の先頭がその宛先の既定。`allowsOther` の宛先では
 * 候補に無い名前も入力できる。候補の無い宛先は入力だけになる。
 */
export const PROVIDER_MODELS: Record<
  ApiKeyProvider,
  { candidates: readonly string[]; allowsOther: boolean }
> = {
  openai: {
    candidates: ['gpt-5.6', 'gpt-5.6-sol', 'gpt-5.6-terra', 'gpt-6-luna'],
    allowsOther: true,
  },
  anthropic: {
    candidates: ['claude-opus-5-5', 'claude-sonnet-5-5', 'claude-haiku-5-5', 'claude-fable-5-1'],
    allowsOther: false,
  },
  fireworks: { candidates: [], allowsOther: true },
};

export function modelCandidates(
  method: AiConnectionMethod,
  provider: ApiKeyProvider
): readonly string[] {
  return method === 'chatgpt' ? CHATGPT_MODEL_CANDIDATES : PROVIDER_MODELS[provider].candidates;
}

/**
 * 切り替え先で使うモデル。モデルは方式とプロバイダーで共有しているので、
 * 候補を持つ宛先へ持ち越せない値（空を含む）は、その宛先の既定に置き換える。
 * 候補を持たない宛先では、利用者が入力した名前をそのまま残す。
 */
export function modelForTarget(params: {
  method: AiConnectionMethod;
  provider: ApiKeyProvider;
  model: string;
}): string {
  const candidates = modelCandidates(params.method, params.provider);
  if (candidates.length === 0 || candidates.includes(params.model)) return params.model;
  return candidates[0];
}

/**
 * ChatGPT 接続の状態。更新に失敗したときは再認証が要るだけで、
 * 保存済みの API キーへは切り替わらない（設計 6.6）。null は未接続。
 */
export type ChatgptConnection = { status: 'connected' | 'reauthentication_required' };

export type ApiKeyConnection = {
  provider: ApiKeyProvider;
  /** 保存済みのキーは値を持たない。保存されているかどうかだけを見せる。 */
  hasSavedKey: boolean;
};

export type AiConnectionState = {
  runtime: ConnectionRuntimeResult;
  method: AiConnectionMethod;
  /** 選んでいる方式のモデル。方式ごとに別々には持たない。 */
  model: string;
  chatgpt: ChatgptConnection | null;
  apiKey: ApiKeyConnection;
  webSearchHasSavedKey: boolean;
  /** safeStorage が使えないと資格情報を保存できない。 */
  canStoreSecrets: boolean;
};
