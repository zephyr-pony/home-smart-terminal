"""家庭智能终端 - CLI 主程序（阶段 1 交互入口，复用 assistant 编排）"""
import os

from audio_io import record_audio, save_audio, play_audio_file
from stt import STT
from llm import LLM
from memory import Memory
from tts import TTS
from assistant import Assistant


def print_result(result, text=None):
    """按结果类型打印 CLI 反馈（TTS 播报由调用方决定）。"""
    rtype = result.get("type")

    if rtype == "list":
        print(f"📋 记忆库共 {len(result.get('memories', []))} 条:")
        for m in result.get("memories", []):
            print(f"   · [{m['time']}] {m['text']}")
    elif rtype == "delete":
        deleted = result.get("deleted", [])
        if deleted:
            print(f"🗑️ 已删除 {len(deleted)} 条相关记忆:")
            for m in deleted:
                print(f"   · [{m['time']}] {m['text']}")
        else:
            print(f"ℹ️ {result.get('reply')}")
    elif rtype == "delete_prompt":
        print(f"❓ {result.get('reply')}")
    elif rtype == "record":
        print(f"✅ 已记录: {result.get('summary', '')}")
    elif rtype == "query":
        memories = result.get("memories", [])
        print(f"🔍 检索到 {len(memories)} 条相关记忆")
        for m in memories:
            print(f"   · {m}")
        print(f"\n🤖 {result.get('reply', '')}")
    else:  # chat / empty / 其他
        print(f"🤖 {result.get('reply', '')}")


def main():
    print("=" * 50)
    print("  家庭智能终端 v0.1 (CLI)")
    print("=" * 50)

    # --- 初始化各模块 ---
    print("\n🔧 正在初始化系统...\n")

    stt = STT()
    llm = LLM()
    memory = Memory()
    tts = TTS()
    assistant = Assistant(memory=memory, llm=llm)

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
                    result = assistant.process_voice(stt, wav_path)
                finally:
                    if os.path.exists(wav_path):
                        os.remove(wav_path)

                if result.get("type") == "empty":
                    print("（未识别到语音内容）")
                    continue

                print(f"📝 识别: {result.get('text', '')}")
                print_result(result)
                tts.speak(result.get("reply", ""))
            elif user_input.lower() == 'quit':
                break
            else:
                # --- 文字输入 ---
                text = user_input
                result = assistant.process_text(text)
                print_result(result)
                tts.speak(result.get("reply", ""))

        except KeyboardInterrupt:
            print("\n\n再见！")
            break
        except Exception as e:
            print(f"⚠️ 出错: {e}")
            import traceback
            traceback.print_exc()


if __name__ == "__main__":
    main()
