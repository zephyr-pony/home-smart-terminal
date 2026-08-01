"""音频录制与播放模块"""
import numpy as np
import soundfile as sf
import tempfile
import os

from config import SAMPLE_RATE, CHUNK_SIZE, MAX_RECORD_SECONDS, SILENCE_THRESHOLD, SILENCE_DURATION


def record_audio(max_duration=MAX_RECORD_SECONDS, silence_threshold=SILENCE_THRESHOLD, silence_duration=SILENCE_DURATION):
    """录音，检测到静音自动停止。使用 PyAudio（Windows 兼容性最好）。

    Args:
        max_duration: 最大录音时长（秒）
        silence_threshold: 静音能量阈值
        silence_duration: 连续静音多久后停止（秒）

    Returns:
        numpy.ndarray: PCM 音频数据（float32, mono, 16kHz），或 None
    """
    print("🎤 录音中... (说完停顿即可)", flush=True)

    # 释放 pygame.mixer，避免占用音频设备
    try:
        import pygame
        if pygame.mixer.get_init():
            pygame.mixer.quit()
    except Exception:
        pass

    import time as _time
    _time.sleep(0.1)

    import pyaudio

    pa = pyaudio.PyAudio()
    try:
        stream = pa.open(
            format=pyaudio.paFloat32,
            channels=1,
            rate=SAMPLE_RATE,
            input=True,
            frames_per_buffer=CHUNK_SIZE,
        )
    except Exception as e:
        print(f"打开录音设备失败: {e}")
        pa.terminate()
        return None

    # 先采 0.3 秒环境底噪，动态设置静音阈值（适应不同环境/麦克风增益）
    ambient_chunks = []
    for _ in range(int(0.3 * SAMPLE_RATE / CHUNK_SIZE)):
        data = stream.read(CHUNK_SIZE, exception_on_overflow=False)
        ambient_chunks.append(np.frombuffer(data, dtype=np.float32))
    ambient = np.concatenate(ambient_chunks)
    ambient_rms = float(np.sqrt(np.mean(ambient ** 2)))
    effective_threshold = max(silence_threshold, ambient_rms * 2.0)
    print(f"  (环境底噪 {ambient_rms:.4f}, 静音阈值 {effective_threshold:.4f})", flush=True)

    chunks = []
    silence_count = 0
    silence_limit = int(silence_duration * SAMPLE_RATE / CHUNK_SIZE)
    has_speech = False
    total_frames = 0
    max_frames = int(max_duration * SAMPLE_RATE)

    try:
        while total_frames < max_frames:
            data = stream.read(CHUNK_SIZE, exception_on_overflow=False)
            chunk = np.frombuffer(data, dtype=np.float32)
            chunks.append(chunk)
            total_frames += len(chunk)

            energy = np.abs(chunk).mean()
            if energy > effective_threshold:
                has_speech = True
                silence_count = 0
            elif has_speech:
                silence_count += 1
                if silence_count >= silence_limit:
                    break
    finally:
        stream.stop_stream()
        stream.close()
        pa.terminate()

    if not chunks or not has_speech:
        print("（未检测到语音）")
        return None

    audio = np.concatenate(chunks).flatten()

    # 裁剪尾部静音，保留 0.3 秒余量
    envelope = np.abs(audio)
    last_speech_idx = np.where(envelope > effective_threshold)[0]
    if len(last_speech_idx) > 0:
        end = min(last_speech_idx[-1] + int(0.3 * SAMPLE_RATE), len(audio))
        audio = audio[:end]

    # 去除直流偏移 + 音量归一化（麦克风增益低时放大，提升识别率）
    audio = audio - audio.mean()
    peak = np.max(np.abs(audio))
    if peak > 0:
        audio = audio * (0.9 / peak)

    if len(audio) < SAMPLE_RATE * 0.3:  # 不到 0.3 秒
        print("（录音太短）")
        return None

    return audio


def save_audio(audio_np, file_path=None, sample_rate=SAMPLE_RATE):
    """将 numpy 数组保存为 WAV 文件。不指定路径则用临时文件。"""
    if file_path is None:
        file_path = tempfile.mktemp(suffix='.wav')
    sf.write(file_path, audio_np, sample_rate)
    return file_path


def play_audio_file(file_path):
    """播放音频文件（WAV 或 MP3），统一用 pygame。"""
    try:
        import pygame
        if not pygame.mixer.get_init():
            pygame.mixer.init()
        pygame.mixer.music.load(file_path)
        pygame.mixer.music.play()
        while pygame.mixer.music.get_busy():
            pygame.time.Clock().tick(30)
        pygame.mixer.music.unload()
        # 播完释放音频设备，避免和录音冲突
        pygame.mixer.quit()
    except Exception as e:
        print(f"播放出错: {e}")
