from __future__ import annotations

import json
import os
from typing import Any

from openai import OpenAI

from llm.base import BaseLLMClient, LLMResponse, ToolCall


class DeepSeekResponsesClient(BaseLLMClient):
    """
    DeepSeek Responses API adapter.

    DeepSeek 提供 OpenAI-compatible API，因此底层仍可以复用 openai Python SDK，
    但 API Key、Base URL、Model 都来自 DeepSeek。

    AgentController 不依赖具体模型供应商；DeepSeek 的适配逻辑收敛在这里。
    """

    DEFAULT_BASE_URL = "https://api.deepseek.com"
    DEFAULT_MODEL = "deepseek-v4-flash"

    def __init__(
        self,
        *,
        api_key: str,
        model: str = DEFAULT_MODEL,
        base_url: str = DEFAULT_BASE_URL,
    ) -> None:
        self.client = OpenAI(
            api_key=api_key,
            base_url=base_url,
        )
        self.model = model

    @classmethod
    def from_env(cls) -> "DeepSeekResponsesClient":
        api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
        if not api_key:
            raise ValueError("缺少 DEEPSEEK_API_KEY")

        model = (
            os.getenv("DEEPSEEK_MODEL", cls.DEFAULT_MODEL).strip()
            or cls.DEFAULT_MODEL
        )
        base_url = (
            os.getenv("DEEPSEEK_BASE_URL", cls.DEFAULT_BASE_URL).strip()
            or cls.DEFAULT_BASE_URL
        )

        return cls(
            api_key=api_key,
            model=model,
            base_url=base_url,
        )

    def create_response(
        self,
        *,
        instructions: str,
        input_items: list[Any],
        tools: list[dict[str, Any]],
    ) -> LLMResponse:
        response = self.client.responses.create(
            model=self.model,
            instructions=instructions,
            input=input_items,
            tools=tools,
        )

        tool_calls: list[ToolCall] = []

        for item in response.output:
            if getattr(item, "type", None) != "function_call":
                continue

            raw_arguments = getattr(item, "arguments", "{}")
            try:
                arguments = json.loads(raw_arguments)
            except json.JSONDecodeError:
                arguments = {
                    "_invalid_json": raw_arguments,
                }

            tool_calls.append(
                ToolCall(
                    call_id=item.call_id,
                    name=item.name,
                    arguments=arguments,
                )
            )

        return LLMResponse(
            text=response.output_text or "",
            output_items=list(response.output),
            tool_calls=tool_calls,
        )
