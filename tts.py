"""语音合成模块 - Edge TTS

两种使用方式：
- CLI/本机：`speak(text)` — 合成 + pygame 播放
- 服务端：`synthesize_bytes(text)` — async 合成返回 mp3 字节（FastAPI 直接 await）
          或 `synthesize_to_file(text, path)` — 同步合成到文件

注意：私有 asyncio loop 一旦创建永不 close（Windows Proactor 下
close 会触发 "Event loop is closed" 警告，历史坑）。
"""
import asyncio
import edge_tts
import tempfile
import os

from config import TTS_VOICE


class TTS:
    def __init__(self, voice=TTS_VOICE):
        """初始化 Edge TTS。

        Args:
            voice: 微软语音角色，默认晓晓（女声）
                   其他可选: zh-CN-YunxiNeural(男声), zh-CN-XiaoyiNeural(女声)
        """
        self.voice = voice
        # 常驻事件循环：仅 CLI 的 speak() 使用；服务端路径不碰它。
        # 不关闭，避免 aiohttp 连接清理时触发 "Event loop is closed" 警告。
        self.loop = None

    def _get_loop(self):
        """懒创建私有事件循环（CLI 播放用）。"""
        if self.loop is None:
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)
        return self.loop

    # ---------- 服务端：async 合成 ----------

    async def synthesize_bytes(self, text):
        """异步合成语音，返回 mp3 字节（FastAPI async handler 直接 await）。

        Args:
            text: 要合成的文本

        Returns:
            bytes: MP3 音频数据
        """
        if not text or not text.strip():
            return b""
        communicate = edge_tts.Communicate(text, self.voice)
        chunks = []
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                chunks.append(chunk["data"])
        return b"".join(chunks)

    def synthesize_to_file(self, text, output_path):
        """同步合成语音到文件（供后台线程/脚本使用）。

        Args:
            text: 要合成的文本
            output_path: 输出 mp3 文件路径
        """
        if not text or not text.strip():
            return
        self._get_loop().run_until_complete(self._synthesize(text, output_path))

    # ---------- CLI：合成 + 本机播放 ----------

    def speak(self, text):
        """合成并播放语音（CLI/本机用）。

        Args:
            text: 要说的文本
        """
        if not text.strip():
            return

        # 生成临时 MP3 文件
        mp3_path = tempfile.mktemp(suffix='.mp3')

        try:
            self.synthesize_to_file(text, mp3_path)
        except Exception as e:
            print(f"TTS 出错: {e}")
            return

        # 播放
        from audio_io import play_audio_file
        play_audio_file(mp3_path)

        # 清理临时文件
        try:
            os.remove(mp3_path)
        except Exception:
            pass

    async def _synthesize(self, text, output_path):
        """异步合成语音到文件。"""
        communicate = edge_tts.Communicate(text, self.voice)
        await communicate.save(output_path)
