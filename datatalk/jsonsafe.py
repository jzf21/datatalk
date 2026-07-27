"""Making warehouse values safe to serialize as JSON.

Warehouse rows carry values that are legal in their engine and in Python but not
in JSON, and every one of them fails at a *different* boundary, silently or
loudly:

* **NUL bytes** are legal in a ClickHouse string. Postgres JSONB rejects them
  outright (SQLite TEXT accepted them, which is why this only appeared after the
  move to Postgres).
* **NaN and ±Infinity** are ordinary Python floats, and any aggregate can
  produce one -- ``0/0``, ``avg()`` over an empty group, a variance of a single
  row. ``json.dumps`` emits them as the bare tokens ``NaN``/``Infinity``, which
  are *not* JSON. Postgres JSONB rejects them with a DataError, and the
  browser's ``JSON.parse`` rejects them too -- so an un-sanitized value does not
  merely fail to save, it makes the NDJSON line carrying the document
  unparseable, and the frontend's tolerant line parser drops the whole event
  without a word. A dashboard would simply never appear.

So sanitizing has to happen at *both* serialization boundaries: the JSONB
columns (via the engine's ``json_serializer``) and the NDJSON wire. This module
is the one implementation both use.

Non-finite floats become ``None`` rather than a string: they mean "no value
here", every consumer already handles null, and ``"NaN"`` would render as text
in a table cell.
"""

from __future__ import annotations

import json
import math
from typing import Any


def json_safe(obj: Any) -> Any:
    """Recursively replace values JSON cannot represent.

    NUL bytes are stripped from strings; NaN and ±Infinity become ``None``.
    Everything else is returned untouched -- types that merely lack a JSON
    encoding (datetime, Decimal, UUID) are left for ``default=str``.
    """
    if isinstance(obj, str):
        return obj.replace("\x00", "")
    # bool before float: bools are ints, not floats, but keeping the ordering
    # explicit stops a future edit from routing True through isfinite().
    if isinstance(obj, bool):
        return obj
    if isinstance(obj, float):
        # Covers numpy.float64 too, which subclasses float.
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {json_safe(k): json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [json_safe(v) for v in obj]
    return obj


def dumps(obj: Any) -> str:
    """``json.dumps`` that cannot emit a token JSON does not have.

    ``default=str`` is the project-wide convention (warehouse rows carry
    datetimes and Decimals); ``allow_nan=False`` turns any non-finite value that
    slips past :func:`json_safe` -- inside a Decimal, say -- into a loud
    ValueError here rather than a confusing DataError from Postgres or a
    silently dropped line in the browser.
    """
    return json.dumps(json_safe(obj), default=str, allow_nan=False)
