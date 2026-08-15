"""Rewrite a captured query into a filterable template. Configuration only.

This is the single place a model is involved in dashboard filters, and it runs
when someone configures them -- never during a refresh. The reason a model is
needed at all is that a filter has to reach *inside* an already-aggregated query:
a stat tile's SQL is ``SELECT sum(amount) FROM orders``, whose output has one row
and no date column, so no amount of wrapping can restrict it to a quarter. Only
an added ``WHERE`` predicate can, and placing one correctly means understanding
the query.

Two things keep that acceptable in a codebase whose central rule is that the LLM
never types a number:

* **The model rewrites SQL, not values.** Every filter value is bound by the
  driver at refresh time (:mod:`datatalk.warehouse.binding`); the template is
  static text.
* **The rewrite is verified by execution, not by reading.** The caller runs the
  template once and compares its result columns against the captured query's.
  Blocks reference columns *by name*, so a rewrite that renames or drops one
  would turn every widget on that dataset into a degradation note. A template
  that fails the check is discarded and the dataset stays unfiltered.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from datatalk import observability as obs
from datatalk.agent.blocks import parse_json_object
from datatalk.llm.prompts import TEMPLATIZE_SYSTEM
from datatalk.warehouse.binding import PARAM_TYPES, parameter_names

if TYPE_CHECKING:
    from datatalk.context import TenantContext

__all__ = ["TemplateProposal", "propose_template"]

_MAX_SQL_CHARS = 8_000


class TemplateProposal(dict):
    """``{"sql": ..., "params": [{"name", "type"}], "filters": [...]}``."""


def _filter_brief(defn: dict[str, Any]) -> dict[str, Any]:
    """What the model needs to know about one filter, and nothing more."""
    fid = defn["id"]
    if defn.get("kind") == "date_range":
        return {
            "id": fid,
            "kind": "date_range",
            "label": defn.get("label") or fid,
            "params": {
                "all": f"p_{fid}_all",
                "from": f"p_{fid}_from",
                "to": f"p_{fid}_to",
            },
            "shape": (
                f"({{{{dt.p_{fid}_all}}}} OR (<timestamp column> >= "
                f"{{{{dt.p_{fid}_from}}}} AND <timestamp column> < "
                f"{{{{dt.p_{fid}_to}}}}))"
            ),
        }
    return {
        "id": fid,
        "kind": "dimension",
        "label": defn.get("label") or fid,
        "column": defn.get("column"),
        "params": {"all": f"p_{fid}_all", "values": f"p_{fid}_values"},
        "shape": (
            f"({{{{dt.p_{fid}_all}}}} OR <column> {{{{dt.in:p_{fid}_values}}}})"
        ),
    }


def _expected_params(defs: list[dict[str, Any]]) -> dict[str, str]:
    """Every placeholder a template may legally use, with its declared type."""
    out: dict[str, str] = {}
    for defn in defs:
        fid = defn["id"]
        if defn.get("kind") == "date_range":
            out[f"p_{fid}_all"] = "bool"
            out[f"p_{fid}_from"] = "date"
            out[f"p_{fid}_to"] = "date"
        else:
            out[f"p_{fid}_all"] = "bool"
            out[f"p_{fid}_values"] = "string_list"
    return out


def propose_template(
    query: dict[str, Any],
    filter_defs: list[dict[str, Any]],
    *,
    ctx: "TenantContext",
    dialect_hint: str = "",
) -> TemplateProposal | None:
    """Ask the model to add filter predicates to one captured query.

    Returns ``None`` when the model declines, replies unusably, or proposes
    placeholders outside the declared set -- all of which mean "this dataset
    stays unfiltered", never an exception. The caller still has to verify the
    proposal by executing it.
    """
    sql = (query.get("sql") or "").strip()
    if not sql or not filter_defs:
        return None

    allowed = _expected_params(filter_defs)
    user_content = json.dumps(
        {
            "sql": sql[:_MAX_SQL_CHARS],
            "columns": query.get("columns") or [],
            "engine_hint": dialect_hint,
            "filters": [_filter_brief(d) for d in filter_defs],
            "allowed_placeholders": allowed,
        },
        default=str,
    )

    with obs.agent_run(
        "templatize-query",
        ctx,
        feature="dashboard-filters",
        input={"sql": sql, "filters": [d.get("id") for d in filter_defs]},
        metadata={"dataset_id": query.get("dataset_id")},
    ) as root:
        resp = ctx.author_openai.chat.completions.create(
            model=ctx.author_model,
            temperature=0,
            messages=[
                {"role": "system", "content": TEMPLATIZE_SYSTEM},
                {"role": "user", "content": user_content},
            ],
            **obs.llm_kwargs("templatize-query"),
        )
        raw = resp.choices[0].message.content or ""
        data = parse_json_object(raw)
        proposal = _validate(data, allowed)
        root.update(
            output={"template": (proposal or {}).get("sql", "")},
            metadata={"accepted": proposal is not None},
        )
        return proposal


def _validate(
    data: dict[str, Any] | None, allowed: dict[str, str]
) -> TemplateProposal | None:
    """Structural checks the caller's execution test cannot make for it."""
    if not isinstance(data, dict):
        return None
    sql = data.get("sql")
    if not isinstance(sql, str) or not sql.strip():
        return None

    used = set(parameter_names(sql))
    if not used:
        # A template that binds nothing is not a template; the dataset is simply
        # not filterable and should keep its original SQL.
        return None
    if used - set(allowed):
        # An invented placeholder would fail at bind time on every refresh; it is
        # cheaper and clearer to reject the whole proposal now.
        return None

    params = [
        {"name": name, "type": allowed[name]}
        for name in sorted(used)
        if allowed[name] in PARAM_TYPES
    ]
    if len(params) != len(used):
        return None

    applied = [f for f in (data.get("filters") or []) if isinstance(f, str)]
    return TemplateProposal(sql=sql, params=params, filters=applied)
