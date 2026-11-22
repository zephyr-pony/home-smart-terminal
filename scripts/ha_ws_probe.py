"""真实连一次 HA 事件流，证明确实能收到事件（不需要门铃）。

订阅温湿度计的温度实体——它自己会周期性上报，90 秒内几乎一定有更新。
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from adapters.home import HAClient  # noqa: E402
from adapters.home_ws import run_event_stream  # noqa: E402
from config import HOME_BASE_URL  # noqa: E402

TEMP = "sensor.miaomiaoc_cn_blt_3_1lsvig5bh0c00_t9_temperature_p_3_1001"
LOCK_EVENTS = [
    "event.loock_cn_1109372528_t3pul_doorbell_ring_e_7_1",
    "event.loock_cn_1109372528_t3pul_lock_event_e_2_1020",
]
WATCHED = [TEMP] + LOCK_EVENTS

received = []


async def on_event(entity_id, new_state, attributes):
    name = attributes.get("friendly_name") or entity_id
    received.append((entity_id, new_state))
    print("收到事件：%s → %s" % (name, new_state), flush=True)


async def main():
    client = HAClient()
    print("连 %s，订阅 %d 个实体" % (HOME_BASE_URL, len(WATCHED)), flush=True)
    task = asyncio.create_task(run_event_stream(client, WATCHED, on_event))
    try:
        await asyncio.wait_for(task, timeout=90)
    except asyncio.TimeoutError:
        pass
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    print("共收到 %d 条事件" % len(received), flush=True)
    return 0 if received else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
