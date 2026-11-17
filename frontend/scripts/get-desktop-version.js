/**
 * Desktop アプリのビルド用バージョン（semver）を決定する。
 *
 * 目的:
 * - **配布ビルド**（GitHub Releases / タグ）では `vX.Y.Z`（beta チャネルは `vX.Y.Z-beta.N`）を
 *   SSOT として、`v` を除いた文字列を使う。
 * - **非配布ビルド**（ローカルなど）では、最新の配布タグの `X.Y.Z` を基準に
 *   `X.Y.Z-dev.<距離>+<short_sha>` を自動生成して衝突を避ける。`-dev` ビルドは更新 feed を持たない。
 *   - `v*` タグが存在しない場合は `0.0.0-dev.<距離>+<short_sha>` を使う。
 *
 * 入力:
 * - `PANTARAY_DESKTOP_VERSION`（任意）:
 *   - 設定されていれば、その値を **そのまま**採用する（semver形式は検証する）。
 *   - 例: `1.8.2`
 *
 * 出力:
 * - stdout に semver 文字列を出力する。
 *
 * 失敗時:
 * - git リポジトリ外、git 実行失敗、期待しないタグ形式などは **明示的にエラー**で終了する。
 */

const { execFileSync } = require('child_process');
const path = require('path');

const REPO_ROOT = path.resolve(__dirname, '..', '..');

function git(args) {
  return execFileSync('git', args, {
    cwd: REPO_ROOT,
    encoding: 'utf8',
    // 失敗時は例外にstderrが入るが、通常の成功時はstderr不要
    stdio: ['ignore', 'pipe', 'pipe'],
  }).trim();
}

function tryGit(args) {
  try {
    return git(args);
  } catch {
    return null;
  }
}

function requireGitWorkTree() {
  const inside = tryGit(['rev-parse', '--is-inside-work-tree']);
  if (inside !== 'true') {
    throw new Error('This build requires a git repository (inside work tree).');
  }
}

function validateSemver(value) {
  const v = String(value || '').trim();
  // ここでは厳密semverの完全実装は不要だが、electron-builder引数注入などの事故を防ぐため
  // 「空白なし」「危険文字なし」を強く制約する。
  const ok = /^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$/.test(v);
  if (!ok) throw new Error(`Invalid semver: ${v}`);
  return v;
}

// release-desktop.yml が受け付けるタグと同じ形式。
const RELEASE_TAG = /^v(\d+\.\d+\.\d+)(-beta\.\d+)?$/;

function parseReleaseTag(tag) {
  const raw = String(tag || '').trim();
  const m = RELEASE_TAG.exec(raw);
  if (!m) throw new Error(`Invalid release tag format: ${raw}`);
  return { core: m[1], version: `${m[1]}${m[2] || ''}` };
}

function main() {
  requireGitWorkTree();

  const forced = String(process.env.PANTARAY_DESKTOP_VERSION || '').trim();
  if (forced) {
    process.stdout.write(`${validateSemver(forced)}\n`);
    return;
  }

  const sha = git(['rev-parse', '--short', 'HEAD']);

  const exactTag = tryGit(['describe', '--tags', '--match', 'v*', '--exact-match']);
  if (exactTag) {
    process.stdout.write(`${parseReleaseTag(exactTag).version}\n`);
    return;
  }

  const latestTag = tryGit(['describe', '--tags', '--match', 'v*', '--abbrev=0']);
  if (!latestTag) {
    const distance = git(['rev-list', '--count', 'HEAD']);
    process.stdout.write(`0.0.0-dev.${distance}+${sha}\n`);
    return;
  }

  const base = parseReleaseTag(latestTag).core;
  const distance = git(['rev-list', '--count', `${latestTag}..HEAD`]);
  process.stdout.write(`${validateSemver(`${base}-dev.${distance}+${sha}`)}\n`);
}

main();


