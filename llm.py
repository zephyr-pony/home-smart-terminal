"""LLM 交互模块 - OpenAI 兼容接口（远端 OneAPI 网关）

配置统一从 config.py 加载（数据源：.env，不入库）。
"""
import json
import re
import time
from datetime import datetime

import requests

from config import LLM_BASE_URL, LLM_API_KEY, LLM_MODEL

BASE_URL = LLM_BASE_URL
API_KEY = LLM_API_KEY
MODEL_NAME = LLM_MODEL
TIMEOUT = 120

if not BASE_URL or not API_KEY:
    raise RuntimeError(
        "缺少 LLM 配置。请复制 .env.example 为 .env 并填入 LLM_BASE_URL / LLM_API_KEY / LLM_MODEL。"
    )

ANALYZE_PROMPT = """你是家庭记忆系统的分类器。判断用户输入的类型并返回JSON。

当前日期：{today}（回答和摘要里的相对时间必须以这个日期为准换算）

类型说明：
- record: 用户在陈述一个事实或记录信息（系统需要存储这条信息供日后查询）
- query: 用户在提问或查询已记录的信息
- chat: 打招呼、闲聊、或不需要存储的对话

重要规则：
- 用户说"不是X，是Y"或"是Y，不是X"时，事实以纠正后的 Y 为准，不要记录 X
- summary 必须准确反映用户最终想表达的事实，不能与原文冲突
- summary 必须忠实原文，不能凭空添加或重复信息，尽量简洁（15 字内）
- **summary 中的相对时间词（今天/明天/昨天/后天/上周/下月等）必须换算成绝对日期**：如当前日期是 8月8日，则"今天"→"8月8日"、"明天"→"8月9日"。无时间词或原文就是绝对日期时照原样
- 如果输入与"已有记忆"相关，并且带疑问语气（几/多少/什么时候/还有多久/吗/呢/怎么），归类为 query
- 只有明确的陈述事实才归类为 record；拿不准时优先 query
- 用户要求"查看/看看/列出记忆"这类管理指令时，归类为 chat（系统会单独处理），不要返回 query

已有记忆（供判断参考）：
{memories_context}

示例：
用户: "我今天给鼠标换了电池" (当前日期 8月8日) -> {"type": "record", "summary": "8月8日给鼠标换了电池"}
用户: "我今天换了鼠标电池，不是我今年换的" (当前日期 8月8日) -> {"type": "record", "summary": "8月8日换了鼠标电池"}
用户: "冰箱里还有三个鸡蛋" -> {"type": "record", "summary": "冰箱有鸡蛋三个"}
用户: "明天要交电费" (当前日期 8月8日) -> {"type": "record", "summary": "8月9日交电费"}
用户: "我什么时候换的鼠标电池？" -> {"type": "query", "keywords": "鼠标 电池 更换时间"}
用户: "我的酸奶还有几天过期" (已有记忆: 酸奶8月5号过期) -> {"type": "query", "keywords": "酸奶 过期"}
用户: "冰箱里有什么？" -> {"type": "query", "keywords": "冰箱 物品"}
用户: "你好" -> {"type": "chat", "reply": "你好，有什么需要帮忙的吗？"}

只返回JSON，不要其他文字。"""


# 疑问句特征词：命中则倾向 query 类型
QUESTION_WORDS = (
    "吗", "呢", "几", "多少", "什么", "怎么", "怎样", "为什么",
    "何时", "哪", "多久", "几天", "几点", "会不会", "是不是",
    "有没有", "能不能", "是否",
)


def detect_question(text):
    """启发式检测疑问句。以问号结尾必为疑问，含疑问词也视为强线索。"""
    t = text.strip()
    if t.endswith(("？", "?")):
        return True
    return any(w in t for w in QUESTION_WORDS)

ANSWER_PROMPT = """你是家庭智能终端助手"小马"。根据检索到的记忆片段回答用户的问题。
规则：
- 只根据记忆片段回答，不要编造
- 记忆中没有相关信息时，坦诚说"我还没有这方面的记录"
- 回答简洁口语化，像家人聊天一样，不超过3句话
- **每条记忆前面的 [日期] 是记忆的存储时间**。用户问"今天/昨天/几天前/什么时候"等时间相关问题，必须拿记忆日期和用户说的时间对比：
  - 如果记忆日期不是用户问的那天，必须先纠正，例如："你换鼠标电池是 8月1日，不是今天哦" 或 "那是几天前（8月1日）的事了"
  - 严禁把旧记忆说成今天发生的事，也不要在回答里出现"你今天在X月X日"这类自相矛盾的表述

记忆片段：
{memories}

用户问题：{query}"""


def _chat(messages, temperature=0.3, response_format=None, max_tokens=None):
    """调用 OpenAI 兼容的 /v1/chat/completions 接口，返回 content 文本。

    Args:
        messages: 对话消息列表
        temperature: 采样温度
        response_format: {"type": "json_object"} 等
        max_tokens: 最大生成 token 数

    Returns:
        str: 模型回复的 content 文本
    """
    data = _chat_raw(messages, temperature=temperature, response_format=response_format, max_tokens=max_tokens)
    return data["choices"][0]["message"]["content"]


def _chat_with_tools(messages, tools, temperature=0.2, tool_choice="auto", max_tokens=512):
    """调用带 tools 参数的接口，返回完整响应 dict（供 agent loop 使用）。

    返回结构（OpenAI 兼容协议）：
    {
        "choices": [{
            "message": {"role", "content", "tool_calls"?: [{"id", "function": {"name", "arguments"}}]},
            "finish_reason": "stop" | "tool_calls" | ...
        }]
    }
    """
    body = {
        "model": MODEL_NAME,
        "messages": messages,
        "temperature": temperature,
        "tools": tools,
    }
    if tool_choice is not None:
        body["tool_choice"] = tool_choice
    if max_tokens is not None:
        body["max_tokens"] = max_tokens

    resp = requests.post(
        f"{BASE_URL}/chat/completions",
        json=body,
        headers={"Authorization": f"Bearer {API_KEY}"},
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()


def _chat_raw(messages, temperature=0.3, response_format=None, max_tokens=None):
    """底层请求：调用接口并返回完整响应 dict（_chat 与 _chat_with_tools 共用）。"""
    body = {
        "model": MODEL_NAME,
        "messages": messages,
        "temperature": temperature,
    }
    if response_format is not None:
        body["response_format"] = response_format
    if max_tokens is not None:
        body["max_tokens"] = max_tokens

    resp = requests.post(
        f"{BASE_URL}/chat/completions",
        json=body,
        headers={"Authorization": f"Bearer {API_KEY}"},
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()


def _strip_code_fence(text):
    """去掉模型偶尔输出的 ```json ... ``` 代码围栏。"""
    text = text.strip()
    if text.startswith("```"):
        # 去掉首行围栏标记和尾部围栏
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return text


class LLM:
    def __init__(self, model_name=MODEL_NAME):
        self.model_name = model_name

    def analyze(self, text, memories=None):
        """分析用户输入，判断是记录、查询还是闲聊。

        Args:
            text: 用户输入
            memories: 先查出的相关记忆（列表），作为分类上下文

        Returns:
            dict: {"type": "record"|"query"|"chat", ...}
        """
        # 构造记忆上下文 + 注入当前日期（供 summary 相对时间换算）
        if memories:
            memories_context = "\n".join(f"- {m}" for m in memories)
        else:
            memories_context = "（无相关记忆）"
        today = datetime.now().strftime("%Y年%m月%d日")
        prompt = ANALYZE_PROMPT.replace("{memories_context}", memories_context).replace("{today}", today)

        is_q = detect_question(text)

        for attempt in range(3):
            try:
                content = _chat(
                    [
                        {"role": "system", "content": prompt},
                        {"role": "user", "content": text},
                    ],
                    temperature=0.1,
                    response_format={"type": "json_object"},
                    max_tokens=200,
                )
                # 清理代码围栏和 thinking 标签（某些模型会内嵌）
                content = _strip_code_fence(content)
                content = re.sub(r'<think>.*?</think>', '', content, flags=re.DOTALL).strip()
                if not content:
                    if attempt < 2:
                        print(f"  (LLM 空响应，重试 {attempt+1}/3)")
                        time.sleep(1)
                        continue
                    return {"type": "chat", "reply": "我没听清楚，能再说一遍吗？"}

                result = json.loads(content)
                result.setdefault("type", "chat")
                # keywords 可能是 list，统一转成字符串
                if "keywords" in result and isinstance(result["keywords"], list):
                    result["keywords"] = " ".join(result["keywords"])

                # 疑问特征强但被误判为记录 -> 转为查询（先查记忆再答，更安全）
                if is_q and result.get("type") == "record":
                    return {"type": "query", "keywords": text}
                return result
            except (json.JSONDecodeError, KeyError) as e:
                if attempt < 2:
                    print(f"  (LLM 解析失败，重试 {attempt+1}/3: {e})")
                    time.sleep(1)
                    continue
                return {"type": "chat", "reply": "我没听清楚，能再说一遍吗？"}
            except requests.RequestException as e:
                if attempt < 2:
                    print(f"  (LLM 连接失败，重试 {attempt+1}/3: {e})")
                    time.sleep(2)
                    continue
                raise

    def answer(self, query, memories):
        """根据记忆片段生成回答。

        Args:
            query: 用户问题
            memories: 检索到的记忆列表，每项是字符串

        Returns:
            str: 回答文本
        """
        if memories:
            mem_text = "\n".join(f"- {m}" for m in memories)
        else:
            mem_text = "（无相关记忆）"

        prompt = ANSWER_PROMPT.format(memories=mem_text, query=query)
        content = _chat(
            [{"role": "user", "content": prompt}],
            temperature=0.3,
        )
        return content.strip()
