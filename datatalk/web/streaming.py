"""The NDJSON event encoding shared by every streaming endpoint.

One line per event, ``{"kind": ..., "data": {...}}``, always terminated by a
``done`` event. Lives here rather than in ``web/app.py`` so a second router can
stream without importing the app module and creating a cycle.
"""

from __future__ import annotations

import json
from typing import Any

MEDIA_TYPE = "application/x-ndjson"


def ndjson(kind: str, data: dict[str, Any]) -> str:
    # default=str: materialized rows can hold datetimes/Decimals from ClickHouse.
    return json.dumps({"kind": kind, "data": data}, default=str) + "\n"
