"""唤醒词识别 - 从 STT 文本中提取唤醒词和后续指令。

只做纯文本处理，录音 / 识别 / 播报由调用方负责，便于单独测试。
"""
import re

_JUNK_RE = re.compile(r"[\s，。！？、,.!?~～…]+")


def _normalize(text):
    return _JUNK_RE.sub("", str(text or ""))


def extract_wake_command(text, wake_word="小马"):
    """提取唤醒词之后的指令。

    Args:
        text: STT 识别文本
        wake_word: 唤醒词，如「小马」

    Returns:
        None: 未命中唤醒词
        "": 只说了唤醒词
        str: 唤醒词之后的指令（已去掉标点和空格）
    """
    wake = _normalize(wake_word)
    if not wake:
        return None

    normalized = _normalize(text)
    index = normalized.find(wake)
    if index < 0:
        return None

    command = normalized[index + len(wake):]
    while command.startswith(wake):
        command = command[len(wake):]
    return command
