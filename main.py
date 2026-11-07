"""家庭智能终端 - CLI 主程序（阶段 1 交互入口，复用 assistant 编排）"""
import os
import sys
import traceback

from config import WAKE_ACK_TEXT, WAKE_WORD, WAKE_WORD_MAX_SECONDS
from audio_io import record_audio, save_audio, play_audio_file
from stt import STT
from llm import LLM
from memory import Memory
from tts import TTS
from assistant import Assistant
from wake_word import extract_wake_command

EXIT_COMMANDS = ("退出", "关闭", "结束", "关机", "拜拜")


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
    elif rtype == "clarify":
        print(f"❓ {result.get('reply', '')}")
    else:  # chat / empty / 其他
        print(f"🤖 {result.get('reply', '')}")


def transcribe_audio(stt, audio):
    """保存录音 → STT 识别 → 删除临时文件。"""
    wav_path = save_audio(audio)
    try:
        return stt.transcribe(wav_path).strip()
    finally:
        if os.path.exists(wav_path):
            os.remove(wav_path)


def run_wake_loop(assistant, stt, tts):
    """常驻监听唤醒词：命中「小马」后执行同句指令，或再录一句指令。"""
    print(f"\n👂 正在监听唤醒词「{WAKE_WORD}」...")
    print(f"   · 可以直接说「{WAKE_WORD}，明天要交电费」")
    print(f"   · 也可以只喊「{WAKE_WORD}」，听到回应后再说")
    print("   · 说「退出」或按 Ctrl+C 结束")
    print("-" * 50)

    while True:
        try:
            audio = record_audio(max_duration=WAKE_WORD_MAX_SECONDS, verbose=False)
            if audio is None:
                continue

            text = transcribe_audio(stt, audio)
            command = extract_wake_command(text, WAKE_WORD)
            if command is None:
                continue

            print(f"🔔 唤醒: {text}")
            if not command:
                if WAKE_ACK_TEXT:
                    tts.speak(WAKE_ACK_TEXT)
                audio = record_audio()
                if audio is None:
                    tts.speak("我没听到，请再说一遍")
                    continue
                command = transcribe_audio(stt, audio)
                if not command:
                    tts.speak("我没听清，请再说一遍")
                    continue

            if command in EXIT_COMMANDS or command.startswith("退出"):
                print("👋 再见！")
                tts.speak("再见")
                return

            print(f"👤 {command}")
            result = assistant.process_text(command, client_id="cli")
            print_result(result)
            tts.speak(result.get("reply", ""))
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            print(f"⚠️ 出错: {exc}")
            traceback.print_exc()


def main():
    wake_mode = any(arg in ("wake", "--wake", "-w") for arg in sys.argv[1:])

    print("=" * 50)
    print("  家庭智能终端 v0.1 (CLI)")
    print("=" * 50)

    # --- 初始化各模块 ---
    print("\n🔧 正在初始化系统...\n")

    llm = LLM()
    stt = STT()
    memory = Memory()
    tts = TTS()
    assistant = Assistant(memory=memory, llm=llm)

    print(f"\n📦 记忆库已有 {memory.count()} 条记忆")

    if wake_mode:
        tts.speak("家庭智能终端已启动，说小马唤醒我。")
        try:
            run_wake_loop(assistant, stt, tts)
        except KeyboardInterrupt:
            print("\n\n再见！")
        return

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
                    result = assistant.process_voice(stt, wav_path, client_id="cli")
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
                result = assistant.process_text(text, client_id="cli")
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
