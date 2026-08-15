"""Filter coercion and binding: where a user-chosen value meets SQL.

Every test here asserts on both halves -- what the statement says and what the
parameter map holds -- because the guarantee being tested is that a filter value
is never part of the statement at all.
"""

from datetime import datetime, timezone

import pytest

from datatalk.dashboards.filters import (
    FilterError,
    build_bindings,
    coerce_values,
    param_name,
)
from datatalk.warehouse.clickhouse import CLICKHOUSE_DIALECT
from datatalk.warehouse.postgres import POSTGRES_DIALECT

NOW = datetime(2026, 8, 15, tzinfo=timezone.utc)

FILTERS = {
    "version": 1,
    "filters": [
        {"id": "range", "kind": "date_range", "label": "Period",
         "default": {"preset": "last_30_days"}},
        {"id": "region", "kind": "dimension", "label": "Region",
         "column": "region", "source": "main", "multi": True,
         "options": ["EMEA", "APAC", "O'Brien Holdings"],
         "default": {"all": True}},
    ],
    "templates": {
        "q1": {
            "sql": (
                "SELECT month, sum(revenue) AS revenue FROM sales.orders\n"
                "WHERE ({{dt.p_range_all}} OR (ts >= {{dt.p_range_from}} "
                "AND ts < {{dt.p_range_to}}))\n"
                "  AND ({{dt.p_region_all}} OR region {{dt.in:p_region_values}})\n"
                "GROUP BY 1 LIMIT 1000"
            ),
            "params": [
                {"name": "p_range_all", "type": "bool"},
                {"name": "p_range_from", "type": "date"},
                {"name": "p_range_to", "type": "date"},
                {"name": "p_region_all", "type": "bool"},
                {"name": "p_region_values", "type": "string_list"},
            ],
            "columns": ["month", "revenue"],
        }
    },
}


def _bind(selections, dialect=POSTGRES_DIALECT):
    return build_bindings(FILTERS, selections, {"q1": dialect}, now=NOW)


# --- coercion ----------------------------------------------------------------


def test_a_preset_is_resolved_against_the_server_clock():
    out = coerce_values(FILTERS, {"range": {"preset": "last_7_days"}}, now=NOW)
    assert out[param_name("range", "from")].isoformat() == "2026-08-08"
    assert out[param_name("range", "all")] is False


def test_a_client_supplied_window_is_ignored_when_a_preset_is_named():
    """The one input that would otherwise let a caller name any range at all."""
    out = coerce_values(
        FILTERS,
        {"range": {"preset": "last_7_days", "from": "1970-01-01", "to": "2099-01-01"}},
        now=NOW,
    )
    assert out[param_name("range", "from")].isoformat() == "2026-08-08"


def test_all_time_binds_a_flag_rather_than_a_window():
    out = coerce_values(FILTERS, {"range": {"preset": "all_time"}}, now=NOW)
    assert out[param_name("range", "all")] is True


def test_a_custom_range_is_inclusive_of_its_last_day():
    out = coerce_values(
        FILTERS,
        {"range": {"from": "2026-01-01", "to": "2026-01-01"}},
        now=NOW,
    )
    assert out[param_name("range", "from")].isoformat() == "2026-01-01"
    # Exclusive upper bound, so the whole of the 1st is included whatever time
    # component the column carries.
    assert out[param_name("range", "to")].isoformat() == "2026-01-02"


def test_a_backwards_custom_range_is_swapped_not_rejected():
    out = coerce_values(
        FILTERS, {"range": {"from": "2026-03-01", "to": "2026-01-01"}}, now=NOW
    )
    assert out[param_name("range", "from")].isoformat() == "2026-01-01"


def test_a_date_that_is_not_a_date_is_rejected():
    with pytest.raises(FilterError) as exc:
        coerce_values(FILTERS, {"range": {"from": "2026-01-01' OR 1=1 --"}}, now=NOW)
    assert exc.value.code == "filter_value_invalid"


def test_an_unknown_preset_is_rejected():
    with pytest.raises(FilterError) as exc:
        coerce_values(FILTERS, {"range": {"preset": "since_the_dawn_of_time"}}, now=NOW)
    assert exc.value.code == "filter_value_invalid"


def test_an_unknown_filter_id_is_rejected():
    with pytest.raises(FilterError) as exc:
        coerce_values(FILTERS, {"ghost": {"all": True}}, now=NOW)
    assert exc.value.code == "filter_unknown"


def test_a_value_outside_the_options_is_rejected():
    """The allowlist layer, which holds even if binding were weakened."""
    with pytest.raises(FilterError) as exc:
        coerce_values(
            FILTERS,
            {"region": {"values": ["EMEA", "DE'; DROP TABLE orders --"]}},
            now=NOW,
        )
    assert exc.value.code == "filter_value_invalid"


def test_too_many_values_are_rejected():
    with pytest.raises(FilterError):
        coerce_values(FILTERS, {"region": {"values": ["EMEA"] * 500}}, now=NOW)


def test_an_unmentioned_filter_falls_back_to_its_default():
    """So a template never has an unbound placeholder."""
    out = coerce_values(FILTERS, {}, now=NOW)
    assert param_name("region", "all") in out
    assert param_name("range", "from") in out


def test_all_binds_a_flag_and_a_sentinel_not_every_option():
    out = coerce_values(FILTERS, {"region": {"all": True}}, now=NOW)
    assert out[param_name("region", "all")] is True
    # Not the option list: expanding "all" would silently exclude anything the
    # probe truncated or anything added to the warehouse since it ran.
    assert out[param_name("region", "values")] == [""]
    assert "EMEA" not in out[param_name("region", "values")]


def test_an_empty_selection_is_treated_as_all():
    out = coerce_values(FILTERS, {"region": {"values": []}}, now=NOW)
    assert out[param_name("region", "all")] is True


# --- binding -----------------------------------------------------------------


def test_a_selected_value_reaches_the_driver_as_a_parameter_only():
    bound = _bind({"region": {"values": ["EMEA"]}}).bound["q1"]
    assert "EMEA" not in bound.sql
    assert bound.parameters[param_name("region", "values")] == ["EMEA"]


def test_a_quote_in_a_legitimate_option_never_reaches_the_sql():
    """O'Brien Holdings is a real option; naive quoting would break on it."""
    bound = _bind({"region": {"values": ["O'Brien Holdings"]}}).bound["q1"]
    assert "O'Brien" not in bound.sql
    assert bound.parameters[param_name("region", "values")] == ["O'Brien Holdings"]


def test_the_bound_statement_is_identical_whatever_the_selection():
    """With driver binding the SQL does not vary with user input -- which is why
    re-validating it on every refresh checks the same bytes every time."""
    a = _bind({"region": {"values": ["EMEA"]}}).bound["q1"]
    b = _bind({"region": {"values": ["APAC"]}}).bound["q1"]
    c = _bind({"region": {"all": True}}).bound["q1"]
    assert a.sql == b.sql == c.sql


def test_the_bound_sql_passes_the_read_only_guardrails():
    from datatalk.agent.executor import validate_sql

    bound = _bind({"region": {"values": ["EMEA"]}}).bound["q1"]
    assert validate_sql(bound.sql, POSTGRES_DIALECT)


@pytest.mark.parametrize(
    "dialect,needle",
    [
        (POSTGRES_DIALECT, "= ANY(%(p_region_values)s)"),
        (CLICKHOUSE_DIALECT, "IN {p_region_values:Array(String)}"),
    ],
)
def test_each_engine_gets_its_own_placeholder_syntax(dialect, needle):
    bound = _bind({"region": {"all": True}}, dialect).bound["q1"]
    assert needle in bound.sql


def test_a_dataset_with_a_broken_template_is_reported_not_executed():
    filters = {
        **FILTERS,
        "templates": {
            "q1": {
                "sql": "SELECT 1 WHERE x = {{dt.p_never_declared}}",
                "params": [],
                "columns": ["a"],
            }
        },
    }
    out = build_bindings(filters, {}, {"q1": POSTGRES_DIALECT}, now=NOW)
    assert out.bound == {}
    assert out.unfiltered == ["q1"]


def test_a_dashboard_with_no_templates_binds_nothing():
    out = build_bindings({"filters": []}, {}, {}, now=NOW)
    assert out.bound == {} and out.unfiltered == []
