"""Mock LLM client for testing without real Gemini resources.

注意:
    本モックは `<thinking>` / `<answer>` などのタグを出力仕様として扱わない。
    互換性のために任意の文字列（タグ混入を含む）を返すことは可能だが、
    既定の応答はタグ無し（JSON/プレーンテキスト）に寄せる。
"""

import json
import logging
import uuid
from typing import Any, Protocol

from pantaray_llm.contracts.tool_use import (
    LlmToolCall,
    LlmToolUseRequest,
    OpenAiToolContinuation,
)


class GeminiResponse:
    """Geminiレスポンスのモック

    Attributes:
        text (str): テキスト応答
        parsed (object | None): 構造化応答（response_schema使用時を想定）
    """

    def __init__(
        self,
        text: str,
        parsed: object | None = None,
        *,
        tool_calls: tuple[LlmToolCall, ...] = (),
        tool_continuation: OpenAiToolContinuation | None = None,
    ) -> None:
        self.text = text
        self.parsed = parsed
        self.tool_calls = tool_calls
        self.dropped_tool_call_names: tuple[str, ...] = ()
        self.tool_continuation = tool_continuation


class ModelInterface(Protocol):
    """モデルインターフェイス"""

    async def generate_content(self, **kwargs: Any) -> GeminiResponse:
        """コンテンツを生成する"""


class AIOModelInterface:
    """非同期モデルインターフェイス"""

    def __init__(self, parent: Any) -> None:
        self.parent = parent

    @property
    def models(self) -> ModelInterface:
        """モデルを取得する"""
        return self

    async def generate_content(self, **kwargs: Any) -> GeminiResponse:
        """コンテンツを生成する

        Args:
            **kwargs: Gemini API互換のパラメータ

        Returns:
            GeminiResponse: 生成されたレスポンス
        """
        # 親の next_error を確認
        if self.parent.next_error:
            error_to_raise = self.parent.next_error
            self.parent.next_error = None  # 一度使ったらクリア
            raise error_to_raise

        # contentsから最初のテキストを取得
        prompt = ""
        if "contents" in kwargs:
            contents = kwargs["contents"]
            for content in contents:
                if isinstance(content, dict) and "text" in content:
                    prompt = content["text"]
                    break
                elif isinstance(content, str):
                    prompt = content
                    break

        # システム指示/構造化応答設定
        _system_instruction = None
        response_mime_type = None
        response_schema = None
        tool_use = None
        if "config" in kwargs:
            cfg = kwargs["config"]
            if isinstance(cfg, dict):
                _system_instruction = cfg.get("system_instruction", "")
                response_mime_type = cfg.get("response_mime_type")
                response_schema = cfg.get("response_schema")
                tool_use = cfg.get("tool_use")
            else:
                _system_instruction = getattr(cfg, "system_instruction", "")
                response_mime_type = getattr(cfg, "response_mime_type", None)
                response_schema = getattr(cfg, "response_schema", None)
                tool_use = getattr(cfg, "tool_use", None)

        text = self.parent.get_response_text(prompt)
        if tool_use is not None:
            if not isinstance(tool_use, LlmToolUseRequest):
                raise TypeError("Mock tool_use must be an LlmToolUseRequest")
            return self._build_tool_use_response(text=text, tool_use=tool_use)
        parsed = None
        if response_mime_type == "application/json" and response_schema is not None:
            try:
                loaded = json.loads(text)
            except (TypeError, json.JSONDecodeError):
                parsed = None
            else:
                if hasattr(response_schema, "model_validate"):
                    parsed = response_schema.model_validate(loaded)
                else:
                    parsed = loaded
        return GeminiResponse(text, parsed)

    def _build_tool_use_response(
        self, *, text: str, tool_use: LlmToolUseRequest
    ) -> GeminiResponse:
        loaded = json.loads(text)
        if not isinstance(loaded, dict):
            raise ValueError("Mock native tool response must be an object")
        raw_calls = loaded.get("tool_calls")
        specs = raw_calls if isinstance(raw_calls, list) else [loaded]
        calls = tuple(self._build_tool_call(spec, tool_use=tool_use) for spec in specs)
        if len(calls) > tool_use.max_parallel_tool_calls:
            raise ValueError(
                "Mock native tool response exceeds max_parallel_tool_calls"
            )
        return GeminiResponse(
            text,
            tool_calls=calls,
            tool_continuation=None
            if tool_use.continuation_mode == "disabled"
            else OpenAiToolContinuation(
                provider="openai",
                history_items=[
                    {
                        "role": "user",
                        "content": [{"type": "input_text", "text": "prompt"}],
                    }
                ],
            ),
        )

    def _build_tool_call(
        self, spec: object, *, tool_use: LlmToolUseRequest
    ) -> LlmToolCall:
        if not isinstance(spec, dict):
            raise ValueError("Mock native tool call must be an object")
        tool_id = spec.get("tool_id")
        args = spec.get("args")
        if not isinstance(tool_id, str):
            tool_names = [tool.name for tool in tool_use.tools]
            if "submit_suggestion" not in tool_names:
                raise ValueError("Mock native tool response has no tool_id")
            tool_id = "submit_suggestion"
            args = spec
        if not isinstance(args, dict):
            raise ValueError("Mock native tool args must be an object")
        return LlmToolCall(
            call_id=f"mock-{uuid.uuid4()}",
            name=tool_id,
            arguments=args,
        )

    class _StreamChunk:
        """ストリーミングチャンクの簡易モック（textプロパティのみ）"""

        def __init__(self, text: str) -> None:
            self.text = text

    async def generate_content_stream(self, **kwargs: Any) -> Any:  # noqa: D401 - モック
        """コンテンツをストリーミング生成するモック。

        単純化のため、生成されたテキストをスペース区切りで分割して逐次返す。
        """
        # エラー指示があれば例外
        if self.parent.next_error:
            error_to_raise = self.parent.next_error
            self.parent.next_error = None
            raise error_to_raise

        # プロンプトから応答を構築
        prompt = ""
        if "contents" in kwargs:
            contents = kwargs["contents"]
            for content in contents:
                if isinstance(content, dict) and "text" in content:
                    prompt = content["text"]
                    break
                elif isinstance(content, str):
                    prompt = content
                    break

        full_text = self.parent.get_response_text(prompt)
        # 小さめのチャンクに分割
        words = full_text.split(" ") if full_text else []
        if not words:
            yield self._StreamChunk("")
            return
        buf: list[str] = []
        for w in words:
            buf.append(w)
            if len(" ".join(buf)) >= 48:
                yield self._StreamChunk(" ".join(buf))
                buf = []
        if buf:
            yield self._StreamChunk(" ".join(buf))


class MockLLMResponse:
    def __init__(self, text_content: str) -> None:
        self.text = text_content


class MockLLMClient:
    """Geminiのモッククライアント"""

    logger = logging.getLogger(__name__)  # loggerをクラス変数として初期化

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        """初期化"""
        self.config = config or {}

        # テスト用の状態
        # suggestion は `<suggestion>...</suggestion>` 形式で1文の提案を返す。
        # Memory file editor 系エージェントは prompt-visible tool call JSON を返す。
        self.responses: dict[str, Any] = {
            "suggestion": json.dumps(
                {
                    "has_suggestion": False,
                    "interaction_contract": None,
                    "key_point": "",
                    "details": None,
                    "deliverable": None,
                    "agent_session": None,
                    "suggestion_summary": None,
                    "target_context": None,
                }
            ),
            # ActionAgent は `<thinking>`/`<answer>` を使用しない。最終回答は `<final_answer>` を用いる。
            "action": "<final_answer>Mock action answer.</final_answer>",
            # InsightAgent は JSON 出力（facts / short_term_insight_data）を前提とする。
            "insight": json.dumps(
                {
                    "facts": "Mock facts.",
                    "short_term_insight_data": "Mock insight answer.",
                },
                ensure_ascii=False,
            ),
            "default": "Default mock response",
        }
        self.next_response: Any | None = None
        # Served in order after next_response, one per call.
        self.queued_responses: list[Any] = []
        self.next_error: Exception | None = None
        self.last_prompt: str | None = None

        # Gemini APIと互換性のある構造を構築
        self.aio = AIOModelInterface(self)

    def set_next_response(self, response: Any) -> None:
        self.next_response = response

    def set_next_error(self, error: Exception) -> None:
        self.next_error = error

    def get_response_text(self, prompt: str) -> str:
        """プロンプトに基づいて適切な応答テキストを返す"""
        self.last_prompt = prompt
        if not self.next_response and self.queued_responses:
            self.next_response = self.queued_responses.pop(0)
        if self.next_response:
            response_data = self.next_response
            self.next_response = None  # 一度使ったらクリア
            if isinstance(response_data, str):
                return response_data
            if isinstance(response_data, dict):
                # 辞書形式は JSON として返す（タグは出さない）
                return json.dumps(response_data, ensure_ascii=False)

        # プロンプトの内容に基づいて応答を選択
        prompt_lower = prompt.lower()
        if "suggestion task" in prompt_lower:
            agent_type = "suggestion"
        elif "action task" in prompt_lower:
            agent_type = "action"
        elif "insight task" in prompt_lower:
            agent_type = "insight"
        else:
            agent_type = "default"

        response_data = self.responses.get(agent_type, self.responses["default"])

        # response_data が既に文字列の場合はそのまま返す
        if isinstance(response_data, str):
            return response_data

        # response_data が辞書の場合
        if isinstance(response_data, dict):
            # SuggestionAgent についても、辞書からは <suggestion>タグで answer を包んで返す
            if agent_type == "suggestion":
                answer_text: str | None = None
                if "answer" in response_data:
                    answer_text = str(response_data["answer"])
                elif "text" in response_data:
                    answer_text = str(response_data["text"])
                if answer_text is None:
                    self.logger.warning(
                        "MockLLMClient: suggestion response dict に answer/text がありません: %s",
                        response_data,
                    )
                    answer_text = "Mock suggestion answer."
                return f"<suggestion>{answer_text}</suggestion>"

            # それ以外のエージェントは JSON を返す（タグは出さない）
            return json.dumps(response_data, ensure_ascii=False)

        return "Default mock response"  # フォールバック

    async def generate_content_async(
        self, contents: list[Any] | str, **kwargs: Any
    ) -> MockLLMResponse:  # pylint: disable=unused-argument
        """非同期でコンテンツを生成する (BaseAgentからの呼び出しを想定)"""
        if self.next_error:
            error_to_raise = self.next_error
            self.next_error = None  # 一度使ったらクリア
            raise error_to_raise

        # BaseAgentは contents=[prompt] の形式で呼び出すことを想定
        prompt_text = ""
        if isinstance(contents, list) and contents:
            prompt_text = str(contents[0])
        elif isinstance(contents, str):
            prompt_text = contents
        else:
            self.logger.warning(
                "generate_content_async に予期しない形式の contents が渡されました: %s",
                contents,
            )
            prompt_text = "fallback prompt due to unexpected contents"

        # _get_response_text を使用して応答文字列を取得
        response_str = self.get_response_text(prompt_text)
        return MockLLMResponse(text_content=response_str)

    # Vertex AIのクライアントメソッドのモック (もしBaseAgentが直接これらを使うなら)
    async def count_tokens(self, contents: Any) -> dict[str, int]:  # pylint: disable=unused-argument
        """Vertex AI SDKのcount_tokensのモック"""
        return {"total_tokens": 10}  # ダミーのトークン数を返す

    # 他にもモックが必要な google.generativeai.GenerativeModel のメソッドがあればここに追加
    # 例: stream_generate_content など


# Vertex AI SDKのChatSessionのモック（もし使う場合）
# class MockChatSession:
#     def __init__(self, client: MockLLMClient):
#         self.client = client
#         self.history: list[Any] = []

#     async def send_message_async(self, content: Any, generation_config: Any = None, safety_settings: Any = None, tools: Any = None):
#         # 履歴に追加
#         self.history.append({"role": "user", "parts": [content]})

#         # MockLLMClientを使って応答を生成
#         # ここでは単純化のため、プロンプトテキストのみを渡す
#         prompt_text = content
#         if isinstance(content, list) and content and isinstance(content[0], str):
#             prompt_text = content[0]

#         response_text = self.client.get_response_text(prompt_text)
#         mock_response = MockLLMResponse(text_content=response_text)

#         # 履歴に応答を追加
#         self.history.append({"role": "model", "parts": [mock_response.text]})
#         return mock_response

#     async def send_message(self, *args, **kwargs): # 同期版エイリアス
#         return await self.send_message_async(*args, **kwargs)
