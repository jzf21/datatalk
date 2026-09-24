"""What a report template is: filters, widgets, and how they are laid out.

A template is *code*, reviewed and tested like code, and deliberately not an
LLM output. Instantiating one produces an ordinary saved dashboard -- captured
queries, filter templates, an authoring document -- so it refreshes through
exactly the same ``bind`` -> ``run_sql`` -> ``materialize()`` path as a
generated one. The only difference is who wrote the SQL.

Widget SQL uses the neutral ``{{dt.*}}`` placeholders of
:mod:`datatalk.warehouse.binding`, named ``p_<filter id>_<suffix>`` as
:func:`datatalk.dashboards.filters.param_name` builds them, so the parameter
list is inferred from the SQL instead of being declared twice.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from datatalk.warehouse.binding import parameter_names

# The suffixes each filter kind binds (see filters._coerce_*), and their types.
_SUFFIX_TYPES: dict[str, str] = {
    "all": "bool",
    "values": "string_list",
    "from": "date",
    "to": "date",
    "mode": "string",
    "n": "int",
    "ids": "string_list",
}


def infer_params(sql: str, filter_ids: set[str]) -> list[dict[str, str]]:
    """``[{"name", "type"}]`` for every placeholder in ``sql``, or ValueError.

    A placeholder that does not decompose into a known filter and suffix is a
    template bug, and raising here (at import, via :meth:`ReportTemplate.check`)
    is what keeps it out of a saved dashboard.
    """
    out: dict[str, str] = {}
    for name in parameter_names(sql):
        if not name.startswith("p_"):
            raise ValueError(f"placeholder {name!r} is not a filter parameter")
        fid, _, suffix = name[2:].rpartition("_")
        if fid not in filter_ids or suffix not in _SUFFIX_TYPES:
            raise ValueError(f"placeholder {name!r} names no filter of this template")
        out[name] = _SUFFIX_TYPES[suffix]
    return [{"name": n, "type": t} for n, t in out.items()]


@dataclass(frozen=True)
class FilterSpec:
    """One dashboard control. ``options_sql`` returns ``value[, label[, group]]``."""

    id: str
    kind: str  # date_range | dimension | sprint
    label: str
    default: dict[str, Any]
    options_sql: str | None = None
    multi: bool = True

    def definition(self) -> dict[str, Any]:
        """The persisted definition, before options are probed."""
        out: dict[str, Any] = {
            "id": self.id,
            "kind": self.kind,
            "label": self.label,
            "default": dict(self.default),
        }
        if self.kind != "date_range":
            out["multi"] = self.multi
        return out


@dataclass(frozen=True)
class WidgetSpec:
    """One query and the blocks that render it.

    ``blocks`` are authoring-block dicts whose ``dataset_id`` is left out: the
    instantiator assigns ``qN`` ids. ``filters`` are the template filters the
    SQL is written against -- the widget's wiring.
    """

    key: str
    title: str
    sql: str
    columns: tuple[str, ...]
    filters: tuple[str, ...]
    blocks: tuple[dict[str, Any], ...]
    description: str = ""


@dataclass(frozen=True)
class Section:
    """A heading (optional) over one grid row of widgets, in order."""

    heading: str | None
    widgets: tuple[str, ...]


@dataclass(frozen=True)
class ReportTemplate:
    id: str
    version: int
    title: str
    description: str
    family: str  # sprint | flow | backlog | people
    source_types: frozenset[str]
    filters: tuple[FilterSpec, ...]
    widgets: tuple[WidgetSpec, ...]
    layout: tuple[Section, ...]
    # Widgets not placed by default but offered in the editor's "Add widget".
    extra_widgets: tuple[WidgetSpec, ...] = field(default_factory=tuple)
    # For a synced source: the minimum ``_sync_meta.schema_version`` whose
    # tables this template's SQL reads. A store synced before an upgrade has
    # not been rebuilt yet, and its queries would fail on missing tables.
    min_store_version: int | None = None

    def widget(self, key: str) -> WidgetSpec | None:
        return next((w for w in self.all_widgets() if w.key == key), None)

    def all_widgets(self) -> tuple[WidgetSpec, ...]:
        return self.widgets + self.extra_widgets

    def check(self) -> "ReportTemplate":
        """Fail loudly on a template that could not instantiate correctly."""
        fids = {f.id for f in self.filters}
        keys = [w.key for w in self.all_widgets()]
        if len(keys) != len(set(keys)):
            raise ValueError(f"{self.id}: duplicate widget keys")
        placed = [k for s in self.layout for k in s.widgets]
        unknown = set(placed) - {w.key for w in self.widgets}
        if unknown:
            raise ValueError(f"{self.id}: layout names unknown widgets {sorted(unknown)}")
        for w in self.all_widgets():
            if set(w.filters) - fids:
                raise ValueError(f"{self.id}.{w.key}: wired to unknown filters")
            infer_params(w.sql, set(w.filters))
            for block in w.blocks:
                for col in _block_columns(block):
                    if col not in w.columns:
                        raise ValueError(f"{self.id}.{w.key}: block reads unknown column {col!r}")
        return self

    def summary(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "version": self.version,
            "title": self.title,
            "description": self.description,
            "family": self.family,
            "filters": [f.label for f in self.filters],
            "widgets": [w.title for w in self.widgets],
        }


def _block_columns(block: dict[str, Any]) -> list[str]:
    cols: list[str] = []
    for key in ("x_col", "value_col", "delta_col"):
        if block.get(key):
            cols.append(block[key])
    cols += list(block.get("series_cols") or [])
    cols += list(block.get("columns") or [])
    return cols
