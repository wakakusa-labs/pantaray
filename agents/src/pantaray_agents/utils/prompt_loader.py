"""プロンプトローダーユーティリティ

YAMLファイルからプロンプトをロードし、適切なフォーマットで返す機能を提供します。
system_instruction と prompt を分離して管理できます。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml  # type: ignore[import-untyped]


@dataclass
class PromptConfig:
    """プロンプト設定を保持するデータクラス。

    Attributes:
        prompt: 動的なコンテキストを含むプロンプトテンプレート
        system_instruction: 静的な指示（役割、出力形式、ガイドラインなど）
    """

    prompt: str
    system_instruction: str | None = None
    # One section per role that shares the system instruction (Action only).
    role_rules: dict[str, str] = field(default_factory=dict)
    # Templates for a changed part of the Action prompt's head (Action only).
    world_state_updates: dict[str, str] = field(default_factory=dict)

    def require_role_rule(self, key: str) -> str:
        """指定キーの role rule を取得する。"""

        return _require_entry(self.role_rules, kind="role rule", key=key)

    def require_world_state_update(self, key: str) -> str:
        """指定キーの world state 更新テンプレートを取得する。"""

        return _require_entry(
            self.world_state_updates, kind="world state update", key=key
        )


def _require_entry(entries: dict[str, str], *, kind: str, key: str) -> str:
    entry = entries.get(key)
    if entry is None:
        raise ValueError(f"Prompt {kind} not found: {key}")
    normalized = entry.strip()
    if not normalized:
        raise ValueError(f"Prompt {kind} is empty: {key}")
    return normalized


class PromptLoader:
    """プロンプトローダークラス

    YAMLファイルからプロンプトをロードし、キャッシュする機能を提供します。
    system_instruction キーがある場合はそれも読み込みます。
    """

    def __init__(self, prompt_dir: str | Path | None = None):
        """初期化

        Args:
            prompt_dir: プロンプトディレクトリのパス（指定しない場合はデフォルトパスを使用）
        """
        if prompt_dir is None:
            self.prompt_dir = Path(__file__).parent.parent / "prompts"
        else:
            self.prompt_dir = Path(prompt_dir)

        # プロンプト設定キャッシュ
        self._config_cache: dict[str, PromptConfig] = {}
        # 後方互換性のためのプロンプト文字列キャッシュ
        self._prompt_cache: dict[str, str] = {}

    def load_config(self, prompt_name: str) -> PromptConfig:
        """プロンプト設定全体をロードする

        Args:
            prompt_name: プロンプト名（拡張子なし）

        Returns:
            PromptConfig: system_instruction と prompt を含む設定

        Raises:
            FileNotFoundError: プロンプトファイルが見つからない場合
            ValueError: プロンプトファイルの形式が不正な場合
        """
        # キャッシュに存在する場合はキャッシュから返す
        if prompt_name in self._config_cache:
            return self._config_cache[prompt_name]

        # プロンプトファイルのパスを構築
        prompt_file = self.prompt_dir / f"{prompt_name}.yaml"

        # ファイルが存在しない場合はエラー
        if not prompt_file.exists():
            raise FileNotFoundError(f"Prompt file not found: {prompt_file}")

        # YAMLファイルを読み込む
        try:
            with open(prompt_file, encoding="utf-8") as f:
                prompt_data = yaml.safe_load(f)
        except Exception as e:
            raise ValueError(
                f"Failed to load prompt file {prompt_file}: {str(e)}"
            ) from e

        # プロンプト文字列を取得（キー'prompt'があることを期待）
        if not isinstance(prompt_data, dict) or "prompt" not in prompt_data:
            raise ValueError(
                f"Invalid prompt file format in {prompt_file}. Missing 'prompt' key."
            )

        config = PromptConfig(
            prompt=prompt_data["prompt"],
            system_instruction=prompt_data.get("system_instruction"),
            role_rules=_parse_prompt_string_map(
                prompt_data.get("role_rules", {}),
                prompt_file=prompt_file,
                section_name="role_rules",
            ),
            world_state_updates=_parse_prompt_string_map(
                prompt_data.get("world_state_updates", {}),
                prompt_file=prompt_file,
                section_name="world_state_updates",
            ),
        )

        # キャッシュに保存
        self._config_cache[prompt_name] = config

        return config

    def load_prompt(self, prompt_name: str) -> str:
        """指定した名前のプロンプトをロードする（後方互換性のため維持）

        Args:
            prompt_name: プロンプト名（拡張子なし）

        Returns:
            str: ロードされたプロンプト文字列

        Raises:
            FileNotFoundError: プロンプトファイルが見つからない場合
            ValueError: プロンプトファイルの形式が不正な場合
        """
        # キャッシュに存在する場合はキャッシュから返す
        if prompt_name in self._prompt_cache:
            return self._prompt_cache[prompt_name]

        # load_config を使用して取得
        config = self.load_config(prompt_name)
        prompt_text = config.prompt

        # 後方互換性キャッシュに保存
        self._prompt_cache[prompt_name] = prompt_text

        return prompt_text

    def get_full_path(self, prompt_name: str) -> Path:
        """プロンプトファイルのフルパスを取得する

        Args:
            prompt_name: プロンプト名（拡張子なし）

        Returns:
            Path: プロンプトファイルの絶対パス
        """
        return self.prompt_dir / f"{prompt_name}.yaml"


# シングルトンインスタンス（複数箇所で同じインスタンスを使用できるように）
prompt_loader = PromptLoader()


def _parse_prompt_string_map(
    raw: object,
    *,
    prompt_file: Path,
    section_name: str,
) -> dict[str, str]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError(
            f"Invalid prompt file format in {prompt_file}. "
            f"'{section_name}' must be a mapping."
        )
    parsed: dict[str, str] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not key.strip():
            raise ValueError(
                f"Invalid prompt file format in {prompt_file}. "
                f"'{section_name}' keys must be non-empty strings."
            )
        if not isinstance(value, str) or not value.strip():
            raise ValueError(
                f"Invalid prompt file format in {prompt_file}. "
                f"'{section_name}.{key}' must be a non-empty string."
            )
        parsed[key.strip()] = value.strip()
    return parsed


def load_prompt(prompt_name: str) -> str:
    """指定した名前のプロンプトをロードする（シングルトン版）

    Args:
        prompt_name: プロンプト名（拡張子なし）

    Returns:
        str: ロードされたプロンプト文字列
    """
    return prompt_loader.load_prompt(prompt_name)


def load_config(prompt_name: str) -> PromptConfig:
    """プロンプト設定全体をロードする（シングルトン版）

    Args:
        prompt_name: プロンプト名（拡張子なし）

    Returns:
        PromptConfig: system_instruction と prompt を含む設定
    """
    return prompt_loader.load_config(prompt_name)
