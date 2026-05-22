import queue

from app.services.event_stream_service import _EventHub


def _make_hub():
    return _EventHub()


def test_register_returns_id_and_queue():
    hub = _make_hub()
    client_id, q = hub.register()
    assert isinstance(client_id, str) and len(client_id) > 0
    assert isinstance(q, queue.Queue)


def test_subscriber_count_increases_on_register():
    hub = _make_hub()
    assert hub.subscriber_count == 0
    hub.register()
    assert hub.subscriber_count == 1
    hub.register()
    assert hub.subscriber_count == 2


def test_unregister_removes_subscriber():
    hub = _make_hub()
    client_id, _ = hub.register()
    hub.unregister(client_id)
    assert hub.subscriber_count == 0


def test_unregister_unknown_id_is_noop():
    hub = _make_hub()
    hub.unregister("nonexistent-id")


def test_publish_delivers_to_subscriber():
    hub = _make_hub()
    _, q = hub.register()
    hub.publish("test_event", {"key": "value"})
    payload = q.get_nowait()
    assert payload["type"] == "test_event"
    assert payload["data"] == {"key": "value"}
    assert "timestamp" in payload


def test_publish_delivers_to_multiple_subscribers():
    hub = _make_hub()
    _, q1 = hub.register()
    _, q2 = hub.register()
    hub.publish("broadcast", {"msg": "hi"})
    assert q1.get_nowait()["type"] == "broadcast"
    assert q2.get_nowait()["type"] == "broadcast"


def test_publish_with_no_subscribers_is_noop():
    hub = _make_hub()
    hub.publish("orphan_event", {})


def test_full_queue_drops_oldest_on_publish():
    hub = _make_hub()
    _, q = hub.register()
    for i in range(_EventHub.MAX_QUEUE_SIZE):
        hub.publish("filler", {"i": i})
    assert q.full()

    hub.publish("new_event", {"seq": "latest"})
    items = []
    while not q.empty():
        items.append(q.get_nowait())
    assert items[-1]["data"]["seq"] == "latest"
    assert len(items) == _EventHub.MAX_QUEUE_SIZE


def test_publish_only_reaches_registered_subscribers():
    hub = _make_hub()
    client_id, q = hub.register()
    hub.unregister(client_id)
    hub.publish("post_unsub", {"x": 1})
    assert q.empty()
