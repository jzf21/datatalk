"""Filter definitions, value coercion, and the bindings a refresh executes.

Three independent layers protect this path, and they are ordered so that each
one would still hold if the next were refactored wrong:

1. **Coerce to a Python type.** A ``date`` object cannot carry SQL. A value that
   will not parse is rejected here and never reaches a query.
2. **Check against the persisted option allowlist.** A dimension value must be
   one the configuration probe actually found. This is the layer that holds even
   if binding were later replaced with something weaker.
3. **Bind through the driver** (:mod:`datatalk.warehouse.binding`), so the value
   is not part of the statement text at all.

The filter definitions themselves are written by the configuration pass and
stored as JSONB; this module is the only thing that reads them, so their shape is
documented here rather than in a schema file:

    {
      "version": 1,
      "filters": [
        {"id": "range", "kind": "date_range", "label": "Period",
         "default": {"preset": "last_30_days"}},
        {"id": "region", "kind": "dimension", "label": "Region",
         "column": "region", "source": "prod_pg", "multi": true,
         "options": ["EMEA", "APAC"], "options_truncated": false,
         "option_labels": {"EMEA": "Europe"},          # optional, display only
         "default": {"all": true}},
        {"id": "sprint", "kind": "sprint", "label": "Sprint", "multi": false,
         "options": ["41", "42"], "option_labels": {"42": "Sprint 42"},
         "option_groups": {"42": "active"},             # optional, display only
         "default": {"mode": "active"}}
      ],
      "templates": {
        "q1": {"sql": "... {{dt.p_range_from}} ...",
               "params": [{"name": "p_range_from", "type": "date"}, ...],
               "columns": ["month", "revenue"],
               "filters": ["range", "region"],
               "overrides": {"region": {"values": ["EMEA"]}}}   # optional
      }
    }

A dataset's ``filters`` list is its *wiring*: a dashboard filter it does not
list is bound as "all" for that dataset (and reported in ``unwired``), and an
``overrides`` entry pins that filter's selection for that dataset alone. Both go
through the same coercion and allowlist as a viewer's selection.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any

from datatalk.warehouse.base import Dialect
from datatalk.warehouse.binding import BoundQuery, ParamSpec, TemplateError, bind

__all__ = [
    "DATE_PRESETS",
    "SPRINT_MODES",
    "MAX_DIMENSION_VALUES",
    "FilterError",
    "build_bindings",
    "coerce_values",
    "param_name",
]

# Presets are resolved *here*, against the server's clock. A client-computed
# window is never trusted: it is the one filter input that would otherwise let a
# caller name any range at all regardless of what the dashboard offers.
DATE_PRESETS: dict[str, int | None] = {
    "last_24_hours": 1,
    "last_7_days": 7,
    "last_30_days": 30,
    "last_90_days": 90,
    "last_12_months": 365,
    "all_time": None,
}

MAX_DIMENSION_VALUES = 100
MAX_VALUE_CHARS = 200

# A sprint filter picks sprints by rule, resolved in SQL against the synced
# sprints table, so "active" keeps meaning the active sprint as sprints roll.
SPRINT_MODES = frozenset({"active", "last_n", "ids", "all"})
MAX_LAST_N_SPRINTS = 26


class FilterError(ValueError):
    """A filter selection is unknown or unacceptable.

    ``code`` is the stable machine code the endpoint puts in ``detail``.
    """

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


def param_name(filter_id: str, suffix: str) -> str:
    """The placeholder a filter binds to, e.g. ``p_range_from``."""
    return f"p_{filter_id}_{suffix}"


# --- coercion ----------------------------------------------------------------


def _as_date(value: Any, field: str) -> date:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if not isinstance(value, str):
        raise FilterError("filter_value_invalid", f"{field} must be a date")
    try:
        # fromisoformat accepts "2026-01-01" and full timestamps; anything with
        # SQL in it fails here, which is the point.
        return datetime.fromisoformat(value).date()
    except ValueError:
        raise FilterError(
            "filter_value_invalid", f"{field} is not an ISO date: {value!r}"
        ) from None


def _coerce_date_range(
    defn: dict[str, Any], selection: dict[str, Any], now: datetime
) -> dict[str, Any]:
    fid = defn["id"]
    preset = selection.get("preset")

    if preset and preset != "custom":
        if preset not in DATE_PRESETS:
            raise FilterError("filter_value_invalid", f"unknown preset {preset!r}")
        days = DATE_PRESETS[preset]
        if days is None:  # all time -- no lower bound
            return {
                param_name(fid, "all"): True,
                param_name(fid, "from"): now.date(),
                param_name(fid, "to"): now.date() + timedelta(days=1),
            }
        return {
            param_name(fid, "all"): False,
            param_name(fid, "from"): (now - timedelta(days=days)).date(),
            param_name(fid, "to"): now.date() + timedelta(days=1),
        }

    raw_from = selection.get("from")
    raw_to = selection.get("to")
    if not raw_from and not raw_to:
        # Nothing chosen: behave as "all time" rather than inventing a window.
        return {
            param_name(fid, "all"): True,
            param_name(fid, "from"): now.date(),
            param_name(fid, "to"): now.date() + timedelta(days=1),
        }

    start = _as_date(raw_from, f"{fid}.from") if raw_from else now.date()
    end = _as_date(raw_to, f"{fid}.to") if raw_to else now.date()
    if start > end:
        start, end = end, start
    return {
        param_name(fid, "all"): False,
        param_name(fid, "from"): start,
        # Exclusive upper bound, so a single-day range includes that whole day
        # whatever the column's time component is.
        param_name(fid, "to"): end + timedelta(days=1),
    }


def _coerce_dimension(
    defn: dict[str, Any], selection: dict[str, Any]
) -> dict[str, Any]:
    fid = defn["id"]
    if selection.get("all"):
        # "All" is a flag, never the expanded option list: binding every option
        # would silently exclude anything the probe truncated, and anything that
        # appeared in the warehouse since it ran.
        return {
            param_name(fid, "all"): True,
            # psycopg cannot infer the element type of an empty list, and the
            # predicate short-circuits on the flag above, so this sentinel is
            # never actually compared.
            param_name(fid, "values"): [""],
        }

    raw = selection.get("values") or []
    if not isinstance(raw, list):
        raise FilterError("filter_value_invalid", f"{fid}.values must be a list")
    if len(raw) > MAX_DIMENSION_VALUES:
        raise FilterError(
            "filter_value_invalid",
            f"{fid}: at most {MAX_DIMENSION_VALUES} values may be selected",
        )

    options = defn.get("options")
    allowed = set(options) if isinstance(options, list) else None
    values: list[str] = []
    for one in raw:
        if not isinstance(one, str) or len(one) > MAX_VALUE_CHARS:
            raise FilterError("filter_value_invalid", f"{fid}: bad value")
        if allowed is not None and one not in allowed:
            # The allowlist layer. A value the probe never saw cannot be
            # selected, whatever it contains.
            raise FilterError(
                "filter_value_invalid", f"{fid}: {one!r} is not an available option"
            )
        values.append(one)

    if not values:
        return {param_name(fid, "all"): True, param_name(fid, "values"): [""]}
    return {param_name(fid, "all"): False, param_name(fid, "values"): values}


def _coerce_sprint(defn: dict[str, Any], selection: dict[str, Any]) -> dict[str, Any]:
    fid = defn["id"]
    single = defn.get("multi") is False
    mode = selection.get("mode") or "active"
    if mode not in SPRINT_MODES or (single and mode == "all"):
        raise FilterError("filter_value_invalid", f"{fid}: unknown sprint mode {mode!r}")

    n = 1
    if mode == "last_n":
        raw_n = selection.get("n", 1)
        if isinstance(raw_n, bool) or not isinstance(raw_n, int):
            raise FilterError("filter_value_invalid", f"{fid}.n must be an integer")
        if not 1 <= raw_n <= MAX_LAST_N_SPRINTS:
            raise FilterError(
                "filter_value_invalid", f"{fid}.n must be 1..{MAX_LAST_N_SPRINTS}"
            )
        # A single-sprint report shows "the last completed sprint", not six.
        n = 1 if single else raw_n

    ids: list[str] = [""]  # the same never-compared sentinel as a dimension's
    if mode == "ids":
        raw = selection.get("ids") or []
        if not isinstance(raw, list) or not raw:
            raise FilterError("filter_value_invalid", f"{fid}.ids must be a non-empty list")
        if len(raw) > (1 if single else MAX_DIMENSION_VALUES):
            raise FilterError("filter_value_invalid", f"{fid}: too many sprints selected")
        allowed = set(defn.get("options") or [])
        for one in raw:
            if not isinstance(one, str) or one not in allowed:
                raise FilterError(
                    "filter_value_invalid", f"{fid}: {one!r} is not an available sprint"
                )
        ids = list(raw)

    return {
        param_name(fid, "mode"): mode,
        param_name(fid, "n"): n,
        param_name(fid, "ids"): ids,
    }


def _coerce_one(
    defn: dict[str, Any], selection: dict[str, Any], now: datetime
) -> dict[str, Any]:
    kind = defn.get("kind")
    if kind == "date_range":
        return _coerce_date_range(defn, selection, now)
    if kind == "dimension":
        return _coerce_dimension(defn, selection)
    if kind == "sprint":
        return _coerce_sprint(defn, selection)
    raise FilterError("filter_value_invalid", f"{defn.get('id')}: unknown filter kind")


def _all_selection(defn: dict[str, Any]) -> dict[str, Any]:
    """What "this filter does not apply here" means for each kind."""
    if defn.get("kind") == "date_range":
        return {"preset": "all_time"}
    if defn.get("kind") == "sprint":
        # A single-sprint widget cannot be "all sprints"; unwiring it keeps it
        # on the active sprint, which is what the widget was built around.
        return {"mode": "active"} if defn.get("multi") is False else {"mode": "all"}
    return {"all": True}


def coerce_values(
    filters: dict[str, Any],
    selections: dict[str, Any],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Turn client selections into a flat, typed parameter map.

    Every definition contributes its parameters, whether or not the client named
    it -- an unmentioned filter falls back to its stored default, so a template
    never has an unbound placeholder.
    """
    now = now or datetime.now(timezone.utc)
    defs = filters.get("filters") or []
    by_id = {d["id"]: d for d in defs if isinstance(d, dict) and d.get("id")}

    unknown = sorted(set(selections) - set(by_id))
    if unknown:
        raise FilterError(
            "filter_unknown", f"no such filter(s) on this dashboard: {unknown}"
        )

    out: dict[str, Any] = {}
    for fid, defn in by_id.items():
        selection = selections.get(fid)
        if not isinstance(selection, dict):
            selection = defn.get("default") or {}
        out.update(_coerce_one(defn, selection, now))
    return out


def dataset_values(
    filters: dict[str, Any],
    template: dict[str, Any],
    values: dict[str, Any],
    *,
    now: datetime | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """``values`` adjusted for one dataset's wiring and overrides.

    Returns ``(values, unwired filter ids)``. A template without a ``filters``
    list is wired to everything, which is how templates written before wiring
    was editable keep their behaviour.
    """
    now = now or datetime.now(timezone.utc)
    by_id = {
        d["id"]: d for d in filters.get("filters") or [] if isinstance(d, dict) and d.get("id")
    }
    wired = template.get("filters")
    overrides = template.get("overrides") or {}
    if wired is None and not overrides:
        return values, []

    out = dict(values)
    unwired: list[str] = []
    for fid, defn in by_id.items():
        if fid in overrides:
            if not isinstance(overrides[fid], dict):
                raise FilterError("filter_value_invalid", f"{fid}: bad override")
            out.update(_coerce_one(defn, overrides[fid], now))
        elif wired is not None and fid not in wired:
            out.update(_coerce_one(defn, _all_selection(defn), now))
            unwired.append(fid)
    return out, unwired


# --- bindings ----------------------------------------------------------------


@dataclass
class BindingSet:
    """Per-dataset bound SQL, plus the datasets no filter could reach."""

    bound: dict[str, BoundQuery]
    unfiltered: list[str]
    # dataset id -> dashboard filters it is deliberately not wired to.
    unwired: dict[str, list[str]] = field(default_factory=dict)


def build_bindings(
    filters: dict[str, Any],
    selections: dict[str, Any],
    dialects: dict[str, Dialect],
    *,
    now: datetime | None = None,
) -> BindingSet:
    """Render every filterable dataset's template against ``selections``.

    ``dialects`` maps ``dataset_id`` to the dialect of the source that dataset
    runs on, because the same neutral template renders differently per engine.

    A dataset whose template is missing or malformed is reported in
    ``unfiltered`` rather than raising: it refreshes with its original SQL, which
    is stale with respect to the filter but correct with respect to itself, and
    the UI can say so.
    """
    templates = filters.get("templates") or {}
    if not templates:
        return BindingSet(bound={}, unfiltered=[])

    now = now or datetime.now(timezone.utc)
    values = coerce_values(filters, selections, now=now)

    bound: dict[str, BoundQuery] = {}
    unfiltered: list[str] = []
    unwired: dict[str, list[str]] = {}
    for dataset_id, template in templates.items():
        dialect = dialects.get(dataset_id)
        if not isinstance(template, dict) or dialect is None:
            unfiltered.append(dataset_id)
            continue
        try:
            # Wiring and overrides are the dashboard author's, stored beside the
            # template; a bad one is a stored-state problem, so it degrades this
            # dataset like a malformed template rather than 400ing the viewer.
            ds_values, ds_unwired = dataset_values(filters, template, values, now=now)
            params = [
                ParamSpec(name=p["name"], type=p["type"])
                for p in template.get("params") or []
            ]
            bound[dataset_id] = bind(
                template.get("sql") or "", params, ds_values, dialect
            )
            if ds_unwired:
                unwired[dataset_id] = ds_unwired
        except (TemplateError, FilterError, KeyError, TypeError):
            # A template that disagrees with its own parameter list, or with a
            # definition edited since. Never a 500, and never a query built from
            # a half-rendered template.
            unfiltered.append(dataset_id)

    return BindingSet(bound=bound, unfiltered=unfiltered, unwired=unwired)
