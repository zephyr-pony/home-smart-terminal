"""测试 PyAudio 录音"""
import sys
from audio_io import record_audio, save_audio, play_audio_file
import numpy as np

print("按 Enter 开始录音 3 次测试...")
input()

for i in range(1, 4):
    print(f"\n--- 第 {i} 次录音 ---")
    audio = record_audio()
    if audio is None:
        print("录音失败或未检测到语音")
        continue
    print(f"录音成功: {len(audio)/16000:.1f} 秒, 能量={np.abs(audio).mean():.4f}")
    wav = save_audio(audio, f"test_rec_{i}.wav")
    print(f"已保存: {wav}")

    print("播放录音回放...")
    play_audio_file(wav)

print("\n全部测试完成")
