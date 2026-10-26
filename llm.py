"""LLM 交互模块 - OpenAI 兼容接口（远端 OneAPI 网关）

配置统一从 config.py 加载（数据源：.env，不入库）。
配置校验放在实例化时，避免没有 .env 时 import 失败、难以测试。
"""
import time

import requests

from config import LLM_BASE_URL, LLM_API_KEY, LLM_MODEL

BASE_URL = LLM_BASE_URL
API_KEY = LLM_API_KEY
MODEL_NAME = LLM_MODEL
TIMEOUT = 120
RETRIES = 2


class LLM:
    def __init__(self, model_name=MODEL_NAME):
        if not BASE_URL or not API_KEY:
            raise RuntimeError(
                "缺少 LLM 配置。请复制 .env.example 为 .env 并填入 "
                "LLM_BASE_URL / LLM_API_KEY / LLM_MODEL。"
            )
        self.model_name = model_name or MODEL_NAME

    def chat_with_tools(
        self,
        messages,
        tools,
        temperature=0.2,
        tool_choice="auto",
        max_tokens=512,
        retries=RETRIES,
    ):
        """调用带 tools 参数的接口，返回完整响应 dict（供 agent loop 使用）。

        网络错误或响应结构异常时自动重试；重试耗尽后抛 RuntimeError，
        由调用方降级成文字回复。

        返回结构（OpenAI 兼容协议）：
        {
            "choices": [{
                "message": {"role", "content", "tool_calls"?: [...]},
                "finish_reason": "stop" | "tool_calls" | ...
            }]
        }
        """
        body = {
            "model": self.model_name,
            "messages": messages,
            "temperature": temperature,
            "tools": tools,
        }
        if tool_choice is not None:
            body["tool_choice"] = tool_choice
        if max_tokens is not None:
            body["max_tokens"] = max_tokens

        last_error = None
        for attempt in range(retries + 1):
            try:
                resp = requests.post(
                    f"{BASE_URL}/chat/completions",
                    json=body,
                    headers={"Authorization": f"Bearer {API_KEY}"},
                    timeout=TIMEOUT,
                )
                resp.raise_for_status()
                data = resp.json()
                choices = data.get("choices") if isinstance(data, dict) else None
                if (
                    not choices
                    or not isinstance(choices[0], dict)
                    or not choices[0].get("message")
                ):
                    raise ValueError("LLM 响应缺少 choices/message")
                return data
            except (requests.RequestException, ValueError) as exc:
                last_error = exc
                if attempt < retries:
                    time.sleep(1 + attempt)

        raise RuntimeError(f"LLM 请求失败：{last_error}") from last_error
