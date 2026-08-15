"""Neutral SQL templates, rendered to one engine's parameter syntax.

A dashboard filter has to reach *inside* a captured query -- into the ``WHERE``
that runs before the aggregation and before the ``LIMIT`` -- because filtering a
query's output cannot touch a single-value stat tile, whose output has no date
column at all. So a captured query is rewritten once into a *template* carrying
named placeholders, and every refresh binds values into it.

The values are bound by the driver and never enter the statement text. That is
not a stylistic preference. The two engines disagree about escaping inside string
literals -- Postgres (with ``standard_conforming_strings=on``) treats a backslash
as an ordinary character, ClickHouse treats it as an escape -- so a single
hand-written quoting function cannot be correct for both, and the one written
against Postgres semantics is exploitable on ClickHouse. Binding sidesteps the
entire class of bug.

Templates are stored engine-neutral::

    WHERE o.created_at >= {{dt.p_from}} AND o.created_at < {{dt.p_to}}
      AND ({{dt.p_region_all}} OR o.region {{dt.in:p_region}})

so :mod:`datatalk.llm.prompts` stays dialect-free (no prompt in this codebase
names an engine) and re-pointing a dashboard at the other engine keeps working.
The membership form ``{{dt.in:name}}`` renders the *whole predicate operator*,
not just the placeholder, because the engines differ in shape and not merely in
spelling: ClickHouse wants ``IN {p:Array(String)}`` while psycopg 3 wants
``= ANY(%(p)s)``. Keeping that divergence here is the point of the module.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from datatalk.warehouse.base import Dialect

__all__ = [
    "PARAM_TYPES",
    "BoundQuery",
    "ParamSpec",
    "TemplateError",
    "bind",
    "parameter_names",
]

# The types a filter parameter may declare. Deliberately small: every one maps
# to a Python type that cannot carry SQL, which is the first of the three layers
# protecting the filter path (coerce -> allowlist -> bind).
PARAM_TYPES = frozenset(
    {"date", "datetime", "string", "int", "float", "bool", "string_list"}
)

# ClickHouse names the type inside the placeholder itself.
_CH_TYPE = {
    "date": "Date",
    "datetime": "DateTime64(3, 'UTC')",
    "string": "String",
    "int": "Int64",
    "float": "Float64",
    "bool": "Bool",
    "string_list": "Array(String)",
}

# {{dt.name}} or {{dt.in:name}}. The `dt.` prefix keeps the marker from colliding
# with anything an engine or an author might legitimately write in a query.
_PARAM_RE = re.compile(r"\{\{dt\.(in:)?([a-z][a-z0-9_]{0,47})\}\}")


class TemplateError(ValueError):
    """A stored template is malformed, or disagrees with its parameter list."""


@dataclass(frozen=True)
class ParamSpec:
    """One declared parameter of a template."""

    name: str
    type: str

    def __post_init__(self) -> None:
        if self.type not in PARAM_TYPES:
            raise TemplateError(
                f"unknown parameter type '{self.type}' for '{self.name}'; "
                f"expected one of {sorted(PARAM_TYPES)}"
            )


@dataclass(frozen=True)
class BoundQuery:
    """Engine-ready SQL plus the parameter map the driver will bind."""

    sql: str
    parameters: dict[str, Any] | None


def parameter_names(template: str) -> list[str]:
    """Every placeholder name referenced by ``template``, in order of appearance."""
    return [m.group(2) for m in _PARAM_RE.finditer(template)]


def bind(
    template: str,
    params: Sequence[ParamSpec],
    values: Mapping[str, Any],
    dialect: Dialect,
) -> BoundQuery:
    """Render a neutral template for ``dialect`` and collect its parameter map.

    ``values`` must already have been coerced and allowlist-checked by
    :mod:`datatalk.dashboards.filters`; this function binds, it does not decide
    what is acceptable. It never writes a value into the returned SQL -- every
    value leaves through :attr:`BoundQuery.parameters`.

    Raises :class:`TemplateError` if the template references a parameter that was
    not declared, or if a declared parameter has no value.
    """
    spec = {p.name: p.type for p in params}

    undeclared = sorted(set(parameter_names(template)) - set(spec))
    if undeclared:
        # Refusing here is what stops a hand-edited or model-authored template
        # from reaching the driver with an unbound placeholder, which on
        # ClickHouse would be sent as a literal `{p:String}` string.
        raise TemplateError(
            f"template references undeclared parameter(s): {undeclared}"
        )

    missing = sorted(n for n in spec if n not in values)
    if missing:
        raise TemplateError(f"no value supplied for parameter(s): {missing}")

    text = template
    if dialect.param_style == "pyformat":
        # ORDER MATTERS. Once parameters are supplied, psycopg scans the whole
        # statement for `%`, so an author-written `LIKE '%eu%'` raises before it
        # ever reaches the server. Double the literal percents first, then insert
        # the placeholders -- doing it the other way round would escape the
        # placeholders we just wrote.
        text = text.replace("%", "%%")

    def render(match: re.Match[str]) -> str:
        membership = bool(match.group(1))
        name = match.group(2)
        kind = spec[name]
        if dialect.param_style == "curly":
            token = "{%s:%s}" % (name, _CH_TYPE[kind])
            return f"IN {token}" if membership else token
        # psycopg 3: `IN %(p)s` is not the idiom; `= ANY(%(p)s)` is, and it takes
        # a Python list directly.
        return f"= ANY(%({name})s)" if membership else f"%({name})s"

    sql = _PARAM_RE.sub(render, text)
    return BoundQuery(sql=sql, parameters={p.name: values[p.name] for p in params})
