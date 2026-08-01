"""语音识别模块 - FunASR SenseVoice"""
import os
import tempfile

from config import STT_MODEL, STT_LANGUAGE


class STT:
    def __init__(self, model_name=STT_MODEL, language=STT_LANGUAGE):
        """加载 FunASR SenseVoiceSmall 模型。首次运行会自动下载。"""
        print("⏳ 加载语音识别模型 (FunASR SenseVoice)...", flush=True)
        from funasr import AutoModel
        self.model = AutoModel(
            model=model_name,
            trust_remote_code=True,
            device="cpu",
        )
        self.language = language
        print("✅ 语音识别模型已就绪", flush=True)

    def transcribe(self, audio_path):
        """识别音频文件，返回文本。

        Args:
            audio_path: WAV 文件路径

        Returns:
            str: 识别出的文本
        """
        result = self.model.generate(
            input=audio_path,
            cache={},
            language=self.language,
            use_itn=True,
        )
        text = result[0]["text"].strip()
        # SenseVoice 输出可能带 <|zh|> <|HAPPY|> 等标签，清理掉
        import re
        text = re.sub(r'<\|[^|]+\|>', '', text).strip()
        return text
