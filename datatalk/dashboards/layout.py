"""Accepting a user-edited layout: what may change, and what may not.

The layout editor sends back an *authoring* document -- blocks that reference
datasets by id and column. What it may change is presentation: order, rows,
widths, chart type, titles, which blocks exist. What it may not change is where
a number comes from:

* **No values.** A materialized field (a table's ``rows``, a chart's ``x`` /
  ``series``, a stat's ``value``) is dropped, not trusted. Otherwise an edited
  stat tile could carry a typed-in number, and the product's one rule -- every
  number is materialized from a captured query -- would hold for everyone but
  the dashboard's own editor.
* **No new datasets.** Every reference must name a dataset this dashboard
  already captured, and columns it actually returned. New data arrives through
  the widget endpoints, which bring their own query.

Validation reuses :func:`~datatalk.agent.blocks.validate_references`, so it
cannot disagree with what :func:`~datatalk.agent.blocks.materialize` would do.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from datatalk.agent.blocks import (
    Chart,
    Document,
    Heading,
    Paragraph,
    Row,
    Stat,
    Table,
    validate_references,
)

MAX_BLOCKS = 80
MAX_TEXT = 500


class LayoutError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class _Shape:
    """A stand-in dataset: the columns a query returned, and rows to index."""

    columns: list[str]
    rows: list[list[Any]]


def _shapes(queries: list[dict[str, Any]]) -> dict[str, _Shape]:
    out: dict[str, _Shape] = {}
    for q in queries:
        dataset_id = q.get("dataset_id")
        columns = list(q.get("columns") or [])
        if not dataset_id or not columns:
            continue
        # Enough rows that a stat's row_index is judged against the real count;
        # at least one, so an empty result today is not a structural error.
        n = max(int(q.get("row_count") or 0), 1)
        out[dataset_id] = _Shape(columns, [[None] * len(columns)] * n)
    return out


def _strip(block: Any) -> Any:
    """The block with every materialized field removed."""
    if isinstance(block, Table):
        return replace(block, rows=None)
    if isinstance(block, Chart):
        return replace(block, x=None, series=None, title=(block.title or "")[:MAX_TEXT])
    if isinstance(block, Stat):
        return replace(block, value=None, delta=None, delta_pct=None, label=block.label[:MAX_TEXT])
    if isinstance(block, (Heading, Paragraph)):
        return replace(block, text=block.text[:MAX_TEXT])
    return block


def sanitize(raw: dict[str, Any], queries: list[dict[str, Any]]) -> Document:
    """A safe authoring document from an editor's payload, or LayoutError."""
    if not isinstance(raw, dict) or not isinstance(raw.get("blocks"), list):
        raise LayoutError("layout_invalid", "expected {blocks: [...]}")
    doc = Document.from_dict(raw)

    blocks: list[Any] = []
    count = 0
    for block in doc.blocks:
        if isinstance(block, Row):
            children = []
            for child in block.children:
                if isinstance(child, Row):
                    raise LayoutError("layout_invalid", "rows cannot contain rows")
                children.append(_strip(child))
            if not children:
                continue  # an emptied row is just gone
            count += len(children)
            blocks.append(replace(block, children=children))
        else:
            count += 1
            blocks.append(_strip(block))
    if count > MAX_BLOCKS:
        raise LayoutError("layout_invalid", f"at most {MAX_BLOCKS} blocks")

    out = Document(blocks=blocks)
    errors = validate_references(out, _shapes(queries))
    if errors:
        raise LayoutError("layout_invalid", "; ".join(str(e) for e in errors[:5]))
    return out


def dataset_filter_support(template: dict[str, Any]) -> set[str]:
    """Filter ids a dataset's SQL template can bind: the only ones it can wire."""
    names = {p.get("name", "") for p in template.get("params") or []}
    return {n[2:].rpartition("_")[0] for n in names if n.startswith("p_")}
