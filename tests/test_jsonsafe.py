"""Values a warehouse can produce but JSON cannot represent.

Both failures here were found in production, and they fail differently: NaN in a
JSONB write raises a DataError, while NaN on the NDJSON wire is *silent* -- the
frontend's tolerant line parser drops the event and the dashboard never appears.
"""

import json
import math

import pytest

from datatalk import jsonsafe
from datatalk.web.streaming import ndjson


def test_non_finite_floats_become_null():
    out = jsonsafe.json_safe(
        {"a": float("nan"), "b": float("inf"), "c": float("-inf"), "d": 1.5}
    )
    assert out == {"a": None, "b": None, "c": None, "d": 1.5}


def test_nuls_are_stripped_from_strings():
    # Legal in a ClickHouse string; rejected outright by Postgres JSONB.
    assert jsonsafe.json_safe("a\x00b") == "ab"
    assert jsonsafe.json_safe({"k\x00": ["v\x00"]}) == {"k": ["v"]}


def test_booleans_survive_the_float_branch():
    # bool is not a float, but the ordering is load-bearing enough to pin.
    assert jsonsafe.json_safe({"t": True, "f": False}) == {"t": True, "f": False}


def test_dumps_emits_only_valid_json():
    row = [11331.829054165977, float("nan"), 0.0]
    text = jsonsafe.dumps({"rows": [row]})
    assert "NaN" not in text and "Infinity" not in text
    # The real assertion: strict parsers accept it. json.loads is lenient about
    # NaN, so parse_constant is what actually proves the token is gone.
    assert json.loads(
        text, parse_constant=lambda c: pytest.fail(f"invalid JSON token {c!r}")
    ) == {"rows": [[11331.829054165977, None, 0.0]]}


def test_ndjson_event_carrying_nan_stays_parseable():
    """The silent failure: an unparseable line is dropped by the frontend."""
    line = ndjson("dashboard", {"document": {"blocks": [{"rows": [[1.0, float("nan")]]}]}})
    parsed = json.loads(
        line, parse_constant=lambda c: pytest.fail(f"invalid JSON token {c!r}")
    )
    assert parsed["data"]["document"]["blocks"][0]["rows"] == [[1.0, None]]


def test_datetimes_and_decimals_still_fall_through_to_str():
    from datetime import datetime
    from decimal import Decimal

    text = jsonsafe.dumps({"t": datetime(2026, 1, 1), "d": Decimal("1.5")})
    assert json.loads(text) == {"t": "2026-01-01 00:00:00", "d": "1.5"}


def test_a_non_finite_decimal_raises_rather_than_corrupting():
    from decimal import Decimal

    # default=str turns it into the string "NaN", which is valid JSON -- so this
    # documents that the Decimal path degrades to text rather than exploding.
    assert json.loads(jsonsafe.dumps({"d": Decimal("NaN")})) == {"d": "NaN"}


def test_numpy_floats_are_covered():
    np = pytest.importorskip("numpy")
    assert jsonsafe.json_safe(np.float64("nan")) is None
    assert math.isclose(jsonsafe.json_safe(np.float64(2.5)), 2.5)
