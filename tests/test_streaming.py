"""The shared NDJSON stream plumbing: heartbeats and the queue drain."""

from __future__ import annotations

import queue
import threading

from datatalk.web.streaming import drain


def test_drain_yields_events_until_the_sentinel():
    q: queue.Queue = queue.Queue()
    q.put(("status", {"message": "hi"}))
    q.put(("result", {"dataset_id": "q1"}))
    q.put(None)

    assert list(drain(q)) == [
        ("status", {"message": "hi"}),
        ("result", {"dataset_id": "q1"}),
    ]


def test_a_quiet_worker_produces_pings_not_silence():
    """A proxy idle timeout kills a silent stream; the ping is the fix."""
    q: queue.Queue = queue.Queue()
    events = []
    for kind, data in drain(q, heartbeat_s=0.01):
        events.append(kind)
        if kind == "ping" and len(events) >= 2:
            # Two pings with no worker at all: the heartbeat does not depend
            # on anything ever arriving.
            q.put(None)
    assert events == ["ping", "ping"]


def test_pings_stop_once_events_flow_again():
    q: queue.Queue = queue.Queue()
    seen = []

    def worker():
        q.put(("status", {"message": "late"}))
        q.put(None)

    threading.Timer(0.05, worker).start()
    for kind, _ in drain(q, heartbeat_s=0.01):
        seen.append(kind)

    assert seen[-1] == "status"
    assert "ping" in seen
