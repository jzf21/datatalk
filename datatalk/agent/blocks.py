"""Dataset-referencing block document model (multiagent reporting).

The key trust decision: **the LLM never re-types numbers.** The Analyst captures
query result sets, each addressable by a ``dataset_id``. The Reporter and Q&A
agent emit *authoring* blocks that reference a dataset plus column mappings;
:func:`materialize` resolves those references into concrete values before the
Document is stored or sent to the UI.

A :class:`Document` is an ordered list of typed blocks. Two block families carry
data — ``table`` and ``chart`` — and each has an *authoring* form (references a
dataset by id) and a *materialized* form (holds concrete values). A malformed
reference degrades gracefully to a paragraph note; it never raises.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any

CHART_TYPES = {"bar", "line", "area", "pie"}


# --- block dataclasses -------------------------------------------------------

@dataclass
class Heading:
    text: str
    level: int = 2
    type: str = "heading"


@dataclass
class Paragraph:
    text: str
    type: str = "paragraph"


@dataclass
class Table:
    # authoring form
    dataset_id: str | None = None
    columns: list[str] | None = None  # None = all columns of the dataset
    # materialized form (columns is reused; rows is populated)
    rows: list[list[Any]] | None = None
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
    type: str = "chart"


_BLOCK_CLASSES = {
    "heading": Heading,
    "paragraph": Paragraph,
    "table": Table,
    "chart": Chart,
}


# --- (de)serialization -------------------------------------------------------

def block_to_dict(block: Any) -> dict[str, Any]:
    """Serialize a block, dropping keys whose value is ``None``."""
    return {k: v for k, v in asdict(block).items() if v is not None}


def block_from_dict(data: dict[str, Any]) -> Any:
    """Reconstruct a block from a dict; unknown keys are ignored."""
    kind = data.get("type")
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
    return Table(columns=list(cols), rows=rows)


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
        x={"label": block.x_col, "values": x_values},
        series=series,
    )


def _is_authoring_table(b: Any) -> bool:
    return isinstance(b, Table) and b.rows is None


def _is_authoring_chart(b: Any) -> bool:
    return isinstance(b, Chart) and b.x is None


def materialize(doc: Document, datasets: dict[str, Any]) -> Document:
    """Resolve every authoring table/chart against ``datasets``.

    ``datasets`` maps ``dataset_id`` -> an object exposing ``.columns`` and
    ``.rows`` (e.g. an :class:`~datatalk.agent.executor.QueryResult`). A missing
    dataset id or unknown column degrades that block to a paragraph note rather
    than raising, so one bad reference never aborts a whole report.
    """
    out: list[Any] = []
    for b in doc.blocks:
        if _is_authoring_table(b):
            out.append(_materialize_table(b, datasets))
        elif _is_authoring_chart(b):
            out.append(_materialize_chart(b, datasets))
        else:
            out.append(b)
    return Document(blocks=out)


# --- flattening --------------------------------------------------------------

def _fmt(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def document_to_text(doc: Document) -> str:
    """Flatten a materialized Document to plain text.

    Used by the Analyze agent and for memory embedding, which both expect a
    single narrative string. Tables become pipe rows; charts become a titled
    value summary.
    """
    parts: list[str] = []
    for b in doc.blocks:
        if isinstance(b, Heading):
            level = max(1, min(6, b.level))
            parts.append(f"{'#' * level} {b.text}")
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
