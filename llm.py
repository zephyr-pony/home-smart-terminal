"""LLM 交互模块 - OpenAI 兼容接口（远端 OneAPI 网关）

配置统一从 config.py 加载（数据源：.env，不入库）。
配置校验放在实例化时，避免没有 .env 时 import 失败、难以测试。
"""
import json
import time

import requests

from config import (
    LLM_BASE_URL,
    LLM_API_KEY,
    LLM_MODEL,
    LLM_REASONING_EFFORT,
    LLM_CHAT_TEMPLATE_KWARGS,
)

BASE_URL = LLM_BASE_URL
API_KEY = LLM_API_KEY
MODEL_NAME = LLM_MODEL
# 语音场景对首字延迟敏感：默认关掉思考模式（实测 5s → 2s）
REASONING_EFFORT = LLM_REASONING_EFFORT

# 本地 llama.cpp / vLLM 用模板参数关思考；解析失败就当没配
try:
    CHAT_TEMPLATE_KWARGS = json.loads(LLM_CHAT_TEMPLATE_KWARGS) if LLM_CHAT_TEMPLATE_KWARGS else {}
except ValueError:
    CHAT_TEMPLATE_KWARGS = {}
TIMEOUT = 120
RETRIES = 2


def _apply_reasoning(body):
    """把「关思考」的相关参数塞进请求体，两个后端参数名不同，都支持。"""
    effort = str(REASONING_EFFORT or "").strip().lower()
    if effort and effort != "off":
        body["reasoning_effort"] = effort
    if CHAT_TEMPLATE_KWARGS:
        body["chat_template_kwargs"] = CHAT_TEMPLATE_KWARGS
    return body


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
        on_delta=None,
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
        }
        # 网关不接受 tools: []，空列表要整个省略
        if tools:
            body["tools"] = tools
            if tool_choice is not None:
                body["tool_choice"] = tool_choice
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        # 网关支持 reasoning_effort：none 即关闭思考，能省掉几秒
        _apply_reasoning(body)

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

    def chat_with_tools_stream(
        self,
        messages,
        tools,
        temperature=0.2,
        tool_choice="auto",
        max_tokens=512,
        on_delta=None,
    ):
        """流式版本：拿到一段就回调一段，返回与 chat_with_tools 相同结构。

        on_delta 在工具调用轮可能一次都不会触发（模型只吐 tool_calls）。
        """
        body = {
            "model": self.model_name,
            "messages": messages,
            "temperature": temperature,
            "stream": True,
        }
        if tools:
            body["tools"] = tools
            if tool_choice is not None:
                body["tool_choice"] = tool_choice
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        _apply_reasoning(body)

        try:
            resp = requests.post(
                f"{BASE_URL}/chat/completions",
                json=body,
                headers={"Authorization": f"Bearer {API_KEY}"},
                timeout=TIMEOUT,
                stream=True,
            )
            resp.raise_for_status()
            return self._consume_stream(resp, on_delta)
        except Exception as exc:
            raise RuntimeError(f"LLM 流式请求失败：{exc}") from exc

    @staticmethod
    def _consume_stream(resp, on_delta):
        content_parts = []
        tool_calls = {}
        finish = None

        for raw in resp.iter_lines(decode_unicode=False):
            if not raw:
                continue
            line = raw.decode("utf-8", "ignore") if isinstance(raw, bytes) else raw
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            try:
                chunk = json.loads(payload)
            except ValueError:
                continue
            choices = chunk.get("choices") or []
            if not choices:
                continue
            choice = choices[0]
            if choice.get("finish_reason"):
                finish = choice["finish_reason"]
            delta = choice.get("delta") or {}

            piece = delta.get("content")
            if piece:
                content_parts.append(piece)
                if on_delta is not None:
                    on_delta(piece)

            for call in delta.get("tool_calls") or []:
                index = call.get("index", 0)
                slot = tool_calls.setdefault(index, {
                    "id": "", "type": "function",
                    "function": {"name": "", "arguments": ""},
                })
                if call.get("id"):
                    slot["id"] = call["id"]
                function = call.get("function") or {}
                if function.get("name"):
                    slot["function"]["name"] += function["name"]
                if function.get("arguments"):
                    slot["function"]["arguments"] += function["arguments"]

        message = {
            "role": "assistant",
            "content": "".join(content_parts) or None,
        }
        if tool_calls:
            message["tool_calls"] = [tool_calls[i] for i in sorted(tool_calls)]
            finish = finish or "tool_calls"
        return {
            "choices": [{
                "message": message,
                "finish_reason": finish or "stop",
            }]
        }
