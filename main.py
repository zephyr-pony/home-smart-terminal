"""家庭智能终端 - MVP 主程序"""
import os
import sys
import time

from audio_io import record_audio, save_audio, play_audio_file
from stt import STT
from llm import LLM
from memory import Memory
from tts import TTS


def main():
    print("=" * 50)
    print("  家庭智能终端 v0.1")
    print("=" * 50)

    # --- 初始化各模块 ---
    print("\n🔧 正在初始化系统...\n")

    stt = STT()
    llm = LLM()
    memory = Memory()
    tts = TTS()

    print(f"\n📦 记忆库已有 {memory.count()} 条记忆")
    tts.speak("家庭智能终端已启动，随时可以跟我说话。")

    # --- 主循环 ---
    print("\n" + "-" * 50)
    print("使用方式:")
    print("  · 按 Enter 开始语音录音（说完停顿自动结束）")
    print("  · 或直接输入文字")
    print("  · 输入 quit 退出")
    print("-" * 50)

    while True:
        try:
            user_input = input("\n👤 > ").strip()

            if not user_input:
                # --- 语音输入 ---
                audio = record_audio()
                if audio is None:
                    continue

                wav_path = save_audio(audio)
                try:
                    text = stt.transcribe(wav_path)
                finally:
                    if os.path.exists(wav_path):
                        os.remove(wav_path)

                if not text:
                    print("（未识别到语音内容）")
                    continue

                print(f"📝 识别: {text}")
            elif user_input.lower() == 'quit':
                break
            else:
                text = user_input

            # --- 先查现有记忆，再让 LLM 带上下文分类 ---
            related = memory.search(text)
            result = llm.analyze(text, related)
            msg_type = result.get("type", "chat")

            if msg_type == "record":
                # --- 记录记忆 ---
                summary = result.get("summary", text)
                memory.store(text, summary)
                print(f"✅ 已记录: {summary}")
                tts.speak("记住了")

            elif msg_type == "query":
                # --- 查询记忆 ---
                keywords = result.get("keywords", text)
                memories = memory.search(keywords)

                print(f"🔍 检索到 {len(memories)} 条相关记忆")
                for m in memories:
                    print(f"   · {m}")

                answer = llm.answer(text, memories)
                print(f"\n🤖 {answer}")
                tts.speak(answer)

            else:
                # --- 闲聊 ---
                reply = result.get("reply", "好的")
                print(f"🤖 {reply}")
                tts.speak(reply)

        except KeyboardInterrupt:
            print("\n\n再见！")
            break
        except Exception as e:
            print(f"⚠️ 出错: {e}")
            import traceback
            traceback.print_exc()


if __name__ == "__main__":
    main()
