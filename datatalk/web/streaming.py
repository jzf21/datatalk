"""The NDJSON event encoding shared by every streaming endpoint.

One line per event, ``{"kind": ..., "data": {...}}``, always terminated by a
``done`` event. Lives here rather than in ``web/app.py`` so a second router can
stream without importing the app module and creating a cycle.
"""

from __future__ import annotations

import queue
from typing import Any, Iterator

from datatalk import jsonsafe

MEDIA_TYPE = "application/x-ndjson"

# The longest legitimately silent window is a non-streaming author/insight LLM
# call. Anything quieter than this gets a ping so proxy idle timeouts do not
# kill the connection — and so a vanished client surfaces as a failed yield.
HEARTBEAT_SECONDS = 15.0


def drain(
    q: queue.Queue, heartbeat_s: float = HEARTBEAT_SECONDS
) -> Iterator[tuple[str, dict[str, Any]]]:
    """Yield a worker's (kind, data) events until its ``None`` sentinel,
    emitting a ``ping`` whenever nothing has arrived for ``heartbeat_s``."""
    while True:
        try:
            item = q.get(timeout=heartbeat_s)
        except queue.Empty:
            yield ("ping", {})
            continue
        if item is None:
            return
        yield item


def ndjson(kind: str, data: dict[str, Any]) -> str:
    """One event line.

    Goes through :mod:`datatalk.jsonsafe` rather than a bare ``json.dumps``:
    materialized rows carry datetimes and Decimals (hence ``default=str``) but
    also NaN, which ``json.dumps`` writes as a bare ``NaN`` token. The browser's
    ``JSON.parse`` rejects that, and the frontend's deliberately tolerant line
    parser then drops the whole event without a trace -- so a single NaN in one
    cell would make an entire dashboard silently fail to appear.
    """
    return jsonsafe.dumps({"kind": kind, "data": data}) + "\n"
