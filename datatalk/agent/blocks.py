"""Dataset-referencing block document model (multiagent reporting).

The key trust decision: **the LLM never re-types numbers.** The Analyst captures
query result sets, each addressable by a ``dataset_id``. The Reporter and Q&A
agent emit *authoring* blocks that reference a dataset plus column mappings;
:func:`materialize` resolves those references into concrete values before the
Document is stored or sent to the UI.

A :class:`Document` is an ordered list of typed blocks. Three block families
carry data — ``table``, ``chart``, and ``stat`` — and each has an *authoring*
form (references a dataset by id) and a *materialized* form (holds concrete
values). ``row`` is a layout container (not itself data-carrying);
:func:`materialize` recurses into its children. A malformed reference degrades
gracefully to a paragraph note; it never raises.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, replace
from typing import Any

CHART_TYPES = {"bar", "line", "area", "pie"}


# --- block dataclasses -------------------------------------------------------

@dataclass
class Heading:
    text: str
    level: int = 2
    width: int | None = None
    type: str = "heading"


@dataclass
class Paragraph:
    text: str
    width: int | None = None
    type: str = "paragraph"


@dataclass
class Table:
    # authoring form
    dataset_id: str | None = None
    columns: list[str] | None = None  # None = all columns of the dataset
    # materialized form (columns is reused; rows is populated)
    rows: list[list[Any]] | None = None
    width: int | None = None
    type: str = "table"


@dataclass
class Chart:
    chart_type: str = "bar"
    title: str = ""
    # authoring form
    dataset_id: str | None = None
    x_col: str | None = None
    series_cols: list[str] | None = None
    # materialized form
    x: dict[str, Any] | None = None  # {label, values}
    series: list[dict[str, Any]] | None = None  # [{name, values}]
    width: int | None = None
    type: str = "chart"


@dataclass
class Stat:
    # authoring form
    label: str = ""
    dataset_id: str | None = None
    value_col: str | None = None
    row_index: int | None = None  # None = last row (aggregates are single-row)
    delta_col: str | None = None  # a prior-period value column in the same dataset
    unit: str | None = None       # e.g. "%", "$"
    # materialized form
    value: Any = None
    delta: Any = None
    delta_pct: float | None = None
    width: int | None = None
    type: str = "stat"


@dataclass
class Row:
    # a horizontal grid row; children carry their own width (1-12). Not a data
    # block itself — materialize() recurses into its children.
    children: list[Any] = field(default_factory=list)
    width: int | None = None
    type: str = "row"


_BLOCK_CLASSES = {
    "heading": Heading,
    "paragraph": Paragraph,
    "table": Table,
    "chart": Chart,
    "stat": Stat,
    "row": Row,
}


# --- (de)serialization -------------------------------------------------------

def block_to_dict(block: Any) -> dict[str, Any]:
    """Serialize a block, dropping keys whose value is ``None``.

    A ``Row`` serializes its children recursively (so each child keeps the
    drop-None convention); a plain ``asdict`` would keep every ``None`` field.
    """
    if isinstance(block, Row):
        out: dict[str, Any] = {"type": "row"}
        if block.width is not None:
            out["width"] = block.width
        out["children"] = [block_to_dict(c) for c in block.children]
        return out
    return {k: v for k, v in asdict(block).items() if v is not None}


def block_from_dict(data: dict[str, Any]) -> Any:
    """Reconstruct a block from a dict; unknown keys are ignored."""
    kind = data.get("type")
    if kind == "row":
        return Row(
            children=[block_from_dict(c) for c in data.get("children", []) if isinstance(c, dict)],
            width=data.get("width"),
        )
    cls = _BLOCK_CLASSES.get(kind)
    if cls is None:
        # Unknown block type degrades to a paragraph rather than raising.
        return Paragraph(text=str(data.get("text", "")))
    fields = cls.__dataclass_fields__
    kwargs = {k: v for k, v in data.items() if k in fields and k != "type"}
    return cls(**kwargs)


@dataclass
class Document:
    blocks: list[Any] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"blocks": [block_to_dict(b) for b in self.blocks]}

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "Document":
        data = data or {}
        return cls(blocks=[block_from_dict(b) for b in data.get("blocks", [])])

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), default=str)


# --- materialization ---------------------------------------------------------

def _note(text: str) -> Paragraph:
    """A graceful-degradation paragraph note (italic)."""
    return Paragraph(text=f"_{text}_")


def _materialize_table(block: Table, datasets: dict[str, Any]) -> Any:
    ds = datasets.get(block.dataset_id)
    if ds is None:
        return _note(f"table unavailable: unknown dataset '{block.dataset_id}'")
    cols = block.columns or list(ds.columns)
    missing = [c for c in cols if c not in ds.columns]
    if missing:
        return _note(f"table unavailable: unknown column(s) {', '.join(missing)}")
    idx = [ds.columns.index(c) for c in cols]
    rows = [[row[i] for i in idx] for row in ds.rows]
    # dataset_id survives materialization so the UI can cite the query behind
    # every number. It is not what makes a block "authoring" -- ``rows is None``
    # is (see ``_is_authoring_table``).
    return Table(dataset_id=block.dataset_id, columns=list(cols), rows=rows)


def _materialize_chart(block: Chart, datasets: dict[str, Any]) -> Any:
    ds = datasets.get(block.dataset_id)
    if ds is None:
        return _note(f"chart unavailable: unknown dataset '{block.dataset_id}'")
    if block.chart_type not in CHART_TYPES:
        return _note(f"chart unavailable: unknown chart type '{block.chart_type}'")
    if not block.x_col or not block.series_cols:
        return _note("chart unavailable: missing x_col or series_cols")
    needed = [block.x_col, *block.series_cols]
    missing = [c for c in needed if c not in ds.columns]
    if missing:
        return _note(f"chart unavailable: unknown column(s) {', '.join(missing)}")
    xi = ds.columns.index(block.x_col)
    x_values = [row[xi] for row in ds.rows]
    series = []
    for name in block.series_cols:
        si = ds.columns.index(name)
        series.append({"name": name, "values": [row[si] for row in ds.rows]})
    return Chart(
        chart_type=block.chart_type,
        title=block.title,
        dataset_id=block.dataset_id,
        x={"label": block.x_col, "values": x_values},
        series=series,
    )


def _is_authoring_table(b: Any) -> bool:
    return isinstance(b, Table) and b.rows is None


def _is_authoring_chart(b: Any) -> bool:
    return isinstance(b, Chart) and b.x is None


def _is_authoring_stat(b: Any) -> bool:
    return isinstance(b, Stat) and b.value is None


def _compute_delta(value: Any, prior: Any) -> tuple[Any, float | None]:
    """(delta, delta_pct) from current + prior; (None, None) if non-numeric."""
    try:
        v = float(value)
        p = float(prior)
    except (TypeError, ValueError):
        return None, None
    delta = v - p
    delta_pct = (delta / p * 100.0) if p != 0 else None
    return delta, delta_pct


def _materialize_stat(block: Stat, datasets: dict[str, Any]) -> Any:
    ds = datasets.get(block.dataset_id)
    if ds is None:
        return _note(f"stat unavailable: unknown dataset '{block.dataset_id}'")
    if not block.value_col or block.value_col not in ds.columns:
        return _note(f"stat unavailable: unknown column '{block.value_col}'")
    if not ds.rows:
        return _note(f"stat unavailable: dataset '{block.dataset_id}' has no rows")
    ri = block.row_index if block.row_index is not None else len(ds.rows) - 1
    if ri < 0 or ri >= len(ds.rows):
        return _note(f"stat unavailable: row_index {ri} out of range")
    value = ds.rows[ri][ds.columns.index(block.value_col)]
    delta = delta_pct = None
    if block.delta_col:
        if block.delta_col not in ds.columns:
            return _note(f"stat unavailable: unknown delta column '{block.delta_col}'")
        prior = ds.rows[ri][ds.columns.index(block.delta_col)]
        delta, delta_pct = _compute_delta(value, prior)
    return Stat(
        label=block.label, unit=block.unit, width=_clamp_width(block.width),
        dataset_id=block.dataset_id,
        value=value, delta=delta, delta_pct=delta_pct,
    )


def _clamp_width(width: int | None) -> int | None:
    if width is None:
        return None
    return max(1, min(12, width))


def _materialize_block(b: Any, datasets: dict[str, Any]) -> Any:
    if isinstance(b, Row):
        children = []
        for c in b.children:
            mat = _materialize_block(c, datasets)
            w = _clamp_width(getattr(c, "width", None))
            if w is not None and hasattr(mat, "width"):
                mat = replace(mat, width=w)
            children.append(mat)
        return Row(children=children, width=b.width)
    if _is_authoring_table(b):
        return _materialize_table(b, datasets)
    if _is_authoring_chart(b):
        return _materialize_chart(b, datasets)
    if _is_authoring_stat(b):
        return _materialize_stat(b, datasets)
    return b


def materialize(doc: Document, datasets: dict[str, Any]) -> Document:
    """Resolve every authoring table/chart/stat against ``datasets``.

    ``datasets`` maps ``dataset_id`` -> an object exposing ``.columns`` and
    ``.rows`` (e.g. an :class:`~datatalk.agent.executor.QueryResult`). A missing
    dataset id, unknown column, or out-of-range row degrades that block to a
    paragraph note rather than raising, so one bad reference never aborts a
    whole dashboard. ``Row`` blocks recurse into their children.
    """
    return Document(blocks=[_materialize_block(b, datasets) for b in doc.blocks])


# --- flattening --------------------------------------------------------------

def _fmt(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def _flatten_block(b: Any, parts: list[str]) -> None:
    if isinstance(b, Row):
        for c in b.children:
            _flatten_block(c, parts)
        return
    if isinstance(b, Heading):
        level = max(1, min(6, b.level))
        parts.append(f"{'#' * level} {b.text}")
    elif isinstance(b, Stat):
        line = f"{b.label}: {_fmt(b.value)}{b.unit or ''}"
        if b.delta is not None:
            line += f" (Δ {_fmt(b.delta)})"
        parts.append(line)
    elif isinstance(b, Paragraph):
        parts.append(b.text)
    elif isinstance(b, Table):
        if b.columns:
            parts.append(" | ".join(_fmt(c) for c in b.columns))
            parts.append(" | ".join("---" for _ in b.columns))
            for row in b.rows or []:
                parts.append(" | ".join(_fmt(v) for v in row))
    elif isinstance(b, Chart):
        parts.append(f"{b.title or 'Chart'} ({b.chart_type})")
        if b.x and b.series:
            labels = b.x.get("values", [])
            for s in b.series:
                pairs = ", ".join(
                    f"{_fmt(x)}={_fmt(v)}"
                    for x, v in zip(labels, s.get("values", []))
                )
                parts.append(f"{s.get('name', '')}: {pairs}")
    parts.append("")  # blank separator line


def document_to_text(doc: Document) -> str:
    """Flatten a materialized Document to plain text.

    Used by the Analyze agents and for memory embedding, which both expect a
    single narrative string. Tables become pipe rows; charts become a titled
    value summary; stats become ``label: value (Δ delta)``; a row flattens by
    flattening its children.
    """
    parts: list[str] = []
    for b in doc.blocks:
        _flatten_block(b, parts)
    return "\n".join(parts).strip()


# --- JSON helper -------------------------------------------------------------

def parse_json_object(text: str) -> dict[str, Any]:
    """Parse a JSON object from an LLM response, tolerating code fences.

    Returns ``{}`` if no object can be recovered.
    """
    if not text:
        return {}
    s = text.strip()
    # Strip ``` or ```json fences if present.
    if s.startswith("```"):
        s = s.split("\n", 1)[-1]
        if s.rstrip().endswith("```"):
            s = s.rstrip()[:-3]
    s = s.strip()
    try:
        obj = json.loads(s)
        return obj if isinstance(obj, dict) else {}
    except json.JSONDecodeError:
        pass
    # Fall back to the first balanced { ... } span.
    start = s.find("{")
    end = s.rfind("}")
    if start >= 0 and end > start:
        try:
            obj = json.loads(s[start : end + 1])
            return obj if isinstance(obj, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}
