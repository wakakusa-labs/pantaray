"""BaseAgent 向けの LLM 生成 mixin。"""

from __future__ import annotations

import asyncio
import contextvars
import random
from typing import NoReturn

from pantaray_agents.agents.core.llm_file_inputs import (
    LlmFileInput,
    interleave_file_inputs,
)
from pantaray_agents.utils.llm_types import types
from pantaray_llm.errors import LlmProxyExecutionError
from pantaray_llm.providers.openai_responses.retry_policy import (
    LLM_TRANSPORT_MAX_ATTEMPTS,
)

from .llm_usage import (
    LlmUsage,
    TokenSink,
    add_usage_metadata,
)

_LLM_THOUGHTS_CONTEXT: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "pantaray_llm_thoughts",
    default=None,
)
"""直近の LLM thought（Geminiの thinking / thoughts）を Task-local に保持する。

前提:
    Gemini の Thought summarization は best-effort であり、
    include_thoughts 等を設定しても常に thought が返る保証はない。

方針:
    thought が存在する場合のみ抽出・保存し、存在しない/構造差分がある場合は例外にせず None とする。
    これにより、thinking モード未使用・thought 未提供の環境でも既存挙動を維持できる。
"""


class LLMGenerationMixin:  # pylint: disable=too-few-public-methods
    """LLM呼び出し（単発/構造化）を提供するミックスイン。"""

    # best-effortな thought を保存する属性（使わない場合は None のまま）
    _last_llm_thoughts: str | None = None
    client: object
    llm_config: dict[str, object]
    DEFAULT_SYSTEM_INSTRUCTION: str

    # LLM（Gemini）一時障害対策: 限定的な指数バックオフリトライ（全エージェント共通）
    _LLM_RETRY_MAX_ATTEMPTS = LLM_TRANSPORT_MAX_ATTEMPTS
    _LLM_RETRY_BASE_DELAYS_S = (0.5, 1.0, 2.0, 4.0)
    _LLM_RETRY_JITTER_MAX_S = 0.2
    LLM_INFERENCE_PROFILE_ID: str | None = None
    LLM_STAGE_PROFILE_IDS: dict[str, str] = {}

    def _resolve_inference_profile_id(self, *, stage: str | None) -> str:
        """Cloud LLM proxy に渡す inference profile ID を返す。"""

        stage_profiles = getattr(self, "LLM_STAGE_PROFILE_IDS", None)
        if isinstance(stage_profiles, dict) and stage is not None:
            stage_profile = stage_profiles.get(stage)
            if isinstance(stage_profile, str) and stage_profile.strip():
                return stage_profile

        profile_id = getattr(self, "LLM_INFERENCE_PROFILE_ID", None)
        if isinstance(profile_id, str) and profile_id.strip():
            return profile_id

        raise RuntimeError(
            f"LLM inference profile is not configured for {type(self).__name__}"
        )

    @classmethod
    def _is_retryable_gemini_error(cls, exc: Exception) -> bool:
        """Gemini 呼び出しのうち「一時的」な失敗のみをリトライ対象とする。

        対象（Gemini API guide に準拠）:
            - 429 RESOURCE_EXHAUSTED
            - 500 INTERNAL
            - 503 UNAVAILABLE
            - 504 DEADLINE_EXCEEDED
            - 一部ネットワーク/タイムアウト系（SDK実装差分があるため best-effort）

        非対象:
            - 400 INVALID_ARGUMENT / 403 PERMISSION_DENIED / 404 NOT_FOUND 等の恒久エラー
        """
        code = getattr(exc, "code", None)
        if code in {429, 500, 503, 504}:
            return True

        msg = str(exc)
        # 明示的な HTTP code / status string
        retryable_tokens = (
            " 429 ",
            "RESOURCE_EXHAUSTED",
            " 500 ",
            " 503 ",
            "UNAVAILABLE",
            " 504 ",
            "DEADLINE_EXCEEDED",
        )
        if any(tok in msg for tok in retryable_tokens):
            return True

        # 例: "code': 429" のような表現揺れ
        if "'code': 429" in msg or '"code": 429' in msg:
            return True
        if "'code': 500" in msg or '"code": 500' in msg:
            return True
        if "'code': 503" in msg or '"code": 503' in msg:
            return True
        if "'code': 504" in msg or '"code": 504' in msg:
            return True

        # ネットワーク/タイムアウト（SDK内部で OSError/TimeoutError などに落ちることがある）
        if isinstance(exc, TimeoutError | ConnectionError | OSError):
            return True
        lowered = msg.lower()
        if "timeout" in lowered or "timed out" in lowered:
            return True
        if "connection reset" in lowered or "connection aborted" in lowered:
            return True
        return False

    @classmethod
    def _is_retryable_llm_error(cls, exc: Exception) -> bool:
        """LLM 失敗の retryable 判定を一元化する。"""

        if isinstance(exc, LlmProxyExecutionError):
            return bool(exc.retryable)
        return cls._is_retryable_gemini_error(exc)

    @staticmethod
    def _raise_llm_upstream_error(exc: Exception) -> NoReturn:
        """LLM upstream 失敗を agent 層に伝える。"""

        if isinstance(exc, LlmProxyExecutionError):
            raise exc
        # repr names the exception class, which is all some failures carry.
        raise RuntimeError(f"LLM upstream error: {exc!r}") from exc

    @classmethod
    async def _sleep_llm_retry_backoff(cls, attempt_index: int) -> None:
        """指数バックオフ（0.3, 0.6, 1.2）+ jitter で待機する。"""
        delays = cls._LLM_RETRY_BASE_DELAYS_S
        base = delays[attempt_index] if attempt_index < len(delays) else delays[-1]
        jitter = random.random() * float(cls._LLM_RETRY_JITTER_MAX_S)
        await asyncio.sleep(float(base) + jitter)

    def _resolve_system_instruction(self, system_instruction: str | None) -> str:
        if system_instruction is not None:
            return system_instruction
        configured = self.llm_config.get("system_instruction")
        if isinstance(configured, str):
            return configured
        return self.DEFAULT_SYSTEM_INSTRUCTION

    @staticmethod
    def _normalize_model_name(model: str) -> str:
        """resource path と version suffix を除いた末尾のモデル名を返す。"""
        raw = (model or "").strip()
        if not raw:
            return raw
        tail = raw.split("/")[-1].strip()
        return tail.split("@", 1)[0].strip().lower()

    @classmethod
    def _is_gemini_3_flash_model(cls, model: object) -> bool:
        """モデルが Gemini 3 Flash 系かを判定する。

        Gemini のモデル名は経路/SDKによりフルパスやサフィックスが付くことがあるため、
        正規化後の末尾名で判定する。
        """
        norm = cls._normalize_model_name(str(model) if model is not None else "")
        return norm.startswith(("gemini-3-flash", "gemini-3.1-flash"))

    @staticmethod
    def _extract_thoughts_best_effort(response: object) -> str | None:
        """LLMレスポンスから thought を best-effort で抽出する。

        Args:
            response: LLM client の response / stream chunk 相当（構造は実装差分がありうる）

        Returns:
            str | None: thought=True な parts の text を改行連結した文字列。
            thought が無い/構造が異なる場合は None。

        注意:
            - best-effort 機能のため、include_thoughts 等を設定しても常に取得できるとは限らない。
            - 例外は飲み込み、呼び出し側の推論処理を壊さない（既存挙動維持）。
        """
        try:
            proxy_thinking = getattr(response, "thinking", None)
            if isinstance(proxy_thinking, str) and proxy_thinking.strip():
                return proxy_thinking

            candidates = getattr(response, "candidates", None)
            if not candidates:
                return None
            content = getattr(candidates[0], "content", None)
            if not content:
                return None
            parts = getattr(content, "parts", None)
            if not parts:
                return None

            thought_texts: list[str] = []
            for part in parts:
                if getattr(part, "thought", False):
                    text = getattr(part, "text", None)
                    if isinstance(text, str) and text.strip():
                        thought_texts.append(text)
            return "\n".join(thought_texts) if thought_texts else None
        except Exception:  # noqa: BLE001 - best-effortで例外を表に出さない
            return None

    def _consume_llm_thoughts(self) -> str | None:
        """直近のLLM呼び出しで取得した thought を返し、保持状態をクリアする。

        Returns:
            str | None: thoughtテキスト（空/空白のみはNoneに正規化する）
        """
        ctx_val = _LLM_THOUGHTS_CONTEXT.get()
        if ctx_val is not None:
            _LLM_THOUGHTS_CONTEXT.set(None)
            if isinstance(ctx_val, str) and ctx_val.strip():
                return ctx_val
            return None
        val = self._last_llm_thoughts
        self._last_llm_thoughts = None
        if isinstance(val, str) and val.strip():
            return val
        return None

    async def _generate_llm_response(
        self,
        prompt: str,
        *,
        sink: TokenSink,
        system_instruction: str | None = None,
        file_inputs: list[LlmFileInput] | None = None,
        stage: str | None = None,
        response_schema: object | None = None,
    ) -> str | object:
        """LLMを使用してプロンプトに対する応答を生成する。

        Args:
            prompt: メインプロンプト
            system_instruction: システム指示
        """
        if response_schema is not None:
            text, parsed = await self._generate_llm_structured(
                prompt=prompt,
                sink=sink,
                response_schema=response_schema,
                system_instruction=system_instruction,
                file_inputs=file_inputs,
                stage=stage,
            )
            return parsed if parsed is not None else text

        sink.guard()
        use_system_instruction = self._resolve_system_instruction(system_instruction)
        inference_profile = self._resolve_inference_profile_id(stage=stage)

        contents = interleave_file_inputs(prompt=prompt, file_inputs=file_inputs)

        response = None
        last_exc: Exception | None = None
        usage_ledger = LlmUsage(None, None)
        for attempt in range(self._LLM_RETRY_MAX_ATTEMPTS):
            try:
                response = await self.client.aio.models.generate_content(  # type: ignore[attr-defined]
                    contents=contents,
                    config=types.GenerateContentConfig(
                        inference_profile=inference_profile,
                        system_instruction=use_system_instruction,
                    ),
                )
                last_exc = None
                break
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                attempt_usage = (
                    exc.usage_metadata
                    if isinstance(exc, LlmProxyExecutionError)
                    else None
                )
                usage_ledger = add_usage_metadata(usage_ledger, attempt_usage)
                should_retry = self._is_retryable_llm_error(exc) and (
                    attempt < self._LLM_RETRY_MAX_ATTEMPTS - 1
                )
                if not should_retry:
                    sink.record(usage_ledger, stage=stage, may_raise=False)
                    self._raise_llm_upstream_error(exc)
                await self._sleep_llm_retry_backoff(attempt)

        if last_exc is not None or response is None:
            if last_exc is None:
                raise RuntimeError("LLM upstream error: unknown failure")
            self._raise_llm_upstream_error(last_exc)
        usage = getattr(response, "usage_metadata", None)
        usage_ledger = add_usage_metadata(usage_ledger, usage)
        # thought は best-effort で抽出（存在しない場合は None）
        thoughts = self._extract_thoughts_best_effort(response)
        self._last_llm_thoughts = thoughts
        _LLM_THOUGHTS_CONTEXT.set(thoughts)
        sink.record(usage_ledger, stage=stage, may_raise=True)
        response_text = getattr(response, "text", None)
        if not isinstance(response_text, str):
            raise RuntimeError("LLM upstream returned non-string text response")
        return response_text

    async def _generate_llm_structured(
        self,
        prompt: str,
        *,
        sink: TokenSink,
        response_schema: object,
        system_instruction: str | None = None,
        file_inputs: list[LlmFileInput] | None = None,
        stage: str | None = None,
    ) -> tuple[str, object | None]:
        """構造化出力（response_schema）でLLMを呼び出す。

        Args:
            prompt: メインプロンプト
            response_schema: 出力スキーマ
            system_instruction: システム指示
        """
        sink.guard()
        use_system_instruction = self._resolve_system_instruction(system_instruction)
        inference_profile = self._resolve_inference_profile_id(stage=stage)

        contents = interleave_file_inputs(prompt=prompt, file_inputs=file_inputs)

        response = None
        last_exc: Exception | None = None
        usage_ledger = LlmUsage(None, None)
        for attempt in range(self._LLM_RETRY_MAX_ATTEMPTS):
            try:
                response = await self.client.aio.models.generate_content(  # type: ignore[attr-defined]
                    contents=contents,
                    config=types.GenerateContentConfig(
                        inference_profile=inference_profile,
                        system_instruction=use_system_instruction,
                        response_mime_type="application/json",
                        response_schema=response_schema,
                    ),
                )
                last_exc = None
                break
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                attempt_usage = (
                    exc.usage_metadata
                    if isinstance(exc, LlmProxyExecutionError)
                    else None
                )
                usage_ledger = add_usage_metadata(usage_ledger, attempt_usage)
                should_retry = self._is_retryable_llm_error(exc) and (
                    attempt < self._LLM_RETRY_MAX_ATTEMPTS - 1
                )
                if not should_retry:
                    sink.record(usage_ledger, stage=stage, may_raise=False)
                    self._raise_llm_upstream_error(exc)
                await self._sleep_llm_retry_backoff(attempt)

        if last_exc is not None or response is None:
            if last_exc is None:
                raise RuntimeError("LLM upstream error: unknown failure")
            self._raise_llm_upstream_error(last_exc)
        usage = getattr(response, "usage_metadata", None)
        usage_ledger = add_usage_metadata(usage_ledger, usage)
        # thought は best-effort で抽出（存在しない場合は None）
        thoughts = self._extract_thoughts_best_effort(response)
        self._last_llm_thoughts = thoughts
        _LLM_THOUGHTS_CONTEXT.set(thoughts)
        sink.record(usage_ledger, stage=stage, may_raise=True)
        response_text = getattr(response, "text", None)
        if not isinstance(response_text, str):
            raise RuntimeError("LLM upstream returned non-string structured response")
        return response_text, getattr(response, "parsed", None)
