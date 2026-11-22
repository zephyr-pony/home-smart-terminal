"""设备网关自检：确认 .env 里的 HOME_TOKEN 能用，并把设备清单打出来。

用法：
    python scripts/ha_check.py            列出白名单设备
    python scripts/ha_check.py 客厅灯      查某个设备
    python scripts/ha_check.py --alerts   预览「如果现在巡检，会提醒什么」（不写状态）

只读，不会动任何设备、也不会消耗提醒去重额度。给小马接设备前先跑这个：
    - 令牌无效 → 明确告诉你换一个
    - 连不上   → 说明 HA 容器没起来
    - 正常     → 列出设备名和 entity_id，方便填 HOME_CONTROL_ALLOW
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from adapters.home import HAClient, HomeDevices, HomeUnavailable  # noqa: E402


def _preview_alerts(devices):
    from agent.home_watch import HomeWatcher
    from config import AGENT_DB_PATH
    from domain.store import AgentStore

    store = AgentStore(AGENT_DB_PATH)
    try:
        watcher = HomeWatcher(devices, store)
        alerts = watcher.check(apply=False)
    finally:
        store.close()

    if not alerts:
        print("巡检结果：没有要提醒的")
        return
    print("巡检预览（真的跑起来会推送这些）：")
    for alert in alerts:
        print("  [%s] %s" % (alert["kind"], alert["text"]))


def main():
    client = HAClient()
    if not client.configured:
        print("没配置：.env 里 HOME_TOKEN 是空的")
        return 1

    try:
        rows = client.states(max_age=0)
    except HomeUnavailable as exc:
        print("失败：%s" % exc)
        return 1

    home = HomeDevices(client=client)
    devices = home._devices()
    print("HA 可访问，实体 %d 个，其中设备 %d 个" % (len(rows), len(devices)))

    args = [a for a in sys.argv[1:]]
    if args and args[0] == "--alerts":
        print()
        _preview_alerts(home)
        return 0

    keyword = " ".join(args).strip()
    if keyword:
        print()
        print(home.query(keyword))
    else:
        print()
        for row in devices:
            attrs = row.get("attributes") or {}
            print("- %-28s %-30s %s" % (
                attrs.get("friendly_name") or "",
                row.get("entity_id", ""),
                row.get("state", ""),
            ))
    return 0


if __name__ == "__main__":
    sys.exit(main())
