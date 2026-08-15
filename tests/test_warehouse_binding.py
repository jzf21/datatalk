"""Template binding: the layer that keeps filter values out of the SQL text.

These are the tests that matter most in the dashboard-filter feature. Everything
above them assumes that a value chosen in a dropdown reaches the driver as a
*parameter*, never as bytes in the statement -- so each test here asserts on both
halves: what the SQL says, and what the parameter map holds.
"""

import pytest

from datatalk.warehouse.base import Dialect
from datatalk.warehouse.binding import (
    BoundQuery,
    ParamSpec,
    TemplateError,
    bind,
    parameter_names,
)
from datatalk.warehouse.clickhouse import CLICKHOUSE_DIALECT
from datatalk.warehouse.postgres import POSTGRES_DIALECT

DATE_PARAMS = [ParamSpec("p_from", "date"), ParamSpec("p_to", "date")]
REGION_PARAMS = [
    ParamSpec("p_region_all", "bool"),
    ParamSpec("p_region", "string_list"),
]


def test_postgres_renders_pyformat_placeholders():
    out = bind(
        "SELECT 1 WHERE ts >= {{dt.p_from}} AND ts < {{dt.p_to}}",
        DATE_PARAMS,
        {"p_from": "2026-01-01", "p_to": "2026-04-01"},
        POSTGRES_DIALECT,
    )
    assert out.sql == "SELECT 1 WHERE ts >= %(p_from)s AND ts < %(p_to)s"
    assert out.parameters == {"p_from": "2026-01-01", "p_to": "2026-04-01"}


def test_clickhouse_renders_typed_curly_placeholders():
    out = bind(
        "SELECT 1 WHERE ts >= {{dt.p_from}} AND ts < {{dt.p_to}}",
        DATE_PARAMS,
        {"p_from": "2026-01-01", "p_to": "2026-04-01"},
        CLICKHOUSE_DIALECT,
    )
    assert out.sql == "SELECT 1 WHERE ts >= {p_from:Date} AND ts < {p_to:Date}"


@pytest.mark.parametrize(
    "dialect,expected",
    [
        (POSTGRES_DIALECT, "= ANY(%(p_region)s)"),
        (CLICKHOUSE_DIALECT, "IN {p_region:Array(String)}"),
    ],
)
def test_the_membership_form_renders_the_whole_operator(dialect, expected):
    """The engines differ in shape, not just spelling -- so the operator is ours."""
    out = bind(
        "SELECT 1 WHERE region {{dt.in:p_region}}",
        [REGION_PARAMS[1]],
        {"p_region": ["EMEA"]},
        dialect,
    )
    assert out.sql == f"SELECT 1 WHERE region {expected}"


def test_a_quote_in_an_allowed_value_never_reaches_the_sql():
    """The O'Brien case: a legitimate option that would break naive quoting."""
    value = "O'Brien Holdings"
    out = bind(
        "SELECT 1 WHERE customer = {{dt.p_customer}}",
        [ParamSpec("p_customer", "string")],
        {"p_customer": value},
        POSTGRES_DIALECT,
    )
    assert value not in out.sql
    assert "'" not in out.sql
    assert out.parameters == {"p_customer": value}


def test_an_injection_attempt_stays_entirely_in_the_parameter_map():
    hostile = "DE'; DROP TABLE orders --"
    out = bind(
        "SELECT 1 WHERE region = {{dt.p_region}}",
        [ParamSpec("p_region", "string")],
        {"p_region": hostile},
        POSTGRES_DIALECT,
    )
    assert out.sql == "SELECT 1 WHERE region = %(p_region)s"
    assert "DROP" not in out.sql
    assert out.parameters == {"p_region": hostile}


def test_literal_percent_is_escaped_before_placeholders_are_inserted():
    """The psycopg trap: `LIKE '%eu%'` raises once any parameter is supplied."""
    out = bind(
        "SELECT 1 WHERE name LIKE '%eu%' AND ts >= {{dt.p_from}}",
        [ParamSpec("p_from", "date")],
        {"p_from": "2026-01-01"},
        POSTGRES_DIALECT,
    )
    assert "'%%eu%%'" in out.sql
    # The placeholder we inserted afterwards must NOT have been doubled.
    assert "%(p_from)s" in out.sql
    assert "%%(p_from)s" not in out.sql


def test_clickhouse_leaves_literal_percent_alone():
    out = bind(
        "SELECT 1 WHERE name LIKE '%eu%' AND ts >= {{dt.p_from}}",
        [ParamSpec("p_from", "date")],
        {"p_from": "2026-01-01"},
        CLICKHOUSE_DIALECT,
    )
    assert "'%eu%'" in out.sql


def test_an_undeclared_placeholder_raises_rather_than_executing():
    with pytest.raises(TemplateError, match="undeclared"):
        bind(
            "SELECT 1 WHERE ts >= {{dt.p_from}} AND region = {{dt.p_region}}",
            [ParamSpec("p_from", "date")],
            {"p_from": "2026-01-01"},
            POSTGRES_DIALECT,
        )


def test_a_declared_parameter_with_no_value_raises():
    with pytest.raises(TemplateError, match="no value"):
        bind(
            "SELECT 1 WHERE ts >= {{dt.p_from}}",
            DATE_PARAMS,
            {"p_from": "2026-01-01"},
            POSTGRES_DIALECT,
        )


def test_an_unknown_parameter_type_is_rejected_at_construction():
    with pytest.raises(TemplateError, match="unknown parameter type"):
        ParamSpec("p_x", "jsonb")


def test_parameter_names_reads_both_forms():
    assert parameter_names(
        "{{dt.p_all}} OR x {{dt.in:p_values}} AND y = {{dt.p_one}}"
    ) == ["p_all", "p_values", "p_one"]


def test_a_template_with_no_placeholders_binds_to_an_empty_map():
    out = bind("SELECT 1", [], {}, POSTGRES_DIALECT)
    assert out == BoundQuery(sql="SELECT 1", parameters={})


def test_an_unknown_param_style_falls_back_to_pyformat():
    """A future dialect that forgets to declare one still renders something safe."""
    odd = Dialect(name="odd")
    out = bind(
        "SELECT 1 WHERE x = {{dt.p_x}}",
        [ParamSpec("p_x", "string")],
        {"p_x": "a"},
        odd,
    )
    assert out.sql == "SELECT 1 WHERE x = %(p_x)s"
