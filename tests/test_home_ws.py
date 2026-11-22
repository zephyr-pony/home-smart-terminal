from adapters.home_ws import extract_state_change, websocket_url


def _msg(entity_id, new, old=None, event_type="state_changed"):
    return {
        "type": "event",
        "event": {
            "event_type": event_type,
            "data": {
                "entity_id": entity_id,
                "new_state": {"state": new, "attributes": {"friendly_name": "门铃响了"}},
                "old_state": {"state": old},
            },
        },
    }


def test_websocket_url_converts_scheme_and_path():
    assert websocket_url("http://127.0.0.1:8123") == "ws://127.0.0.1:8123/api/websocket"
    assert websocket_url("https://ha.local/") == "wss://ha.local/api/websocket"


def test_extract_matches_watched_entity():
    hit = extract_state_change(
        _msg("event.doorbell", "2026-11-22T20:59:50+08:00", "unknown"),
        {"event.doorbell"},
    )
    assert hit is not None
    entity_id, state, attrs = hit
    assert entity_id == "event.doorbell"
    assert state.startswith("2026-11-22T20:59:50")
    assert attrs["friendly_name"] == "门铃响了"


def test_extract_ignores_unwatched_entity():
    assert extract_state_change(
        _msg("light.keting", "on", "off"), {"event.doorbell"},
    ) is None


def test_extract_ignores_unchanged_state():
    """属性变了但状态没变（比如亮度微调）不该当成事件。"""
    assert extract_state_change(
        _msg("event.doorbell", "unknown", "unknown"), {"event.doorbell"},
    ) is None


def test_extract_ignores_other_event_types():
    assert extract_state_change(
        _msg("event.doorbell", "x", "y", event_type="call_service"),
        {"event.doorbell"},
    ) is None


def test_extract_ignores_non_event_messages():
    assert extract_state_change({"type": "result", "success": True}, {"a"}) is None
    assert extract_state_change("garbage", {"a"}) is None
