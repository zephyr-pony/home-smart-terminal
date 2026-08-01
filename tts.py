"""语音合成模块 - Edge TTS"""
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
        # 常驻事件循环：不关闭，避免 aiohttp 连接清理时
        # 触发 "Event loop is closed" 警告（Windows Proactor 特有问题）
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)

    def speak(self, text):
        """合成并播放语音。

        Args:
            text: 要说的文本
        """
        if not text.strip():
            return

        # 生成临时 MP3 文件
        mp3_path = tempfile.mktemp(suffix='.mp3')

        try:
            self.loop.run_until_complete(self._synthesize(text, mp3_path))
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
